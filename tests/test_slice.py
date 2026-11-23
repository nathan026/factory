"""Run against real controller processes in a temporary directory. Requires a built slice."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "src" / "Factory.Api"
DLL = API / "bin" / "Debug" / "net10.0" / "Factory.Api.dll"


class FactoryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.token = secrets.token_hex(24)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = None
        self.addCleanup(self.stop)
        self.start(manual=True)

    project_profile = "shipping/v1"
    ci_token = ""
    slack_profile = None
    funding_profile = None
    github_profile = None

    def start(self, manual):
        env = dict(os.environ, FACTORY_PROJECT=self.project_profile, FACTORY_CI_TOKEN=self.ci_token, FACTORY_PORT=str(self.port), FACTORY_STATE=self.folder,
                   FACTORY_LOCAL_TOKEN=self.token, FACTORY_MANUAL_DISPATCH=str(int(manual)))
        env.pop("FACTORY_SLACK_PROFILE", None)
        env.pop("FACTORY_SLACK_SIGNING_SECRET", None)
        if self.slack_profile:
            env.update(FACTORY_SLACK_PROFILE=str(self.slack_profile[0]), FACTORY_SLACK_SIGNING_SECRET=self.slack_profile[1])
        env.pop("FACTORY_FUNDING_PROFILE", None)
        if self.funding_profile:
            env["FACTORY_FUNDING_PROFILE"] = str(self.funding_profile)
        env.pop("FACTORY_GITHUB_PROJECT", None)
        env.pop("FACTORY_GITHUB_TOKEN", None)
        if self.github_profile:
            env.update(FACTORY_GITHUB_PROJECT=str(self.github_profile), FACTORY_GITHUB_TOKEN="must-stay-off-loopback")
        self.process = subprocess.Popen(["dotnet", str(DLL)], env=env, cwd=API,
                                        stdout=subprocess.DEVNULL)
        for _ in range(160):
            self.assertIsNone(self.process.poll(), "Controller exited during startup")
            try:
                self.api("tasks")
                return
            except OSError:
                time.sleep(.05)
        self.fail("Controller did not accept requests")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=10)

    def api(self, path, data=None, token=None):
        req = Request(self.url + "/api/" + path,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={"Authorization":"Bearer " + (self.token if token is None else token),
                               "Content-Type":"application/json"})
        with urlopen(req, timeout=4) as response:
            return json.load(response)

    def create(self, scenario):
        return self.api("tasks", {"id":str(uuid4()), "scenario":scenario})

    def get(self, task_id):
        return next(t for t in self.api("tasks") if t["id"] == task_id)

    def until(self, task_id, matches):
        for _ in range(50):
            task = self.get(task_id)
            if matches(task):
                return task
            self.assertTrue(self.api("step", {})["advanced"], "Unexpected idle dispatch")
        self.fail("Exceeded the bounded number of steps")

    def save_checkpoint(self, task):
        # Manual dispatch leaves this test-owned checkpoint idle until the next step.
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            db.execute("UPDATE tasks SET document=?, activity=? WHERE id=?",
                       (json.dumps(task), task["activity"], task["id"]))
            db.commit()

    def test_owner_context_across_processes(self):
        tid = self.create("scope-change")["id"]
        route = f"tasks/{tid}/owner"
        initial = self.api(route)
        self.assertEqual([b["version"] for b in initial["briefs"]], [1])
        self.assertEqual({c["id"] for c in initial["briefs"][0]["requirements"]["criteria"]},
                         set(initial["policy"]["requiredChecks"]))
        self.until(tid, lambda t: t["activity"] == "Waiting")
        waiting = self.api(route)
        self.assertTrue(waiting["openQuestion"]["reserved"])
        self.assertEqual(waiting["nextAction"], "Await operator decision")
        self.assertEqual(waiting["decisions"], [])
        self.stop()
        self.start(manual=True)
        self.assertEqual(waiting, self.api(route))
        decision = {"value": "approve"}
        self.api(f"tasks/{tid}/decision", decision)
        self.api(f"tasks/{tid}/decision", decision)
        accepted = self.api(route)
        self.assertEqual([b["version"] for b in accepted["briefs"]], [1, 2, 3])
        self.assertEqual(accepted["briefs"][-1]["acceptedDecision"]["value"], "4000")
        self.assertIsNone(accepted["openQuestion"])
        self.assertEqual(accepted["pendingAction"], "Implement")
        record, = accepted["decisions"]
        self.assertEqual((record["actor"], record["authority"], record["scope"]),
                         ("Operator", "Reserved product decision", tid))
        self.assertTrue(record["recordedAt"] and record["historyEntry"])
        self.assertEqual(record["supersedes"], "SHIP-01")
        for doc in accepted["documents"]:
            source = self.api(route + "/documents/" + doc["part"])
            self.assertEqual(source["document"], doc)
            self.assertEqual(hashlib.sha256(source["content"].encode()).hexdigest(), doc["digest"])
            self.assertTrue(source["content"].startswith(doc["excerpt"]))
        self.until(tid, lambda t: t["phase"] == "Review" and t["activity"] == "Running")
        reviewing = self.api(route)
        self.stop()
        self.start(manual=True)
        self.assertEqual(self.api(route), reviewing)
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            orders = [json.loads(r[0]) for r in db.execute("SELECT request FROM dispatch_outbox WHERE task_id=?", (tid,))]
        for order in orders:
            if order["action"] == "Consult":
                self.assertNotIn("context", order)
                self.assertFalse(any(order["source"].values()))
                self.assertEqual([s["path"] for s in order["consultation"]["sources"]],
                                 self.get(tid)["expertProfile"]["documents"])
                continue
            packet = order["context"]
            self.assertEqual(packet["ownerId"], initial["ownerId"])
            self.assertEqual(packet["policy"], initial["policy"])
            self.assertEqual(packet["documents"], accepted["documents"])
            self.assertNotIn("history", packet)
            self.assertNotIn("advice", packet)
            if order["action"] == "Verify":
                self.assertIsNone(order["feedback"])
                self.assertEqual(packet["findings"], [])
            if order["action"] == "Plan":
                self.assertEqual(packet["brief"], initial["briefs"][0])
                self.assertEqual(packet["decisions"], [])
        review, = [o for o in orders if o["action"] == "Review"]
        self.assertEqual(review["id"], reviewing["operationId"])
        self.assertEqual(review["context"]["decisions"], [record])
        self.assertEqual(review["context"]["brief"], accepted["briefs"][-1])
        self.assertEqual(review["context"]["findings"], reviewing["findings"])
        self.assertEqual([all(c["passed"] for c in f["checks"]) for f in reviewing["findings"]], [False, True])
        self.until(tid, lambda t: t["activity"] == "Delivered")

    def test_legacy_owner_pins_excerpt_and_retains_full_source(self):
        task = self.create("known-decision")
        self.stop()
        del task["memory"]
        task["source"]["cases"] += " " * 600
        self.save_checkpoint(task)
        self.start(manual=True)
        route = "tasks/" + task["id"] + "/owner"
        view = self.api(route)
        doc = self.api(route + "/documents/Cases")
        self.assertFalse(doc["document"]["complete"])
        self.assertEqual(doc["content"], task["source"]["cases"])
        self.assertEqual(doc["document"]["excerpt"], doc["content"][:512])
        self.assertEqual(self.get(task["id"])["memory"]["briefs"], view["briefs"])
        self.stop()
        self.start(manual=True)
        self.assertEqual(self.api(route), view)

    def test_fct004_inflight_envelope_survives_upgrade(self):
        tid = self.create("known-decision")["id"]
        task = self.until(tid, lambda t: t["activity"] == "Running")
        self.stop()
        del task["memory"]
        self.save_checkpoint(task)
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            op, raw = db.execute("SELECT id,request FROM dispatch_outbox").fetchone()
            # Keep the exact System.Text.Json encoding of the FCT004 envelope.
            old = raw.split(',"context":', 1)[0] + ',"id":' + json.dumps(op) + '}'
            self.assertNotIn("context", json.loads(old))
            db.execute("UPDATE dispatch_outbox SET request=? WHERE id=?", (old, op))
            db.commit()
        self.start(manual=True)
        route = f"tasks/{tid}/owner"
        self.assertEqual(self.api(route)["operationId"], op)
        done = self.until(tid, lambda t: t["activity"] == "Delivered")
        self.assertEqual(done["ownerId"], task["ownerId"])
        record, = self.api(route)["decisions"]
        self.assertEqual(record["decision"]["id"], "SHIP-01")
        self.assertEqual(record["actor"], "Lifecycle owner")
        self.assertEqual(set(record["sources"]), {"shipping-rule.json", "decisions.json", "acceptance.json"})
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            self.assertEqual(db.execute("SELECT request,applied FROM dispatch_outbox JOIN dispatch_inbox USING(id) "
                                        "WHERE id=?", (op,)).fetchone(), (old, 1))
            self.assertTrue(all(json.loads(r[0])["context"] for r in db.execute(
                "SELECT request FROM dispatch_outbox WHERE id<>?", (op,))))
        self.assertEqual(sum(a["phase"] == "Planning" for a in done["assignments"]), 1)

    def test_abandoned_local_execution_releases_capacity(self):
        task = self.create("known-decision")
        self.until(task["id"], lambda t: t["activity"] == "Running")
        self.stop()
        # Model a killed worker after its claim committed and before it recorded any result.
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            (operation,) = db.execute("SELECT id FROM dispatch_outbox").fetchone()
            db.execute("UPDATE dispatch_outbox SET scheduler_id=? WHERE id=?", ("local." + operation, operation))
            db.execute("INSERT INTO dispatch_inbox(id,claim,deadline) VALUES(?,?,0)", (operation, "abandoned"))
            db.commit()
        self.start(manual=True)
        blocked = self.until(task["id"], lambda t: t["activity"] == "Blocked")
        self.assertEqual(blocked["results"], [])
        self.assertEqual(len(blocked["assignments"]), 1)
        self.assertIn("requires reconciliation", blocked["history"][-1]["text"])
        other = self.create("scope-change")
        self.until(other["id"], lambda t: t["activity"] == "Waiting")
        # Reopen a pre-FCT010 database with the historical dispatch blocker.
        self.stop()
        reason = "Local worker stopped before recording a result. Its execution requires reconciliation."
        blocked["gates"][-1]["reason"] = reason
        blocked["history"][-1]["text"] = reason
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            db.execute("DELETE FROM dispatch_holds")
            db.execute("UPDATE gate_evaluations SET evaluation=? WHERE task_id=? AND seq=?",
                       (json.dumps(blocked["gates"][-1]), task["id"], blocked["gates"][-1]["sequence"]))
            db.commit()
        self.save_checkpoint(blocked)
        self.start(manual=True)
        path = f"tasks/{task['id']}/"
        recovered = self.api(path + "workspace")["recovery"]
        self.assertEqual(recovered["kind"], "Expired claim")
        self.api(path + "recover", {"operationId": operation, "gateSequence": recovered["gateSequence"],
                                    "note": "Confirmed retained local fixture checkpoint."})
        self.until(task["id"], lambda t: t["activity"] == "Delivered")

    def test_committed_output_precedes_stall_inspection(self):
        tid = self.create("known-decision")["id"]
        running = self.until(tid, lambda t: t["activity"] == "Running")
        self.assertTrue(self.api("step", {})["advanced"])  # claim and record the output, without applying it
        self.stop()
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dispatch_inbox WHERE output IS NOT NULL AND applied=0")
                             .fetchone()[0], 1)
            db.execute("UPDATE dispatch_watch SET deadline=0")
            db.execute("UPDATE dispatch_inbox SET deadline=0")
            db.commit()
        self.start(manual=True)
        self.assertTrue(self.api("step", {})["advanced"])
        task = self.get(tid)
        self.assertEqual(task["gates"][-1]["outcome"], "Pass")
        self.assertEqual(task["gates"][-1]["input"]["runId"], running["run"]["runId"])
        self.assertEqual(len(task["assignments"]), 1)
        self.assertIsNone(self.api(f"tasks/{tid}/workspace")["recovery"])

    def test_no_progress_recovery_and_replay(self):
        tid = self.create("known-decision")["id"]
        other = self.create("scope-change")["id"]
        # Stop after a failed verification and its following repair assignment.
        self.until(tid, lambda t: t["repairs"] == 1 and t["activity"] == "Running")
        self.stop()
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            op, = db.execute("SELECT id FROM dispatch_outbox WHERE task_id=? AND id LIKE '%.Implement' "
                             "ORDER BY rowid DESC LIMIT 1", (tid,)).fetchone()
            db.execute("UPDATE dispatch_watch SET deadline=0 WHERE id=?", (op,))
            db.commit()
        self.start(manual=True)
        held = self.until(tid, lambda t: t["activity"] == "Blocked")
        self.until(other, lambda t: t["activity"] == "Waiting")
        path = f"tasks/{tid}/"
        recovery = self.api(path + "workspace")["recovery"]
        self.assertEqual((recovery["kind"], recovery["operationId"]), ("No progress", op))
        for _ in range(6):
            self.assertFalse(self.api("step", {})["advanced"])
        self.assertEqual(self.get(tid), held)
        self.stop()
        self.start(manual=True)
        self.assertEqual(self.api(path + "workspace")["recovery"], recovery)
        command = {"operationId": op, "gateSequence": recovery["gateSequence"], "note": "Inspected the failed check and isolated fixture attempt."}
        for endpoint, data, status, token in [
            (path, command, 401, "wrong"), (f"tasks/{other}/", command, 409, self.token),
            (path, {**command, "gateSequence": 1}, 409, self.token),
            (path, {**command, "note": " "}, 400, self.token),
        ]:
            with self.assertRaises(HTTPError) as err:
                self.api(endpoint + "recover", data, token)
            self.assertEqual(err.exception.code, status)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, replay = pool.map(lambda _: self.api(path + "recover", command), range(2))
        self.assertEqual(first, replay)
        self.assertEqual(first["gates"][-1]["outcome"], "Resume")
        self.assertEqual(first["phase"], held["phase"])
        self.assertEqual(first["receipts"], held["receipts"])
        with self.assertRaises(HTTPError) as err:
            self.api(path + "recover", {**command, "note": "different resolution"})
        self.assertEqual(err.exception.code, 409)
        done = self.until(tid, lambda t: t["activity"] == "Delivered")
        self.assertEqual(done, self.api(path + "recover", command))
        self.assertEqual(done["repairs"], 1)
        self.assertEqual(done["memory"], held["memory"])
        self.assertEqual([r["passed"] for r in done["receipts"]], [False, True])
        self.assertEqual(sum(g["outcome"] == "Resume" for g in done["gates"]), 1)
        self.assertNotEqual(done["writer"], held["assignments"][-1]["runId"])
        self.assertEqual(self.get(other)["activity"], "Waiting")

    def test_gate_and_interrupted_assignment(self):
        task = self.create("known-decision")
        tid = task["id"]
        self.assertEqual(task, self.api("tasks", {"id":tid, "scenario":"known-decision"}))
        failed = self.until(tid, lambda t: len(t["receipts"]) == 1)
        self.assertFalse(failed["receipts"][0]["passed"])
        self.assertEqual(failed["phase"], "Implementation")
        self.assertIsNone(failed["review"])
        self.assertEqual(failed["answer"]["id"], "SHIP-01")
        self.assertIsNone(failed["humanChoice"])
        running = self.until(tid, lambda t: t["activity"] == "Running")
        self.stop()
        self.start(manual=True)
        self.assertEqual(self.get(tid), running)
        finished = self.until(tid, lambda t: t["activity"] == "Delivered")
        self.assertEqual(finished["ownerId"], task["ownerId"])
        self.assertEqual(finished["writer"], running["run"]["runId"])
        self.assertEqual(finished["repairs"], 1)
        self.assertEqual([r["passed"] for r in finished["receipts"]], [False, True])
        for receipt in finished["receipts"]:
            assigned = next(a for a in finished["assignments"] if a["runId"] == receipt["runId"])
            self.assertEqual(assigned["phase"], "Verification")
            self.assertEqual(assigned["inputHash"], receipt["candidateHash"])
            for key in ("taskId", "schemaVersion", "policyVersion", "sourceHash"):
                self.assertEqual(receipt[key], assigned[key])
            self.assertEqual(set(assigned["requiredChecks"]), {c["id"] for c in receipt["checks"]})
            self.assertEqual(hashlib.sha256(receipt["candidate"].encode()).hexdigest(), receipt["candidateHash"])
            self.assertEqual(len(receipt["checks"]), 3)
        self.assertEqual({r["status"] for r in finished["results"]}, {"Completed", "NeedsInput"})
        for r in finished["results"]:
            assigned = next(a for a in finished["assignments"] if a["runId"] == r["assignment"]["runId"])
            self.assertEqual(r["assignment"], {k: assigned[k] for k in r["assignment"]})
        self.assertEqual(finished["review"]["candidateHash"], finished["receipts"][1]["candidateHash"])
        self.assertNotEqual(finished["review"]["runId"], finished["writer"])
        self.assertEqual(sum(e["actor"] == "Planner" for e in finished["history"]), 1)
        self.assertFalse(self.api("step", {})["advanced"])

    def test_reserved_decision_survives_restart_and_releases_capacity(self):
        a = self.create("scope-change")["id"]
        parked = self.until(a, lambda t: t["activity"] == "Waiting")
        self.assertIsNotNone(parked["advice"])
        self.assertIsNone(parked["run"])
        self.assertIsNone(parked["answer"])
        b = self.create("known-decision")["id"]
        self.until(b, lambda t: t["activity"] == "Delivered")
        self.stop()
        self.start(manual=False)
        self.assertEqual(self.get(a), parked)
        path = f"tasks/{a}/decision"
        self.api(path, {"value":"approve"})
        self.api(path, {"value":"approve"})
        with self.assertRaises(HTTPError) as duplicate:
            self.api(path, {"value":"keep"})
        self.assertEqual(duplicate.exception.code, 409)
        for _ in range(120):
            resumed = self.get(a)
            if resumed["activity"] == "Delivered":
                break
            time.sleep(.1)
        self.assertEqual(resumed["activity"], "Delivered")
        self.assertEqual(json.loads(resumed["candidate"])["thresholdCents"], 4000)
        self.assertEqual(sum(e["text"].startswith("Decision recorded") for e in resumed["history"]), 1)
        self.assertEqual(resumed["ownerId"], parked["ownerId"])

    def test_command_authority(self):
        task = self.create("known-decision")
        for path, body, token, expected in [
            ("tasks", None, "wrong", 401),
            ("tasks", {"id":task["id"],"scenario":"scope-change"}, self.token, 409),
            ("tasks", {"id":str(uuid4()),"scenario":"arbitrary-code"}, self.token, 400),
            (f"tasks/{task['id']}/decision", {"value":"keep"}, self.token, 409),
            (f"tasks/{task['id']}/decision", {"value":"unspecified"}, self.token, 400)
        ]:
            with self.subTest(path=path, expected=expected), self.assertRaises(HTTPError) as err:
                self.api(path, body, token)
            self.assertEqual(err.exception.code, expected)
        self.assertEqual(self.get(task["id"]), task)

    def test_foreign_receipt_blocks_delivery_after_restore(self):
        tid = self.create("known-decision")["id"]
        ready = self.until(tid, lambda t: t["phase"] == "Review")
        self.stop()
        # Seed a mismatched receipt in a private, offline test checkpoint.
        ready["receipts"][-1]["taskId"] = str(uuid4())
        self.save_checkpoint(ready)
        self.start(manual=True)
        rejected = self.until(tid, lambda t: t["activity"] == "Blocked")
        self.assertEqual(rejected["phase"], "Review")
        self.assertFalse(self.api("step", {})["advanced"])

    def test_fct006_wait_and_inflight_review_upgrade(self):
        waiting = self.create("scope-change")["id"]
        parked = self.until(waiting, lambda t: t["activity"] == "Waiting")
        tid = self.create("known-decision")["id"]
        active = self.until(tid, lambda t: t["phase"] == "Review" and t["activity"] == "Running")
        original_review = active["run"]["runId"]
        self.stop()
        # Build pre-gate checkpoints while preserving the actual FCT006 dispatch envelope.
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            for task in (parked, active):
                for key in ("gateRule", "gates", "legacyOperation", "legacyWriter", "legacyPlanner", "planningEvidence"):
                    del task[key]
                db.execute("DELETE FROM gate_evaluations WHERE task_id=?", (task["id"],))
                db.execute("UPDATE tasks SET document=? WHERE id=?", (json.dumps(task), task["id"]))
            db.commit()
        self.start(manual=True)
        upgraded = self.until(tid, lambda t: t["activity"] == "Delivered")
        self.assertEqual(upgraded["writer"], active["writer"])
        self.assertEqual(upgraded["repairs"], active["repairs"])
        self.assertEqual(upgraded["receipts"][:-1], active["receipts"])
        gate, = [g for g in upgraded["gates"] if g["input"]["runId"] == original_review]
        self.assertEqual((gate["outcome"], gate["nextPhase"]), ("Revise", "Verification"))
        self.assertNotEqual(upgraded["review"]["runId"], original_review)
        self.api(f"tasks/{waiting}/decision", {"value": "keep"})
        resumed = self.until(waiting, lambda t: t["activity"] == "Delivered")
        self.assertEqual(resumed["ownerId"], parked["ownerId"])
        self.assertEqual(resumed["gates"][0]["nextPhase"], "Implementation")

    def test_gate_journal_rejects_rewritten_review_evidence(self):
        tid = self.create("known-decision")["id"]
        ready = self.until(tid, lambda t: t["phase"] == "Review")
        route = f"tasks/{tid}/gates"
        journal = self.api(route)
        failed, passed = [g for g in journal if g["input"]["phase"] == "Verification"]
        self.assertEqual((failed["outcome"], passed["outcome"]), ("Revise", "Pass"))
        self.assertTrue(any(c["id"] == "order-offset/0" and not c["passed"] for c in failed["criteria"]))
        self.assertEqual(passed["artifactHash"], ready["receipts"][-1]["candidateHash"])
        self.stop()
        ready["receipts"][-1]["checks"][0]["assertion"] = "Different evidence for the same check."
        ready["gates"] = []
        self.save_checkpoint(ready)
        self.start(manual=True)
        self.assertEqual(self.api(route), journal)
        task = self.until(tid, lambda t: t["activity"] == "Blocked")
        latest = self.api(route)
        self.assertEqual(latest[:-1], journal)
        self.assertEqual(latest[-1]["outcome"], "Block")
        self.assertIn("No trusted verification gate", latest[-1]["reason"])
        self.assertFalse(any(a["phase"] == "Review" for a in task["assignments"]))
        self.assertIsNone(self.api(f"tasks/{tid}/workspace")["recovery"])
        with self.assertRaises(HTTPError) as error:
            self.api(f"tasks/{tid}/recover", {"operationId": journal[-1]["input"]["operationId"],
                                            "gateSequence": latest[-1]["sequence"], "note": "Retry"})
        self.assertEqual(error.exception.code, 409)

    def test_receipt_contract_at_review(self):
        tid = self.create("known-decision")["id"]
        ready = self.until(tid, lambda t: t["phase"] == "Review" and t["activity"] == "Running")
        earlier_verifier = ready["receipts"][0]["runId"]
        for name, change, reason in [
            ("old verifier on same task", lambda r: r.update(runId=earlier_verifier), "verifier run"),
            ("old policy", lambda r: r.update(policyVersion="shipping/obsolete"), "stale policy"),
            ("unknown schema", lambda r: r.update(schemaVersion=99), "unsupported schema"),
            ("old gate", lambda r: r.update(gateVersion="controlled-verification/v1"), "unsupported schema"),
            ("omitted passing check", lambda r: r["checks"].pop(), "exactly once"),
            ("duplicate ID", lambda r: r["checks"][1].update(id=r["checks"][0]["id"]), "exactly once"),
            ("different check", lambda r: r["checks"][1].update(id="other"), "exactly once"),
            ("different candidate bytes", lambda r: r.update(candidate="{}"), "different source or candidate"),
        ]:
            with self.subTest(name=name):
                task = deepcopy(ready)
                change(task["receipts"][-1])
                self.save_checkpoint(task)
                self.assertTrue(self.api("step", {})["advanced"])
                denied = self.get(tid)
                self.assertEqual(denied["activity"], "Blocked")
                self.assertIn(reason, denied["history"][-1]["text"])
                self.assertEqual(denied["rejectedReceipt"], {"index": 1, "reason":denied["history"][-1]["text"]})
                self.assertEqual(denied["repairs"], ready["repairs"])
                self.assertIsNone(denied["review"])

    def test_legacy_review_rechecks_before_delivery(self):
        tid = self.create("known-decision")["id"]
        task = self.until(tid, lambda t: t["phase"] == "Review")
        self.stop()
        # The historical format predates both the typed contracts and the gate journal.
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            db.execute("DELETE FROM gate_evaluations WHERE task_id=?", (tid,))
            db.commit()
        for key in ["gateRule", "gates", "contractVersion", "policy", "results"]:
            del task[key]
        for a in task["assignments"]:
            for key in ["schemaVersion", "sourceHash", "taskId", "policyVersion", "requiredChecks"]:
                del a[key]
        for r in task["receipts"]:
            del r["schemaVersion"], r["policyVersion"]
            r["gateVersion"] = "controlled-verification/v1"
            for check in r["checks"]:
                del check["id"]
        self.save_checkpoint(task)
        self.start(manual=True)
        done = self.until(tid, lambda t: t["activity"] == "Delivered")
        self.assertEqual(done["ownerId"], task["ownerId"])
        self.assertEqual(done["repairs"], task["repairs"])
        self.assertEqual(done["answer"], task["answer"])
        self.assertEqual([r["schemaVersion"] for r in done["receipts"]], [0, 0, 1])
        self.assertEqual(done["candidate"], task["candidate"])
        self.assertNotEqual(done["receipts"][-1]["runId"], task["receipts"][-1]["runId"])

    def test_expert_authority_and_legacy_advice(self):
        routine = self.create("expert-comparison")["id"]
        parked = self.create("scope-change")["id"]
        self.until(routine, lambda t: t["activity"] == "Delivered")
        self.until(parked, lambda t: t["activity"] == "Waiting")
        for tid, authority in ((routine, "Lifecycle owner"), (parked, "Operator")):
            route = f"tasks/{tid}/workspace"
            workspace = self.api(route)
            task = self.get(tid)
            consult = workspace["advice"]["consultation"]
            self.assertEqual((consult["taskId"], consult["question"], consult["requiredAuthority"]),
                             (tid, task["question"], authority))
            ask, = [g for g in task["gates"] if (g["input"]["operationId"] or "").endswith(".Ask")]
            self.assertEqual(consult["options"], workspace["choices"])
            self.assertEqual(consult["runId"], ask["input"]["runId"])
            self.assertEqual(consult["briefHash"], ask["input"]["briefHash"])
            self.assertEqual(consult["revision"], ask["input"]["sourceHash"])
            expert, = [g for g in task["gates"] if (g["input"]["operationId"] or "").endswith(".Consult")]
            self.assertNotEqual(expert["input"]["sessionId"], ask["input"]["sessionId"])
            self.assertIsNone(ask.get("adviceHash"))
            self.assertTrue(expert["adviceHash"] and expert["expertProfileHash"])
            docs = self.api(f"tasks/{tid}/owner")["documents"]
            self.assertEqual({s["path"]: s["digest"] for s in consult["sources"]},
                             {d["path"]: d["digest"] for d in docs if d["path"] in task["expertProfile"]["documents"]})
            self.assertEqual(sum(e["actor"] == "Scoped expert" for e in task["history"]), 1)
            self.assertTrue(consult["assumption"] and consult["uncertainty"])
            self.stop()
            self.start(manual=True)
            self.assertEqual(workspace, self.api(route))
        decision, = self.api(f"tasks/{routine}/workspace")["decisions"]
        self.assertEqual((decision["actor"], decision["authority"]), ("Lifecycle owner", "Delegated implementation decision"))
        waiting = self.get(parked)
        self.assertIsNone(waiting["humanChoice"])
        self.assertIsNone(waiting["run"])
        self.assertEqual(waiting["memory"]["decisions"], [])

        # FCT008 saved four advice fields and an escalation gate, without expert provenance.
        self.stop()
        del waiting["expertProfile"], waiting["advice"]["consultation"]
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            for gate in waiting["gates"]:
                gate.pop("adviceHash", None)
                gate.pop("expertProfileHash", None)
                db.execute("UPDATE gate_evaluations SET evaluation=? WHERE task_id=? AND seq=?",
                           (json.dumps(gate), parked, gate["sequence"]))
            db.commit()
        self.save_checkpoint(waiting)
        self.start(manual=True)
        restored = self.api(f"tasks/{parked}/workspace")
        self.assertEqual(restored["advice"], waiting["advice"])
        command = {"value": "keep", "questionKey": restored["questionKey"]}
        self.assertEqual(self.api(f"tasks/{parked}/decision", command), self.api(f"tasks/{parked}/decision", command))
        self.until(parked, lambda t: t["activity"] == "Delivered")

    def test_reference_answers_survive_restart_without_changing_authority(self):
        task = self.create("known-decision")["id"]
        self.until(task,lambda t:t.get("answer") is not None)
        path = f"tasks/{task}/"
        before = self.get(task)
        question = {"id":str(uuid4()),"text":"How does SHIP-01 apply at order-offset/0?"}
        answer = self.api(path+"messages",question)
        self.assertIn("SHIP-01",answer["reply"])
        self.assertIn("5000",answer["reply"])
        self.assertIn("order-offset/0",answer["reply"])
        self.assertIn("decisions.json",answer["reply"])
        self.stop()
        self.start(manual=True)
        self.assertEqual(self.api(path+"messages",question),answer)
        self.assertEqual(self.get(task),before)
        other = self.create("scope-change")["id"]
        waiting = self.until(other,lambda t:t["activity"]=="Waiting")
        reply = self.api(f"tasks/{other}/messages", {"id":str(uuid4()),"text":"SHIP-01?"})
        self.assertNotIn("5000",reply["reply"])
        self.assertEqual(self.get(other),waiting)

    def test_dashboard_discussion_and_queue_recovery(self):
        tid = str(uuid4())
        command = {"id": tid, "scenario": "scope-change", "message": "Please explain the boundary."}
        initial = self.api("tasks", command)
        self.assertEqual(initial, self.api("tasks", command))
        self.assertEqual(initial["history"][0]["text"], command["message"])
        path = f"tasks/{tid}/"
        waiting = self.until(tid, lambda t: t["activity"] == "Waiting")
        workspace = self.api(path + "workspace")
        question = workspace["questionKey"]
        self.assertEqual({c["value"] for c in workspace["choices"]}, {"approve", "keep"})
        for field in ("candidate", "gates", "receipts", "source"):
            self.assertNotIn(field, workspace)
        row, = self.api("dashboard/queue")
        self.assertFalse(set(row["task"]) & {"messages", "history", "receipts", "candidate", "gates", "source"})
        message = {"id": str(uuid4()), "text": "Approve it? Explain the consequences."}
        for route, body in [("dashboard/queue", None), (path + "workspace", None),
                            (path + "evidence", None), (path + "messages", message)]:
            with self.assertRaises(HTTPError) as error:
                self.api(route, body, token="invalid")
            self.assertEqual(error.exception.code, 401)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, replay = pool.map(lambda _: self.api(path + "messages", message), range(2))
        self.assertEqual(first, replay)
        self.assertIn(workspace["advice"]["consequence"], first["reply"])
        self.assertEqual(self.get(tid), waiting)
        self.assertEqual(self.api(path + "workspace")["messages"], [first])
        updated, = self.api("dashboard/queue")
        self.assertEqual(updated, {**row, "revision": row["revision"] + 1})
        for route, body, status in [
            ("tasks", {**command, "message": "Another request"}, 409),
            (path + "messages", {**message, "text": "Different"}, 409),
            (path + "messages", {**message, "text": "  "}, 400),
            (path + "messages", {**message, "text": "x" * 4001}, 400),
            (path + "messages", {"text": "No command ID"}, 400),
            (path + "decision", {"value": "keep", "questionKey": "stale"}, 409),
        ]:
            with self.assertRaises(HTTPError) as error:
                self.api(route, body)
            self.assertEqual(error.exception.code, status)
        self.assertEqual(self.get(tid), waiting)
        self.stop()
        self.start(manual=True)
        self.assertEqual(self.api("dashboard/queue"), [updated])
        choice = {"value": "keep", "questionKey": question}
        self.assertEqual(self.api(path + "decision", choice), self.api(path + "decision", choice))
        done = self.until(tid, lambda t: t["activity"] == "Delivered")
        self.assertEqual(self.api(path + "messages", message), first)
        evidence = self.api(path + "evidence")
        self.assertEqual(evidence["gates"], done["gates"])
        self.assertEqual([r["passed"] for r in evidence["receipts"]], [False, True])
        self.assertIn("Revise", self.api(path + "messages", {"id": str(uuid4()), "text": "What failed?"})["reply"])
        # Reopen an existing checkpoint without a queue projection or admission timestamp.
        self.stop()
        del done["openedAt"]
        self.save_checkpoint(done)
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            db.execute("DELETE FROM dashboard_queue WHERE task_id=?", (tid,))
            db.commit()
        self.start(manual=True)
        recovered, = self.api("dashboard/queue")
        self.assertIsNone(recovered["task"]["openedAt"])
        self.assertEqual(recovered["task"]["id"], tid)
        self.assertEqual(self.api(path + "workspace")["messages"][0], first)
        self.assertEqual(self.api(path + "evidence"), evidence)

    @unittest.skipUnless(os.getenv("FACTORY_BROWSER_TEST") == "1", "enable with -Browser")
    def test_browser_recovery(self):
        tid = self.create("known-decision")["id"]
        self.until(tid, lambda t: t["activity"] == "Running")
        self.stop()
        with closing(sqlite3.connect(Path(self.folder) / "slice.db")) as db:
            db.execute("UPDATE dispatch_watch SET deadline=0")
            db.commit()
        self.start(manual=False)
        subprocess.run(["node", "browser-recovery.mjs", self.url], cwd=ROOT / "src/Factory.Web",
                       env=dict(os.environ, FACTORY_LOCAL_TOKEN=self.token), check=True, timeout=75)
        done = self.get(tid)
        self.assertEqual(done["activity"], "Delivered")
        self.assertEqual(sum(g["outcome"] == "Resume" for g in done["gates"]), 1)

    @unittest.skipUnless(os.getenv("FACTORY_BROWSER_TEST") == "1", "enable with -Browser")
    def test_phone_dashboard(self):
        self.stop()
        self.start(manual=False)
        subprocess.run(["node", "browser-test.mjs", self.url], cwd=ROOT / "src" / "Factory.Web",
                       env=dict(os.environ, FACTORY_LOCAL_TOKEN=self.token), check=True, timeout=100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
