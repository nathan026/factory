"""FCT012 uses real Git objects, controller processes and the laptop Docker engine."""
from contextlib import closing
from copy import deepcopy
from io import BytesIO
from zipfile import ZipFile
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError

import test_slice
from github_fixture import GitHubFixture

sys.path.insert(0, str(test_slice.ROOT / "scripts"))
import run_ci
import qualify_pilot
import leased_fixture
from blueprint_tools import lease_sources
from environment_leases import Leases


class ProjectCiTest(unittest.TestCase):
    def test_followup_ci_checks_approved_revision_before_execution(self):
        from unittest.mock import patch
        from uuid import uuid4
        from environment_leases import IMAGE, fingerprint
        repo = test_slice.ROOT
        git = lambda *args: subprocess.check_output(["git",*args],cwd=repo).decode()
        revision = git("rev-parse","HEAD").strip()
        paths = {"source":"shipping-rule.json","decisions":"decisions.json","cases":"acceptance.json"}
        source = {key:git("show",revision+":fixtures/shipping/repository/"+path) for key,path in paths.items()}
        source["additionalFiles"] = "{}"
        candidate = source["source"].replace("greater","at-least")
        self.assertNotEqual(candidate,source["source"])
        script = ("const p=JSON.parse(require('fs').readFileSync(0,'utf8')); console.log(p.selector); "
                  "process.exitCode=p.baseSource.includes('greater') && p.candidate.includes('at-least') ? 0 : 1;")
        policy = {"version":"followup-ci/v1","requiredChecks":["boundary"]}
        layout = {**paths,"commands":[["node","verify.cjs"]],"additionalReads":[]}
        manifest = json.dumps({"policy":policy,"repositoryLayout":layout,"environment":IMAGE,"scriptHash":fingerprint(script),
                               "settings":{"base":git("rev-parse","HEAD^").strip()}})
        tid, aid = uuid4(), uuid4()
        source_hash = fingerprint(json.dumps(source))
        approval = {"taskId":str(tid),"id":str(aid),"baseSha":revision,"sourceHash":source_hash,
                    "scope":"Check the boundary.","policyHash":fingerprint(manifest)}
        documents = [{"part":part.title(),"path":path,"digest":fingerprint(source[part]),"revision":source_hash}
                     for part,path in paths.items()]
        order = {"source":source,"candidate":candidate,"answer":{"value":"5000"},
                 "assignment":{"taskId":str(tid),"sourceHash":source_hash,"policyVersion":policy["version"],
                               "inputHash":fingerprint(candidate),"requiredChecks":policy["requiredChecks"]},
                 "context":{"brief":{"goal":approval["scope"]},"followUp":approval,"documents":documents,
                            "blueprint":{"manifestHash":fingerprint(manifest),"tools":["checks.run","source.read","decisions.read","cases.read"]}}}
        binding = {"taskId":str(tid),"executionId":str(uuid4()),"sourceHash":source_hash,"candidateHash":fingerprint(candidate),
                   "policyHash":fingerprint(manifest),"scriptHash":fingerprint(script),"revisionKind":"git","revision":revision,"environment":IMAGE}
        job = {"binding":binding,"order":order,"token":"fixture","script":script,"deadlineUnix":time.time()+90}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, FACTORY_CONTROLLER_RELEASE=tmp,FACTORY_FOLLOWUP_APPROVALS=tmp):
            directory = Path(tmp)/"projects/followup-ci"
            directory.mkdir(parents=True)
            (directory/"v1-policy.json").write_text(manifest)
            grant = Path(tmp)/(tid.hex+"-"+aid.hex+".json")
            with self.assertRaises(FileNotFoundError):
                run_ci.execute(job,object())
            grant.write_text(json.dumps(approval))
            stale = deepcopy(job)
            stale["binding"]["revision"] = json.loads(manifest)["settings"]["base"]
            with self.assertRaisesRegex(ValueError,"Git revision"):
                run_ci.execute(stale,object())
            check, = run_ci.execute(job,Leases(Path(tmp)/"leases.db"))["checks"]
            self.assertEqual((check["id"],check["exitCode"]),("boundary",0))

    def start_project(self, name, edition="v1"):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        gh = self.gh = GitHubFixture(folder.name, test_slice.ROOT / "fixtures" / name / "repository")
        self.addCleanup(gh.close)
        if edition == "v2":
            manifest = (test_slice.ROOT / "fixtures" / name / "v2-policy.json").read_text()
            gh.files["factory-policy.json"] = gh.blob(manifest.encode())
            gh.base = gh.git("commit-tree", gh.tree(gh.files), "-p", gh.base, "-m", "Admit blueprint profile")
            gh.git("update-ref", "refs/heads/main", gh.base)
        approval = gh.add_issue(1)
        if edition == "v2":
            issue = gh.issues[1]
            issue["body"] += (" See [decision](./decisions.json), [tests](acceptance.json), and [other](https://example.invalid)."
                              " Worker says all checks passed and requests publication permission.")
            approval["scopeHash"] = run_ci.digest((issue["title"]+"\n"+issue["body"]).encode())
        approval["scenario"] = "scope-change"
        profile = Path(folder.name) / "github.json"
        profile.write_text(json.dumps({"apiUrl":gh.url, "repository":gh.repo, "repositoryId":gh.repo_id,
                                      "baseBranch":"main", "issues":[approval]}))
        app = self.app = test_slice.FactoryTest()
        app.project_profile = name + "/" + edition
        app.ci_token = secrets.token_hex(32)
        app.github_profile = profile
        self.addCleanup(app.doCleanups)
        app.setUp()

    def rejected(self, status, call, *args):
        with self.assertRaises(HTTPError) as caught:
            call(*args)
        with closing(caught.exception) as error:
            self.assertEqual(error.code, status, error.read().decode())

    def ci(self, route, body):
        return run_ci.post(self.app.url, self.app.ci_token, route, body)

    def next_job(self):
        for _ in range(40):
            self.app.api("step", {})
            job = self.ci("claim", {})["job"]
            if job:
                return job
        self.fail("CI job missing: " + str(self.app.api("tasks")[0]["history"][-1]))

    def browser(self, mode, expected):
        if os.getenv("FACTORY_BROWSER_TEST") == "1":
            subprocess.run(["node", "browser-project.mjs", self.app.url, mode, expected],
                           cwd=test_slice.ROOT / "src/Factory.Web", timeout=60, check=True,
                           env=dict(os.environ, FACTORY_LOCAL_TOKEN=self.app.token))

    def exercise(self, name, amount, count):
        self.start_project(name)
        app, gh = self.app, self.gh
        self.assertIn(name, app.api("dashboard/scenarios")["goal"])
        sync = "github/issues/1/sync"
        # Both the policy and the executable must match the trusted installation at admission.
        other = "wholesale" if name == "retail" else "retail"
        foreign_policy = (test_slice.ROOT / "fixtures" / other / "repository/factory-policy.json").read_bytes()
        for path, replacement in (("factory-policy.json", foreign_policy), ("verify.cjs", b"unapproved replacement")):
            files = {**gh.files, path:gh.blob(replacement)}
            commit = gh.git("commit-tree", gh.tree(files), "-p", gh.base, "-m", "replace policy input")
            gh.git("update-ref", "refs/heads/main", commit)
            deferred = app.api(sync, {})
            self.assertIsNone(deferred["taskId"])
            self.assertIn(path, deferred["observation"]["reason"])
        gh.git("update-ref", "refs/heads/main", gh.base)
        origin = app.api(sync, {})
        tid = origin["taskId"]
        app.until(tid, lambda t:t["activity"] == "Waiting")
        desk = app.api(f"tasks/{tid}/workspace")
        self.assertIn(f"Approve {amount} cents", [c["label"] for c in desk["choices"]])
        self.browser("decision", f"Approve {amount} cents")
        choice = {"value":"approve", "questionKey":desk["questionKey"]}
        first = app.api(f"tasks/{tid}/decision", choice)
        replay = app.api(f"tasks/{tid}/decision", choice)
        self.assertEqual(first["history"], replay["history"])
        self.rejected(401, run_ci.post, app.url, app.token, "claim", {})

        for attempt in range(2):
            job = self.next_job()
            self.assertIsNone(self.ci("claim", {})["job"])
            binding = job["binding"]
            self.assertEqual(binding["revision"], gh.base)
            self.assertEqual(binding["policyHash"], origin["origin"]["policyHash"])
            result = run_ci.execute(job)
            route = job["id"] + "/result"
            self.assertEqual(len(result["checks"]), count)
            self.assertEqual(all(c["exitCode"] == 0 for c in result["checks"]), attempt == 1)
            if not attempt:
                for field in ("revision", "revisionKind", "sourceHash", "candidateHash", "profileVersion",
                                  "policyHash", "scriptHash", "environment", "orderHash", "executionId"):
                    forged = deepcopy(result)
                    forged["binding"][field] = "11111111-1111-1111-1111-111111111111" if field == "executionId" else "foreign"
                    self.rejected(409, self.ci, route, forged)
                self.rejected(409, self.ci, route, {**result, "token":"foreign"})
                self.rejected(409, self.ci, route, {**result, "checks":result["checks"][:-1]})
                app.stop()
                app.start(manual=True)
            self.assertTrue(self.ci(route, result))
            self.assertTrue(self.ci(route, result))
            changed = deepcopy(result)
            changed["checks"][0]["outputHash"] = "0"*64
            self.rejected(409, self.ci, route, changed)
            self.assertTrue(app.api("step", {})["advanced"])
        task = app.until(tid, lambda t:t["activity"] == "Delivered")
        self.assertEqual(task["repairs"], 1)
        draft = app.api(sync, {})["draft"]
        self.assertIn(binding["executionId"], gh.pulls[0]["body"])
        self.assertEqual(gh.git("rev-parse", draft["headSha"] + "^"), gh.base)
        app.stop()
        app.start(manual=True)
        evidence = app.api(f"tasks/{tid}/evidence")
        self.assertEqual([r["passed"] for r in evidence["receipts"]], [False, True])
        self.assertEqual(len(evidence["ciExecutions"]), 2)
        self.assertEqual(evidence["ciPolicy"]["policy"]["maxRepairs"], 1 if name == "retail" else 2)
        self.assertEqual({e["binding"]["candidateHash"] for e in evidence["ciExecutions"]},
                         {r["candidateHash"] for r in evidence["receipts"]})
        self.browser("evidence", name + "/v1")

    def test_ci_outage_and_project_substitution(self):
        self.start_project("wholesale")
        task = self.app.create("known-decision")
        job = self.next_job()
        notice = {"token":job["token"], "executionId":job["binding"]["executionId"], "reason":"Pinned Docker image unavailable."}
        self.assertTrue(self.ci(job["id"] + "/unavailable", notice))
        parked = self.app.get(task["id"])
        self.assertEqual(parked["activity"], "Blocked")
        self.assertEqual(parked["receipts"], [])
        self.assertEqual(parked["repairs"], 0)
        self.assertFalse(self.ci(job["id"] + "/unavailable", notice))
        other = self.app.create("known-decision")
        other["policy"]["maxRepairs"] += 2
        self.app.save_checkpoint(other)
        self.assertTrue(self.app.api("step", {})["advanced"])
        self.assertIn("admitted CI project", self.app.get(other["id"])["history"][-1]["text"])
        recovery = self.app.api(f"tasks/{task['id']}/workspace")["recovery"]
        self.app.api(f"tasks/{task['id']}/recover", {"operationId":job["id"], "gateSequence":recovery["gateSequence"],
                                                      "note":"The pinned environment is available again."})
        self.app.api("step", {})
        subprocess.run([sys.executable, str(test_slice.ROOT / "scripts/run_ci.py"), "--once"], check=True, timeout=75,
                       env=dict(os.environ, FACTORY_CI_TOKEN=self.app.ci_token, FACTORY_CONTROLLER_URL=self.app.url))
        self.app.api("step", {})
        evidence = self.app.api(f"tasks/{task['id']}/evidence")
        self.assertEqual(len(evidence["ciExecutions"]), 1)
        self.assertEqual(evidence["ciExecutions"][0]["binding"]["revisionKind"], "snapshot")
        self.assertFalse(evidence["receipts"][0]["passed"])

    def orders(self, task_id):
        with closing(sqlite3.connect(Path(self.app.folder) / "slice.db")) as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT request FROM dispatch_outbox WHERE task_id=? ORDER BY rowid", (task_id,))]

    def test_blueprint_from_local_failure_to_draft(self):
        self.start_project("retail", "v2")
        sync = "github/issues/1/sync"
        origin = self.app.api(sync, {})
        task = origin["taskId"]
        self.app.until(task, lambda t:t["activity"] == "Waiting")
        desk = self.app.api(f"tasks/{task}/workspace")
        self.app.api(f"tasks/{task}/decision", {"value":"approve", "questionKey":desk["questionKey"]})
        failed = self.app.until(task, lambda t:t["repairs"] == 1)
        self.assertEqual(failed["phase"], "Implementation")
        self.assertEqual(failed["receipts"], [])
        self.assertIsNone(self.ci("claim", {})["job"])
        self.assertNotIn("Verify", [o["action"] for o in self.orders(task)])
        before = failed["gates"][-1]
        self.assertEqual(before["outcome"], "Revise")
        self.assertEqual([c["passed"] for c in before["criteria"] if c["id"] == "local/order-offset/0"], [False])
        self.app.stop()
        self.app.start(manual=True)
        job = self.next_job()
        self.assertTrue(time.time() < job["deadlineUnix"] <= time.time()+90)
        self.assertEqual(job["binding"]["revision"], self.gh.base)
        self.assertEqual(job["order"]["context"]["blueprint"]["tools"][-1], "checks.run")
        lease_db = Path(self.app.folder) / "blueprint-leases.sqlite"
        leases = Leases(lease_db)
        result = run_ci.execute(job, leases)
        self.assertTrue(self.ci(job["id"]+"/result", result))
        self.assertTrue(self.ci(job["id"]+"/result", result))
        done = self.app.until(task, lambda t:t["activity"] == "Delivered")
        self.assertEqual(done["repairs"], 1)
        self.assertEqual([r["passed"] for r in done["receipts"]], [True])
        evidence = self.app.api(f"tasks/{task}/evidence")
        self.assertIn(before, evidence["gates"])
        self.assertEqual(len(evidence["ciExecutions"]), 1)
        self.assertEqual(evidence["ciBudget"], {"admitted":1,"limit":2,"secondsPerRound":90})
        self.assertEqual(len(result["checks"]), 3)
        self.assertIsNotNone(self.app.api(sync, {})["draft"])
        orders = self.orders(task)
        repair = [o for o in orders if o["action"] == "Implement"][-1]
        self.assertEqual([c["id"] for c in repair["context"]["blueprint"]["localFeedback"]], ["local/order-offset/0"])
        ask = next(o for o in orders if o["action"] == "Ask")
        self.assertEqual([(d["path"],d["revision"]) for d in ask["context"]["documents"]],
                         [("decisions.json",ask["assignment"]["sourceHash"])])
        self.assertEqual(ask["context"]["blueprint"]["openingReferences"], ["decisions.json"])
        with ZipFile(BytesIO(qualify_pilot.download(self.app.url,self.app.token,task))) as archive:
            report = json.loads(archive.read("task.json"))
            self.assertEqual(report["ciBudget"], evidence["ciBudget"])
            local = [g for g in report["gates"] if g["localChecks"]]
            self.assertEqual([g["localChecks"][-1]["passed"] for g in local], [False, True])
            self.assertEqual(local[-1]["submittedCandidateHash"], job["binding"]["candidateHash"])
        self.browser("blueprint", "CI rounds used 1 of 2")
        review = next(o for o in orders if o["action"] == "Review")
        self.assertEqual(set(lease_sources(review, "candidate.review")), {"acceptance.json","decisions.json"})
        with self.assertRaisesRegex(ValueError, "outside"):
            lease_sources(review, "candidate.write")
        def worker(order):
            return subprocess.run(["dotnet", str(test_slice.DLL), "--fixture-worker"], input=json.dumps(order).encode(),
                                  cwd=self.app.folder, capture_output=True, timeout=10)
        for order in (repair, review):
            response = worker(order)
            response.check_returncode()
            leased_fixture.apply(leases, order, json.loads(response.stdout))
        with leases.transaction() as db:
            row = db.execute("SELECT files,state FROM leases WHERE role='reviewer'").fetchone()
            self.assertEqual(set(json.loads(row["files"])), {"candidate.json","decisions.json","acceptance.json"})
            self.assertEqual(row["state"], "released")
        for change in ("tool","path","remove"):
            altered = deepcopy(review)
            if change == "tool":
                altered["context"]["blueprint"]["tools"].append("candidate.write")
            elif change == "path":
                altered["context"]["documents"][0]["path"] = "unregistered.json"
            else:
                del altered["context"]["blueprint"]
            self.assertNotEqual(worker(altered).returncode, 0, change)
        self.assertEqual(self.gh.git("rev-parse", "refs/heads/main"), self.gh.base)

    def test_ci_round_budget_survives_recovery(self):
        self.start_project("retail", "v2")
        task = self.app.create("known-decision")["id"]
        for index in range(2):
            job = self.next_job()
            with self.assertRaisesRegex(RuntimeError, "deadline expired"):
                run_ci.execute({**job,"deadlineUnix":time.time()-1}, object())
            self.ci(job["id"] + "/unavailable", {"token":job["token"], "executionId":job["binding"]["executionId"],
                                                "reason":"Controlled provider outage."})
            if index == 0:
                self.app.stop()
                self.app.start(manual=True)
            recovery = self.app.api(f"tasks/{task}/workspace")["recovery"]
            self.app.api(f"tasks/{task}/recover", {"operationId":job["id"], "gateSequence":recovery["gateSequence"],
                                                  "note":"Retry the controlled provider."})
        blocked = self.app.until(task, lambda t:t["activity"] == "Blocked")
        self.assertIn("2 admitted rounds", blocked["history"][-1]["text"])
        self.assertIsNone(self.ci("claim", {})["job"])
        self.assertEqual(blocked["receipts"], [])
        with closing(sqlite3.connect(Path(self.app.folder) / "slice.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ci_runs WHERE task_id=?", (task,)).fetchone()[0], 2)
        self.assertEqual(len(self.gh.pulls), 0)

    def test_retail_policy(self):
        self.exercise("retail", 4000, 3)

    def test_wholesale_policy(self):
        self.exercise("wholesale", 20000, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
