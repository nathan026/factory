"""FCT017 laptop qualification against the local Docker engine and a real controller."""
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

import test_project_ci
from environment_leases import Leases, fingerprint, output
import run_ci


class EnvironmentTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name) / "leases.sqlite"
        self.leases = Leases(self.db)
        self.task = str(uuid4())

    def lease(self, role, files, task=None):
        g = self.leases.acquire(task or self.task, str(uuid4()), role, "test", "qualified-policy", files)
        self.addCleanup(self.leases.release, g)
        return g

    def test_missing_journal_cannot_overlap_a_surviving_writer(self):
        writer = self.lease("writer", {"x.txt":"original"})
        row = self.leases.record(writer)
        alternate = Leases(self.db.with_name("replacement.sqlite"))
        with self.assertRaisesRegex(RuntimeError,"writer"):
            generation = alternate.acquire(self.task,str(uuid4()),"writer","replacement","qualified-policy",{"x.txt":"next"})
            self.addCleanup(alternate.release, generation)
        self.assertEqual(self.leases.record(writer)["state"],"active")
        self.assertTrue(self.leases.owned(row)["State"]["Running"])
        self.leases.release(writer)
        successor = alternate.acquire(self.task,str(uuid4()),"writer","replacement","qualified-policy",{"x.txt":"next"})
        self.addCleanup(alternate.release,successor)
        with self.assertRaisesRegex(RuntimeError,"Retired"):
            self.leases.finish(writer,{"late":True})
        self.assertEqual(alternate.record(successor)["state"],"active")

    def test_concurrent_writer_admission_after_journal_loss(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from unittest.mock import patch
        providers = [self.leases, Leases(self.db.with_name("second.sqlite"))]
        both_observed = Barrier(2)
        def compete(provider):
            engine = provider.docker
            def docker(*args, **kwargs):
                result = engine(*args, **kwargs)
                if args[0] == "ps" and "label=factory.environment.lease.role=writer" in args:
                    both_observed.wait(timeout=10)
                return result
            with patch.object(provider,"docker",side_effect=docker):
                try:
                    return provider.acquire(self.task,str(uuid4()),"writer","Implement","policy",{"x":"test"})
                except RuntimeError:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(compete,providers))
        for provider, generation in zip(providers,outcomes):
            if generation:
                self.addCleanup(provider.release,generation)
        self.assertEqual(sum(g is not None for g in outcomes),1)
        winner = next(p for p,g in zip(providers,outcomes) if g)
        row = winner.record(next(g for g in outcomes if g))
        self.assertTrue(winner.owned(row)["State"]["Running"])
        loser = next(p for p,g in zip(providers,outcomes) if not g)
        with loser.transaction() as db:
            entry = db.execute("SELECT state,outcome_hash FROM leases").fetchone()
            self.assertEqual(tuple(entry),("quarantined",None))

    def test_replaced_engine_with_visible_container_cannot_seal_or_retire(self):
        from unittest.mock import patch
        writer = self.lease("writer", {"x.txt":"original"})
        original = self.leases.docker
        def other_engine(*args, **kwargs):
            if args[0] == "info":
                return subprocess.CompletedProcess(args,0,b"linux/replacement-engine",b"")
            return original(*args, **kwargs)
        with patch.object(self.leases,"docker",side_effect=other_engine):
            with self.assertRaisesRegex(RuntimeError,"engine changed"):
                self.leases.finish(writer,{"late":True})
            with self.assertRaisesRegex(RuntimeError,"engine changed"):
                self.leases.release(writer)
        row = self.leases.record(writer)
        self.assertEqual(row["state"],"quarantined")
        self.assertIsNone(row["outcome_hash"])
        self.assertTrue(self.leases.owned(row)["State"]["Running"])
        with self.assertRaisesRegex(RuntimeError,"Retired"):
            self.leases.finish(writer,{"late":True})
        self.leases.release(writer)

    def test_coding_lease_stops_at_original_dispatch_deadline(self):
        task, run = str(uuid4()), str(uuid4())
        hard_end = int(time.time()) + 120
        args = (task,run,"writer","Implement","example",{"desk.css":"body {}"})
        generation = self.leases.acquire(*args,ttl=300,expires_by=hard_end)
        try:
            self.assertEqual(self.leases.record(generation)["expires"],hard_end)
        finally:
            self.leases.release(generation)
        with self.assertRaisesRegex(RuntimeError,"expired before reservation"):
            self.leases.acquire(task,str(uuid4()),*args[2:],ttl=300,expires_by=time.time()-1)

    def test_startup_inspect_timeout_retires_only_its_container(self):
        from unittest.mock import patch
        sibling = self.lease("reviewer",{"sibling":"keep"}, str(uuid4()))
        engine = self.leases.docker
        created = []
        timed_out = []
        def fault(*args, **kwargs):
            if args[:2] == ("container","inspect") and created and not timed_out and args[2] == created[0]:
                timed_out.append(args[2])
                raise subprocess.TimeoutExpired(args,20)
            result = engine(*args, **kwargs)
            if args[0] == "create":
                created.append(output(result))
            return result
        with patch.object(self.leases,"docker",side_effect=fault), self.assertRaises(subprocess.TimeoutExpired):
            self.leases.acquire(self.task,str(uuid4()),"writer","test","qualified-policy",{"x":"pinned"})
        self.assertEqual(created,timed_out)
        self.assertEqual(len(created),1)
        with self.leases.transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE cid=?",(created[0],)).fetchone()
            self.assertEqual((row["state"],row["outcome_hash"]),("released",None))
            self.assertIsNone(self.leases.owned(row))
        self.assertTrue(self.leases.owned(self.leases.record(sibling))["State"]["Running"])

    def test_self_source_snapshot_and_isolated_cold_review(self):
        root = test_project_ci.test_slice.ROOT
        path = "src/Factory.Api/Program.cs"
        before = (root / path).read_bytes()
        original = subprocess.check_output(["git", "show", "HEAD:" + path], cwd=root).decode()
        for invalid in ({"../outside":"x"}, {"/absolute":"x"}, {".git/config":"x"}, {"a":"x","a/b":"y"}):
            with self.assertRaisesRegex(ValueError, "source path"):
                self.lease("writer", invalid)
        writer = self.lease("writer", {path:original})
        sibling = self.lease("reviewer", {"other.txt":"other task"}, str(uuid4()))
        probe = """
const fs = require('node:fs'); const assert = require('node:assert/strict');
assert.equal(process.getuid(), 1000);
assert.equal(fs.existsSync('/var/run/docker.sock'), false);
assert.equal(fs.existsSync('/source/.git'), false);
assert.equal(Object.keys(process.env).filter(k=>k.startsWith('FACTORY_') || k.includes('TOKEN')).length,0);
assert.equal(fs.readFileSync('/proc/net/route','utf8').trim().split('\\n').length, 1);
assert.throws(()=>fs.writeFileSync('/source/policy', 'x'));
assert.throws(()=>fs.writeFileSync('/etc/toolchain-change', 'x'));
process.stdout.write(fs.readlinkSync('/proc/self/ns/pid'));
"""
        ns = output(self.leases.execute(writer, probe))
        self.assertNotEqual(ns, output(self.leases.execute(sibling, probe)))
        programs = ("const fs=require('node:fs'); "
                    "if(fs.existsSync('/source/other.txt')) throw Error('sibling source'); "
                    "const copy=fs.readFileSync('/source/src/Factory.Api/Program.cs','utf8')+'\\n// controlled candidate\\n'; "
                    "fs.writeFileSync('/work/Program.cs',copy); process.stdout.write(fs.readFileSync('/work/Program.cs'));")
        run = self.leases.execute(writer, programs)
        run.check_returncode()
        candidate = run.stdout.decode()
        self.assertEqual(candidate, original + "\n// controlled candidate\n")
        self.leases.snapshot(writer, "Program.cs")
        with self.assertRaisesRegex(RuntimeError, "earlier writer"):
            self.lease("writer", {path:original})
        missing = self.leases.execute(sibling, "console.log(require('node:fs').existsSync('/source/src/Factory.Api/Program.cs'))")
        self.assertEqual(output(missing), "false")
        self.leases.finish(writer, {"candidate":fingerprint(candidate)})
        self.leases.release(writer)
        with self.assertRaisesRegex(RuntimeError, "Retired"):
            self.leases.snapshot(writer, "Program.cs")
        fresh = Leases(self.db)
        sealed = fresh.record(writer)
        self.assertEqual(fresh.restore_candidate(self.task, candidate), candidate)
        with self.assertRaisesRegex(RuntimeError, "differs"):
            fresh.restore_candidate(self.task, original)
        self.assertEqual(sealed["candidate_hash"], fingerprint(candidate))
        review = self.lease("reviewer", {"Program.cs":sealed["candidate"]})
        reject_write = """const fs=require('node:fs'), assert=require('node:assert/strict');
assert.throws(()=>fs.appendFileSync('/source/Program.cs','unauthorized'));
assert.throws(()=>fs.writeFileSync('/work/Program.cs','unauthorized'));
process.stdout.write(fs.readFileSync('/source/Program.cs'));"""
        observed = fresh.execute(review, reject_write)
        observed.check_returncode()
        self.assertEqual(fingerprint(observed.stdout.decode()), sealed["candidate_hash"])
        self.assertEqual(fresh.record(review)["image"], sealed["image"])
        self.assertEqual((root / path).read_bytes(), before)

    def test_cancel_or_quarantine_before_replacement(self):
        g = self.lease("writer", {"base":"admitted"})
        row = self.leases.record(g)
        # Start a real independent writer process in this PID namespace.
        program = "setInterval(()=>require('fs').appendFileSync('/work/heartbeat','.'),50)"
        output(self.leases.docker("exec", "-d", row["cid"], "node", "-e", program))
        time.sleep(.4)
        heartbeat = self.leases.execute(g, "console.log(require('fs').statSync('/work/heartbeat').size)")
        self.assertGreater(int(output(heartbeat)), 0)
        output(self.leases.docker("rename", row["cid"], "factory-renamed-" + g))
        try:
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                self.leases.release(g)
            self.assertEqual(self.leases.record(g)["state"], "quarantined")
            with self.assertRaisesRegex(RuntimeError, "Retired"):
                self.leases.finish(g, {"outcome":"late"})
            self.assertIsNone(self.leases.record(g)["outcome_hash"])
            with self.assertRaisesRegex(RuntimeError, "earlier writer"):
                self.lease("writer", {"base":"admitted"})
            with self.assertRaisesRegex(RuntimeError, "Retired"):
                self.leases.execute(g, "console.log(1)")
            self.assertEqual(output(self.leases.docker("inspect","--format","{{.State.Running}}",row["cid"])), "true")
        finally:
            output(self.leases.docker("rename", row["cid"], self.leases.name(row)))
        self.assertEqual(self.leases.reconcile(), {})
        self.assertIsNone(self.leases.owned(self.leases.record(g)))
        new = self.lease("writer", {"base":"admitted"})
        self.assertNotEqual(new, g)
        self.assertEqual(output(self.leases.execute(new, "console.log(require('fs').existsSync('/work/heartbeat'))")), "false")
        row = self.leases.record(new)
        output(self.leases.docker("restart", row["cid"]))
        with self.assertRaisesRegex(RuntimeError, "generation has changed"):
            self.leases.snapshot(new, "base")

    def interrupted_create(self):
        ready = self.db.parent / "created"
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--hold-create", str(self.db), str(ready)])
        try:
            for _ in range(200):
                if ready.exists() and ready.read_text():
                    break
                if proc.poll() is not None:
                    self.fail("Worker stopped before creating the environment")
                time.sleep(.1)
            cid = ready.read_text()
        finally:
            proc.kill()
            proc.wait(timeout=15)
        with self.leases.transaction() as db:
            row, = db.execute("SELECT * FROM leases")
            self.assertIsNone(row["cid"])
            self.assertEqual(row["state"], "creating")
            generation = row["generation"]
        return generation, cid

    def test_lost_create_acknowledgement(self):
        generation, cid = self.interrupted_create()
        with self.leases.transaction() as db:
            db.execute("UPDATE leases SET expires=0 WHERE generation=?", (generation,))
        self.addCleanup(self.leases.release, generation)
        restarted = Leases(self.db)
        self.assertEqual(restarted.reconcile(), {})
        archived = restarted.record(generation)
        self.assertEqual((archived["cid"], archived["state"]), (cid, "released"))
        self.assertIsNone(restarted.owned(archived))

    def test_missing_create_resolution(self):
        started = time.monotonic()
        generation, cid = self.interrupted_create()
        row = self.leases.record(generation)
        self.assertEqual(self.leases.owned(row)["Id"], cid)
        # Remove only this drill's proven, never-started immutable container.
        self.assertEqual(self.leases.owned(row)["State"]["Status"], "created")
        output(self.leases.docker("rm", cid))
        with self.assertRaisesRegex(RuntimeError, "Missing create acknowledgement"):
            self.leases.release(generation)
        note = "Controller stopped before the create acknowledgement. Verified disposable container removal."
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.leases.resolve_absent(generation, note)
        with self.assertRaisesRegex(RuntimeError, "earlier writer"):
            self.lease("writer", {"base":"new"}, row["task"])
        with self.leases.transaction() as db:
            db.execute("UPDATE leases SET expires=0 WHERE generation=?", (generation,))
        restarted = Leases(self.db)
        first = restarted.resolve_absent(generation, note)
        self.assertEqual(first["engine"], restarted.engine)
        self.assertTrue(first["principal"])
        self.assertEqual(first, Leases(self.db).resolve_absent(generation, note))
        with self.assertRaisesRegex(RuntimeError, "Conflicting"):
            restarted.resolve_absent(generation, "Another reason")
        self.assertIsNone(restarted.record(generation)["outcome_hash"])
        with self.assertRaisesRegex(RuntimeError, "Retired"):
            restarted.finish(generation, {"outcome":"late"})
        successor = self.lease("writer", {"base":"new"}, row["task"])
        self.assertNotEqual(generation, successor)
        self.assertEqual(output(restarted.execute(successor,"console.log('ready')")), "ready")
        with self.assertRaisesRegex(RuntimeError, "earlier writer"):
            self.lease("writer", {"base":"duplicate"}, row["task"])
        if dest := os.getenv("FACTORY_LEASE_DRILL_REPORT"):
            with Path(dest).open("x", encoding="utf-8") as report:
                json.dump({"evidenceClass":"controlled Docker create interrupted before acknowledgement",
                           "generation":generation,"task":row["task"],"successor":successor,
                           "removedContainer":cid,"engineHash":fingerprint(first["engine"]),
                           "resolutionUtcUnix":first["checked_at"],"operatorRecorded":bool(first["principal"]),
                           "outcomeHash":restarted.record(generation)["outcome_hash"],
                           "lateResultRejected":True,"conflictingResolutionRejected":True,
                           "scriptedOperatorResolutions":1,"modelSessions":0,
                           "elapsedSeconds":time.monotonic()-started},report,indent=2)

    def test_provider_killed_between_create_and_result(self):
        case = test_project_ci.ProjectCiTest()
        self.addCleanup(case.doCleanups)
        case.start_project("retail")
        task = case.app.create("known-decision")
        job = case.next_job()
        path = self.db.parent / "ready"
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--hold", str(self.db), str(path)],
                                   stdin=subprocess.PIPE, cwd=test_project_ci.test_slice.ROOT)
        try:
            process.stdin.write(json.dumps(job).encode())
            process.stdin.close()
            for _ in range(200):
                if path.exists() and path.read_text():
                    break
                if process.poll() is not None:
                    self.fail("CI provider stopped before the crash checkpoint")
                time.sleep(.1)
            generation = path.read_text()
        finally:
            process.kill()
            process.wait(timeout=15)
        self.addCleanup(self.leases.release, generation)
        row = self.leases.record(generation)
        self.assertEqual(row["task"], task["id"])
        self.assertIsNotNone(self.leases.owned(row))
        other = self.lease("reviewer", {"other":"retained"}, str(uuid4()))
        # Advance only the abandoned environment and controller deadlines.
        with self.leases.transaction() as db:
            db.execute("UPDATE leases SET expires=0 WHERE generation=?", (generation,))
        restored = Leases(self.db)
        self.assertEqual(restored.reconcile(), {})
        self.assertIsNone(restored.owned(row))
        self.assertEqual(output(restored.execute(other, "console.log('available')")), "available")
        with self.assertRaisesRegex(RuntimeError, "Retired"):
            restored.execute(generation, "console.log('late')")
        with closing(sqlite3.connect(Path(case.app.folder) / "slice.db")) as controller:
            controller.execute("UPDATE dispatch_inbox SET deadline=0 WHERE id=?", (job["id"],))
            controller.commit()
        # Use an otherwise admissible receipt so rejection proves execution fencing.
        stale = {"binding":job["binding"], "token":job["token"], "checks":[
            {"id":k,"assertion":"late receipt","exitCode":0,"outputHash":"0"*64,"containerId":f"{i:064x}"}
            for i,k in enumerate(job["order"]["assignment"]["requiredChecks"], 1)]}
        case.rejected(409, case.ci, job["id"] + "/result", stale)
        case.app.api("step", {})
        route = "tasks/" + task["id"]
        gate = case.app.api(route + "/workspace")["recovery"]
        case.app.api(route + "/recover", {"operationId":job["id"], "gateSequence":gate["gateSequence"],
                                          "note":"Verified abandoned environment removal."})
        successor = case.next_job()
        result = run_ci.execute(successor, restored)
        self.assertTrue(case.ci(successor["id"] + "/result", result))
        case.rejected(409, case.ci, job["id"] + "/result", stale)
        case.app.api("step", {})
        self.assertEqual(len(case.app.api(route + "/evidence")["ciExecutions"]), 1)


if __name__ == "__main__" and sys.argv[1:2] == ["--hold"]:
    # Pause the actual provider immediately before its first verifier invocation.
    class Hold(Leases):
        def execute(self, generation, *args, **kwargs):
            Path(sys.argv[3]).write_text(generation)
            time.sleep(180)
            raise RuntimeError("Crash checkpoint expired")
    run_ci.execute(json.load(sys.stdin), Hold(sys.argv[2]))
elif __name__ == "__main__" and sys.argv[1:2] == ["--hold-create"]:
    class LostAcknowledgement(Leases):
        def docker(self, *args, **kwargs):
            result = super().docker(*args, **kwargs)
            if args[0] == "create":
                Path(sys.argv[3]).write_text(output(result))
                time.sleep(180)
            return result
    LostAcknowledgement(sys.argv[2]).acquire(str(uuid4()), str(uuid4()), "writer", "test", "policy", {"base":"data"})
elif __name__ == "__main__":
    unittest.main(verbosity=2)
