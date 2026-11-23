"""FCT013 qualification using two controller processes and a loopback owner-advice provider."""
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from uuid import uuid4

import test_slice
from funding_fixture import Provider


class FundingTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "funding.json"
        self.provider = Provider()
        self.addCleanup(self.provider.close)
        self.app = test_slice.FactoryTest()
        self.addCleanup(self.app.doCleanups)
        self.app.setUp()
        self.ids = [self.app.create("known-decision")["id"] for _ in range(2)]
        self.base = f"tasks/{self.ids[0]}/funding"
        self.config = {"account":"pilot", "model":"fixture-owner", "endpoint":self.provider.url,
            "accountLimitMicros":1000, "runLimitMicros":600, "maxInputBytes":512, "maxOutputTokens":32,
            "inputMicrosPerToken":1, "outputMicrosPerToken":2, "taskLimitsMicros":dict.fromkeys(self.ids, 800)}
        self.now = datetime.now(timezone.utc).isoformat()
        self.app.funding_profile = self.path

    def configure(self):
        self.app.stop()
        self.path.write_text(json.dumps({"fixtureApi":self.config, "infrastructureCost":{"usdMicros":450, "asOf":self.now,"source":"test meter"},
            "manualApiBalance":{"usdMicros":2000000, "asOf":self.now,"source":"test statement"},
            "subscription":{"auth":"ChatGPT", "observedUtc":self.now, "windows":[{"minutes":300,"usedPercent":75}]}}))
        self.app.start(manual=True)

    def ask(self, request=None, base=None):
        return self.app.api((base or self.base) + "/advice", request or self.question())

    @staticmethod
    def question():
        return {"id":str(uuid4()), "question":"What evidence should I inspect?"}

    def action(self, result, action, base=None):
        return self.app.api((base or self.base) + "/advice/" + result["id"] + "/" + action, {"attempt":result["latest"]["attempt"]})

    def rejects(self, status, call, *args):
        with self.assertRaises(HTTPError) as caught:
            call(*args)
        with closing(caught.exception) as response:
            self.assertEqual(response.code, status, response.read().decode())

    def test_disabled_authority_and_measurements(self):
        self.rejects(409, self.ask)
        self.assertFalse(self.app.api(self.base)["paidApiEnabled"])
        del self.config["taskLimitsMicros"][self.ids[1]]
        self.configure()
        self.rejects(409, self.ask, self.question(), f"tasks/{self.ids[1]}/funding")
        self.rejects(401, self.app.api, self.base + "/advice", self.question(), "wrong")
        request = self.question()
        first = self.ask(request)
        self.assertEqual(first, self.ask(request))
        self.rejects(409, self.ask, {**request, "question":"another question"})
        report = self.app.api(self.base)
        self.assertEqual(report["fixture"]["taskUse"], {"heldMicros":0,"estimatedMicros":18})
        self.assertEqual(report["subscription"]["windows"][0]["usedPercent"], 75)
        self.assertEqual(report["manualApiBalance"]["usdMicros"], 2000000)
        self.assertEqual(report["infrastructureCost"]["usdMicros"], 450)
        self.assertEqual(len(self.provider.calls), 1)
        if os.getenv("FACTORY_BROWSER_TEST") == "1":
            subprocess.run(["node", "browser-funding.mjs", self.app.url, self.ids[0][:8]], cwd=test_slice.ROOT / "src/Factory.Web",
                           env=dict(os.environ, FACTORY_LOCAL_TOKEN=self.app.token), timeout=60, check=True)
            self.assertEqual(len(self.provider.calls), 2)

    def test_parallel_reservation_and_budget_recovery(self):
        self.config["accountLimitMicros"] = 340
        self.configure()
        self.provider.mode = "delay"
        q = self.question()
        other = f"tasks/{self.ids[1]}/funding"
        with ThreadPoolExecutor(max_workers=1) as pool:
            active = pool.submit(self.ask, q)
            try:
                self.assertTrue(self.provider.started.wait(3))
                self.assertEqual(self.ask(q)["latest"]["state"], "InFlight")
                waiting = self.ask(base=other)
                self.assertEqual(waiting["latest"]["state"], "Budget")
                self.assertEqual(len(self.provider.calls), 1)
            finally:
                self.provider.release.set()
            self.assertEqual(active.result()["latest"]["state"], "Completed")
        result = self.action(waiting, "resume", other)
        self.assertEqual(result["latest"]["state"], "Completed")
        self.assertEqual(result["latest"]["attempt"], 2)
        self.assertEqual(result, self.action(waiting, "resume", other))
        self.assertEqual(len(self.provider.calls), 2)

    def test_task_and_run_limits_reset_time_and_account_identity(self):
        self.config["taskLimitsMicros"][self.ids[0]] = 1
        self.configure()
        task_wait = self.ask()
        self.assertIn("task allocation", task_wait["latest"]["reason"])
        self.config["taskLimitsMicros"][self.ids[0]] = 800
        self.config["runLimitMicros"] = 1
        self.configure()
        run_wait = self.action(task_wait, "resume")
        self.assertIn("per-run", run_wait["latest"]["reason"])
        self.assertEqual(len(self.provider.calls), 0)
        self.config["runLimitMicros"] = 600
        self.configure()
        self.provider.mode = "timed-quota"
        blocked = self.action(run_wait, "resume")
        self.assertEqual(self.action(blocked, "resume"), blocked)
        self.assertEqual(self.provider.probes, 0)
        self.config["model"] = "different-model"
        self.configure()
        self.rejects(409, self.action, blocked, "resume")
        self.assertEqual(len(self.provider.calls), 1)

    def test_cancel_retains_reserve_until_provider_confirmation(self):
        self.configure()
        self.provider.mode = "defer"
        pending = self.ask()
        self.provider.mode = "cancel-lost"
        cancelling = self.action(pending, "cancel")
        self.assertEqual(cancelling["latest"]["state"], "CancelPending")
        self.assertGreater(self.app.api(self.base)["fixture"]["taskUse"]["heldMicros"], 0)
        self.configure()
        self.assertEqual(self.action(cancelling, "cancel")["latest"]["state"], "CancelPending")
        self.assertEqual(self.action(cancelling, "reconcile")["latest"]["state"], "Cancelled")
        self.assertEqual(self.app.api(self.base)["fixture"]["taskUse"], {"heldMicros":0,"estimatedMicros":0})
        self.provider.mode = "quota"
        waiting = self.ask()
        cancelled = self.action(waiting,"cancel")
        self.assertEqual(self.action(waiting,"resume"), cancelled)
        self.assertEqual(len(self.provider.calls), 2)

    def test_provider_wait_requires_clearance(self):
        self.configure()
        for mode, kind in (("quota","Quota"),("auth","Authentication")):
            self.provider.mode = mode
            self.provider.ready = False
            blocked = self.ask()
            self.assertEqual(blocked["latest"]["state"], kind)
            count = len(self.provider.calls)
            self.configure()
            for _ in range(3):
                self.app.api("step", {})
                self.app.api(self.base)
            self.assertEqual(self.action(blocked, "resume"), blocked)
            sibling = self.ask()
            self.assertEqual(sibling["latest"]["state"], kind)
            self.assertEqual(len(self.provider.calls), count)
            self.provider.mode = "ready"
            self.provider.ready = True
            self.assertEqual(self.action(blocked, "resume")["latest"]["state"], "Completed")
            self.assertEqual(len(self.provider.calls), count + 1)
        self.assertEqual(self.provider.probes, 4)

    def test_concurrent_rejections_keep_longest_reset(self):
        self.configure()
        self.provider.mode = "delay"
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.ask)
            try:
                self.assertTrue(self.provider.started.wait(3))
                self.provider.mode = "timed-quota"
                self.assertEqual(self.ask()["latest"]["state"], "Quota")
                reset = self.app.api(self.base)["fixture"]["wait"]["retryAt"]
                self.provider.mode = "auth"
            finally:
                self.provider.release.set()
            auth = first.result()
        self.assertEqual(auth["latest"]["state"], "Authentication")
        self.assertEqual(self.app.api(self.base)["fixture"]["wait"]["retryAt"], reset)
        self.assertEqual(self.action(auth, "resume"), auth)
        self.assertEqual(self.provider.probes, 0)

    def test_killed_controller_keeps_inflight_reservation(self):
        self.configure()
        self.provider.mode = "delay"
        q = self.question()
        with ThreadPoolExecutor(max_workers=1) as pool:
            submitting = pool.submit(self.ask, q)
            try:
                self.assertTrue(self.provider.started.wait(3))
                self.app.stop()
            finally:
                self.provider.release.set()
            with self.assertRaises(OSError):
                submitting.result()
        self.app.start(manual=True)
        prior = self.ask(q)
        self.assertEqual(prior["latest"]["state"], "InFlight")
        self.assertGreater(self.app.api(self.base)["fixture"]["accountUse"]["heldMicros"], 0)
        complete = self.action(prior, "reconcile")
        self.assertEqual(complete["latest"]["state"], "Completed")
        self.assertEqual(len(self.provider.calls), 1)

    def test_lost_receipt_restart_and_overrun(self):
        self.configure()
        for mode in ("drop","foreign"):
            self.provider.mode = mode
            request = self.question()
            pending = self.ask(request)
            self.assertEqual(pending["latest"]["state"], "Unconfirmed")
            self.configure()
            self.assertEqual(self.ask(request), pending)
            self.assertGreater(self.app.api(self.base)["fixture"]["accountUse"]["heldMicros"], 0)
            done = self.action(pending, "reconcile")
            self.assertEqual(done["latest"]["state"], "Completed")
            self.assertEqual(done, self.action(pending, "reconcile"))
        self.assertEqual((len(self.provider.calls),self.provider.lookups), (2,2))
        self.provider.mode = "overrun"
        over = self.ask()["latest"]
        self.assertEqual((over["state"],over["estimatedMicros"]), ("Overrun",2010))
        self.assertEqual(self.ask()["latest"]["state"], "Budget")
        self.assertEqual(self.app.api(self.base)["fixture"]["accountUse"]["estimatedMicros"], 2046)


if __name__ == "__main__":
    unittest.main(verbosity=2)
