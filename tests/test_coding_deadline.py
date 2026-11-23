"""Test the model-to-seal boundary using a local journal and a controlled clock."""
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import codex_worker as worker


class CodingDeadlineTest(unittest.TestCase):
    def test_checkpoint_can_finish_after_model_cutoff_but_inside_reserved_time(self):
        for release_time, expected in [(288,"sealed"), (298,"Coding timeout")]:
            with self.subTest(release_time=release_time), ExitStack() as stack:
                root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                db = root/"leases.sqlite"
                now = [1]
                run = str(uuid4())
                assignment = {"taskId":str(uuid4()),"runId":run,"sourceHash":"test","policyVersion":worker.PROFILE}
                order = {"action":"Implement","assignment":assignment,"context":{"blueprint":{"manifestHash":"test"}}}
                leases = Mock(db=db)
                leases.finish.side_effect = lambda generation, outcome, check: check()
                workspace = Mock(generation="generation",target="style.css",role="writer",changed=True,
                                 events=[{"view":"current","tool":"read_file","path":"style.css"}])
                def snapshot():
                    now[0] = release_time
                    return "controlled candidate"
                workspace.checkpoint.side_effect = snapshot

                def turn(*args):
                    self.assertEqual(args[-1],280)
                    now[0] = 279
                    return str(uuid4()), {"decision":"implemented","summary":"Controlled edit."}, {}

                stack.enter_context(patch.dict(worker.os.environ,{"FACTORY_LEASE_DB":str(db),"FACTORY_CODING_END_MS":"300000"}))
                stack.enter_context(patch.object(worker.time,"time",side_effect=lambda:now[0]))
                stack.enter_context(patch.object(worker,"retired",return_value=False))
                stack.enter_context(patch.object(worker,"allowance",return_value=({},None)))
                stack.enter_context(patch.object(worker.subprocess,"run",return_value=SimpleNamespace(returncode=0,stdout=json.dumps({"assignment":{}}))))
                stack.enter_context(patch.object(worker.subprocess,"check_output",return_value=b"codex-cli 0.171.0"))
                stack.enter_context(patch.object(worker,"Leases",return_value=leases))
                stack.enter_context(patch.object(worker,"open_workspace",return_value=nullcontext(workspace)))
                stack.enter_context(patch.object(worker,"tool_server",return_value=nullcontext(("", ""))))
                stack.enter_context(patch.object(worker,"turn",side_effect=turn))
                response = worker.run(order,root/"cancelled",280,run)
                with closing(sqlite3.connect(db)) as conn:
                    status,result = conn.execute("SELECT status,result FROM codex_attempts WHERE run=?",(run,)).fetchone()
                self.assertEqual(status,expected)
                if expected=="sealed":
                    self.assertEqual(response["output"]["output"],"controlled candidate")
                    self.assertIsNotNone(result)
                else:
                    self.assertEqual(response,{"wait":"Coding timeout","fundingMode":"Subscription"})
                    self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
