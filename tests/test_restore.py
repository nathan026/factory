"""AT25 with a live WAL database, restored controller, dashboard and normal dispatcher."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing, ExitStack
from urllib.error import HTTPError
from unittest.mock import patch
from uuid import uuid4

import test_slice

BACKUP = test_slice.ROOT / "scripts/factory_backup.py"
sys.path.insert(0, str(BACKUP.parent))
import factory_backup


def backup(*args, success=True):
    run = subprocess.run([sys.executable, BACKUP, *map(str, args)], capture_output=True, text=True, timeout=120)
    if success:
        assert run.returncode == 0, run.stderr
        return json.loads(run.stdout)
    assert run.returncode != 0, "Unexpected success"
    return run.stderr


class RestoreTest(unittest.TestCase):
    def test_mismatched_live_journal(self):
        import hashlib
        import sqlite3
        build = factory_backup.BUILD
        manifest = (build / "projects/factory-summary/v2-policy.json").read_text(encoding="utf-8")
        profile = hashlib.sha256(manifest.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as folder, closing(sqlite3.connect(":memory:")) as db:
            path = Path(folder) / "leases.sqlite"
            db.executescript("""
                CREATE TABLE controller_project(id,profile);
                CREATE TABLE dispatch_outbox(id,task_id,run_id,request);
                CREATE TABLE dispatch_inbox(id,output);
                CREATE TABLE dispatch_sessions(operation_id,session_id);
            """)
            db.execute("INSERT INTO controller_project VALUES (1,?)", (profile,))
            db.execute("INSERT INTO dispatch_outbox VALUES ('op','task','run',?)", (json.dumps({"action":"Review"}),))
            result = '{"accepted":true}'
            db.execute("INSERT INTO dispatch_inbox VALUES ('op',?)", (result,))
            with self.assertRaisesRegex(ValueError,"requires"):
                factory_backup.validate_sqlite_receipts(db, build, None)
            with closing(sqlite3.connect(path)) as journal:
                journal.executescript("""
                    CREATE TABLE codex_attempts(task,run,status,generation,action,policy,result);
                    CREATE TABLE leases(task,run,state,generation,operation,policy,outcome_hash);
                """)
                with self.assertRaisesRegex(ValueError,"differs"):
                    factory_backup.validate_sqlite_receipts(db, build, path)
                journal.execute("INSERT INTO codex_attempts VALUES ('task','run','sealed','g','Review','p',?)", (result,))
                journal.execute("INSERT INTO leases VALUES ('task','run','released','g','Review','p',?)",
                                (hashlib.sha256(result.encode()).hexdigest(),))
                journal.commit()
                factory_backup.validate_sqlite_receipts(db, build, path)
                db.execute("UPDATE dispatch_inbox SET output='{}'")
                with self.assertRaisesRegex(ValueError,"differs"):
                    factory_backup.validate_sqlite_receipts(db, build, path)

                db.execute("UPDATE controller_project SET profile='shipping/v1'")
                db.execute("UPDATE dispatch_outbox SET request=?", (json.dumps({"action":"Consult","expertRuntime":"codex-subscription"}),))
                with self.assertRaisesRegex(ValueError,"requires"):
                    factory_backup.validate_sqlite_receipts(db, build, None)
                with self.assertRaisesRegex(ValueError,"differs"):
                    factory_backup.validate_sqlite_receipts(db, build, path)

                db.execute("INSERT INTO dispatch_sessions VALUES ('op','expert-session')")
                journal.execute("UPDATE codex_attempts SET action='Consult',run='expert-session'")
                journal.execute("UPDATE leases SET operation='Consult',run='expert-session'")
                journal.commit()
                db.execute("UPDATE dispatch_inbox SET output=?", (result,))
                factory_backup.validate_sqlite_receipts(db, build, path)
                db.execute("UPDATE dispatch_outbox SET request=?", (json.dumps({"action":"Discuss"}),))
                with self.assertRaisesRegex(ValueError,"differs"):
                    factory_backup.validate_sqlite_receipts(db, build, path)
                journal.execute("UPDATE codex_attempts SET action='Discuss'")
                journal.execute("UPDATE leases SET operation='Discuss'")
                journal.commit()
                factory_backup.validate_sqlite_receipts(db, build, path)

    def test_uncertain_external_receipt_blocks_restore_release(self):
        from funding_fixture import Provider

        with ExitStack() as cleanup:
            root = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
            provider = Provider()
            cleanup.callback(provider.close)
            app = test_slice.FactoryTest()
            cleanup.callback(app.doCleanups)
            app.setUp()
            tid = app.create("known-decision")["id"]
            app.stop()
            app.funding_profile = root / "funding.json"
            app.funding_profile.write_text(json.dumps({"fixtureApi": {
                "account": "pilot", "model": "fixture-owner", "endpoint": provider.url,
                "accountLimitMicros": 1000, "runLimitMicros": 600, "maxInputBytes": 512, "maxOutputTokens": 32,
                "inputMicrosPerToken": 1, "outputMicrosPerToken": 2, "taskLimitsMicros": {tid: 800}}}))
            app.start(manual=True)
            qid = str(uuid4())
            provider.mode = "drop"
            route = f"tasks/{tid}/funding/advice"
            self.assertEqual(app.api(route, {"id": qid, "question": "Inspect the receipt."})["latest"]["state"], "Unconfirmed")
            bundle = root / "bundle"
            backup("create", bundle, "--state", app.folder)
            app.stop()
            restored = root / "restored"
            record = backup("restore", bundle, "--state", restored)
            app.folder = str(restored)
            app.start(manual=False)
            status = app.api("restore")
            self.assertEqual(status["blockers"], [{"kind": "Funding", "id": qid}])
            command = {"restoreId": record["restoreId"], "sourceFenced": True, "note": "Source stopped. Provider evidence pending."}
            for path, payload, code in [
                ("restore/retire", {**command, "operationId": qid}, 409),
                ("restore/release", command, 409),
                (f"{route}/{qid}/resume", {"attempt": 1}, 423)]:
                with self.assertRaises(HTTPError) as error:
                    app.api(path, payload)
                with closing(error.exception) as response:
                    self.assertEqual(response.code, code)
            self.assertEqual(len(provider.calls), 1)

    def test_checkpoint_reconciliation_and_release(self):
        self.app = app = test_slice.FactoryTest()
        self.addCleanup(app.doCleanups)
        app.setUp()
        source = Path(app.folder)
        with tempfile.TemporaryDirectory() as temp:
            self.addCleanup(app.stop)
            root = Path(temp)
            bundle, restored = root / "backup", root / "restored"
            tid = app.create("scope-change")["id"]
            app.until(tid, lambda t: t["activity"] == "Waiting")
            app.api(f"tasks/{tid}/decision", {"value": "approve"})
            app.until(tid, lambda t: t["phase"] == "Review" and t["activity"] == "Running")
            routes = [f"tasks/{tid}/{part}" for part in ("owner", "gates", "evidence", "workspace", "owner/documents/Cases")]
            before = {r: app.api(r) for r in routes}
            self.assertTrue(before[routes[0]]["decisions"])
            self.assertTrue(app.get(tid)["receipts"])
            (source / "private-probe.txt").write_text("excluded credential fixture")
            # Exercise low-space handling without filling the operator's disk.
            with patch.object(factory_backup, "MAX_BYTES", 1), tempfile.TemporaryDirectory(dir=root) as work:
                with self.assertRaisesRegex(ValueError, "laptop limit"):
                    factory_backup.snapshot_sqlite(source, Path(work) / "slice.db")
            checkpoint = backup("create", bundle, "--state", source)
            self.assertFalse((bundle / "private-probe.txt").exists())
            backup("create", bundle, "--state", source, success=False)
            backup("restore", bundle, "--state", source, success=False)
            broken = root / "damaged"
            shutil.copytree(bundle, broken)
            with (broken / "slice.db").open("ab") as f:
                f.write(b"corrupt")
            self.assertIn("checksum", backup("restore", broken, "--state", restored, success=False))
            self.assertFalse(restored.exists())
            changed = root / "changed"
            shutil.copytree(bundle, changed)
            (changed / "profiles/shipping-v1.json").write_text("{}")
            self.assertIn("configuration", backup("restore", changed, "--state", restored, success=False))
            late = app.create("known-decision")["id"]
            result = backup("restore", bundle, "--state", restored)
            self.assertTrue(result["paused"])
            app.stop()
            app.folder = str(restored)
            try:
                app.start(manual=False)
                time.sleep(1)
                status = app.api("restore")
                self.assertTrue(status["paused"])
                self.assertEqual(status["blockers"], [])
                self.assertEqual(status["restore"]["backupId"], checkpoint["backupId"])
                self.assertEqual({r: app.api(r) for r in routes}, before)
                self.assertNotIn(late, [t["id"] for t in app.api("tasks")])
                release = {"restoreId": result["restoreId"], "sourceFenced": True, "note": "Stopped fixture source and inspected retained receipts."}
                for path, body, token, code in [
                    (f"tasks/{tid}/messages", {}, app.token, 423), ("step", {}, app.token, 423),
                    ("github/issues/1/sync", {}, app.token, 423), (f"tasks/{tid}/funding/advice", {}, app.token, 423),
                    ("restore/release", release, "incorrect", 401),
                    ("restore/release", {**release, "restoreId": str(uuid4())}, app.token, 409),
                    ("restore/release", {**release, "sourceFenced": False}, app.token, 400)]:
                    with self.assertRaises(HTTPError) as error:
                        app.api(path, body, token)
                    with closing(error.exception) as response:
                        self.assertEqual(response.code, code)
                app.stop()
                app.start(manual=True)
                self.assertTrue(app.api("restore")["paused"])
                if os.getenv("FACTORY_BROWSER_TEST") == "1":
                    subprocess.run(["node", "browser-restore.mjs", app.url, release["note"]],
                                   env=dict(os.environ, FACTORY_LOCAL_TOKEN=app.token),
                                   cwd=test_slice.ROOT / "src/Factory.Web", check=True, timeout=60)
                record = app.api("restore/release", release)
                self.assertEqual(record, app.api("restore/release", release))
                self.assertTrue(app.api("restore")["paused"])
                app.stop()
                app.start(manual=True)
                self.assertFalse(app.api("restore")["paused"])
                self.assertEqual(app.api("restore")["restore"]["reconciliation"], release["note"])
                app.until(tid, lambda t: t["activity"] == "Delivered")
                app.stop()
                app.folder = str(source)
                app.start(manual=True)
                self.assertEqual({r: app.api(r) for r in routes}, before)
                self.assertIsNone(app.api("restore")["restore"])
                self.assertEqual(app.get(late)["id"], late)
            finally:
                app.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
