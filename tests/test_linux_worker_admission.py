"""Submit a synthetic packet to the packaged adapter using an empty, private sign-in directory."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4


@unittest.skipUnless(sys.platform == "linux" and "FACTORY_CONTROLLER_RELEASE" in os.environ,
                     "requires the Linux bundle and a published controller")
class AdmissionTest(unittest.TestCase):
    def test_signed_out_order_parks_before_tools(self):
        root = Path(os.environ.get("FACTORY_WORKER_BUNDLE", Path(__file__).resolve().parents[1])).resolve()
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            home = tmp / "codex"
            home.mkdir(mode=0o700)
            journal = tmp / "leases.sqlite"
            env = {**os.environ, "HOME":directory, "CODEX_HOME":str(home), "FACTORY_LEASE_DB":str(journal)}
            probe = subprocess.run([root/"scripts/linux-worker.sh","allowance"],env=env,capture_output=True,text=True,timeout=35)
            self.assertEqual(probe.returncode,1)
            self.assertIn("ChatGPT subscription sign-in required",probe.stderr)
            session, task = str(uuid4()), str(uuid4())
            question = {"taskId":task,"ownerId":str(uuid4()),"messageId":str(uuid4()),"text":"Which criterion applies?",
                        "sourceRevision":"a"*64,"briefVersion":1,"briefHash":"b"*64,
                        "evidence":{"path":"owner/accepted.json","digest":hashlib.sha256(b"[]").hexdigest(),"content":"[]"}}
            order = {"action":"Discuss","scenario":"known-decision","discussion":question,
                     "source":{"source":"","decisions":"","cases":""},
                     "assignment":{"taskId":task,"runId":str(uuid4()),"phase":"Implementation","objective":"read",
                                   "inputHash":question["briefHash"],"sourceHash":question["sourceRevision"],"schemaVersion":1,
                                   "policyVersion":"shipping/v1","requiredChecks":[]}}
            # The deployed host must validate the packet before admission. Exercise the
            # same binary selection as the controlled phase process in the bridge, too.
            host = Path(env["FACTORY_CONTROLLER_RELEASE"]) / "Factory.Api"
            plan = {**order, "action":"Plan", "discussion":None}
            checked = subprocess.run([host,"--fixture-worker"],input=json.dumps(plan),capture_output=True,text=True,check=True)
            self.assertEqual(json.loads(checked.stdout)["status"],"Completed")
            (tmp/"alive").touch()
            result = subprocess.run([sys.executable,root/"scripts/codex_worker.py",tmp/"cancelled",
                str(int((time.time()+60)*1000)),session],input=json.dumps(order),env=env,capture_output=True,text=True,timeout=85)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),{"wait":"Authentication","fundingMode":"Subscription"})
            with sqlite3.connect(journal) as db:
                row = db.execute("select run,status,thread,generation,result from codex_attempts").fetchone()
                self.assertEqual(row,(session,"Authentication",None,None,None))


if __name__=="__main__":
    unittest.main()
