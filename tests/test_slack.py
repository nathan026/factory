"""FCT015 local transport qualification using real controller processes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
from threading import Barrier
import subprocess
import tempfile
import time
import unittest
from urllib.error import HTTPError
from uuid import uuid4

from slack_fixture import SlackFixture
import test_slice


class SlackTest(unittest.TestCase):
    def setUp(self):
        self.slack = SlackFixture()
        self.addCleanup(self.slack.close)
        self.app = test_slice.FactoryTest()
        self.addCleanup(self.app.doCleanups)
        self.app.setUp()
        self.path = Path(self.app.folder) / "slack.json"

    def configure(self, manual=True):
        self.app.stop()
        self.path.write_text(json.dumps(self.slack.profile))
        self.app.slack_profile = (self.path, self.slack.secret)
        self.app.start(manual)

    def waiting(self):
        tid = self.app.create("scope-change")["id"]
        self.app.until(tid, lambda t: t["activity"] == "Waiting")
        return tid

    def poll(self):
        return self.app.api("notifications/poll", {})["attempted"]

    def history(self, task):
        return self.app.api(f"tasks/{task}/notices")

    def rejects(self, code, call, *args, **kwargs):
        with self.assertRaises(HTTPError) as error:
            call(*args, **kwargs)
        with closing(error.exception) as response:
            self.assertEqual(response.code, code)

    def test_delivery_scope_and_replay(self):
        tid = self.waiting()
        self.rejects(404, self.poll)
        self.configure()
        self.rejects(401, self.app.api, "notifications/poll", {}, "wrong")
        with ThreadPoolExecutor(max_workers=3) as pool:
            self.assertEqual(sum(pool.map(lambda _: self.poll(), range(3))), 1)
        self.assertEqual(len(self.slack.messages), 1)
        _, message = self.slack.messages[0]
        link = self.app.url + "/?task=" + tid
        self.assertIn(link, message["text"])
        cb = self.slack.payload(tid)
        click = lambda payload, **kw: self.slack.click(self.app.url, payload, **kw)
        self.rejects(401, click, cb, secret="wrong")
        self.rejects(401, click, cb, timestamp=int(time.time()) - 310)
        self.rejects(401, click, cb, timestamp=int(time.time()) + 310)
        for section, key in (("team","id"), ("channel","id"), ("container","channel_id"), ("user","id")):
            wrong = deepcopy(cb)
            wrong[section][key] = "FOREIGN"
            self.rejects(403, click, wrong)
        self.rejects(403, click, {**cb, "api_app_id":"AOTHER"})
        self.rejects(409, click, {**cb, "container":{**cb["container"], "message_ts":"1700000000.999999"}})
        for field, value, code in (("block_id", str(uuid4()), 409), ("action_id","choice_99",400), ("value","invented",400)):
            self.rejects(code, click, {**cb, "actions":[{**cb["actions"][0], field:value}]})
        self.assertEqual(self.app.get(tid)["activity"], "Waiting")
        # The currently configured allowlist can revoke access to an already-delivered message.
        self.slack.profile["decisionUsers"] = []
        self.configure()
        self.rejects(403, click, cb)
        self.slack.profile["decisionUsers"] = ["UOPERATOR"]
        self.configure()
        if os.getenv("FACTORY_BROWSER_TEST") == "1":
            other = self.app.create("known-decision")["id"]
            subprocess.run(["node","browser-slack.mjs",link,tid,other],
                cwd=test_slice.ROOT / "src/Factory.Web", env=dict(os.environ,FACTORY_LOCAL_TOKEN=self.app.token), check=True, timeout=60)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(click, [cb, cb])), [200,200])
        self.configure()
        self.assertEqual(click(cb), 200)
        self.rejects(409, click, self.slack.payload(tid,"keep"))
        self.app.until(tid, lambda t: t["activity"] == "Delivered")
        decision, = self.app.api(f"tasks/{tid}/owner")["decisions"]
        self.assertEqual((decision["actor"],decision["decision"]["value"]), ("Slack TLOCAL/UOPERATOR","4000"))
        receipt, = self.history(tid)
        self.assertEqual((receipt["state"],receipt["actor"],receipt["attempts"]), ("Sent","UOPERATOR",1))
        self.assertEqual(self.poll(), 0)

    def test_lost_receipt_and_dashboard_race(self):
        self.configure()
        tid = self.waiting()
        self.slack.mode = "drop"
        self.assertEqual(self.poll(), 1)
        self.configure()
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.history(tid)[0]["state"], "Unconfirmed")
        original = self.slack.payload(tid)
        for wrong in ({}, {**original["message"], "app_id":"AOTHER"}, {**original["message"], "blocks":[]}):
            self.rejects(409, self.slack.click, self.app.url, {**original, "message":wrong})
        self.assertEqual(self.slack.click(self.app.url, original), 200)
        self.assertEqual(self.history(tid)[0]["messageTs"], original["container"]["message_ts"])
        self.assertEqual(len(self.slack.messages), 1)
        self.app.until(tid, lambda t: t["activity"] == "Delivered")
        self.slack.mode = "ok"
        other = self.waiting()
        self.poll()
        self.app.api(f"tasks/{other}/decision", {"value":"keep"})
        self.rejects(409, self.slack.click, self.app.url, self.slack.payload(other))
        self.assertEqual(len(self.slack.messages), 2)
        self.assertEqual(len(self.app.api(f"tasks/{other}/owner")["decisions"]), 1)
        race = self.waiting()
        self.poll()
        contenders = (lambda: self.app.api(f"tasks/{race}/decision", {"value":"keep"}),
                      lambda: self.slack.click(self.app.url, self.slack.payload(race)))
        start = Barrier(2)
        def compete(call):
            start.wait(timeout=3)
            try:
                call()
                return 200
            except HTTPError as error:
                with closing(error):
                    return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(compete, contenders)), [200,409])
        decision, = self.app.api(f"tasks/{race}/owner")["decisions"]
        self.assertIn(decision["actor"], ("Operator","Slack TLOCAL/UOPERATOR"))

    def test_rate_limit_and_startup_poll(self):
        tid = self.waiting()
        self.slack.profile["decisionUsers"] = []
        self.configure()
        self.slack.mode = "rate"
        self.assertEqual(self.poll(), 1)
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.history(tid)[0]["state"], "RateLimited")
        time.sleep(2.2)
        self.slack.mode = "ok"
        self.slack.secret = ""  # Link-only mode needs no signing credential.
        self.configure(manual=False)
        for _ in range(60):
            if self.history(tid)[0]["state"] == "Sent":
                break
            time.sleep(.1)
        self.assertEqual(self.history(tid)[0]["attempts"], 2)
        self.assertEqual(len(self.slack.messages), 1)
        self.assertFalse(any(b["type"] == "actions" for b in self.slack.messages[0][1]["blocks"]))

    def test_destination_and_project_must_match(self):
        self.app.stop()
        env = {k:v for k,v in os.environ.items() if not k.startswith("FACTORY_")}
        env.update(FACTORY_SLACK_PROFILE=str(self.path), FACTORY_LOCAL_TOKEN=self.app.token, FACTORY_STATE=self.app.folder,
                   FACTORY_PORT=str(self.app.port), FACTORY_SLACK_SIGNING_SECRET=self.slack.secret)
        for fields in ({"apiOrigin":"https://example.invalid"}, {"apiOrigin":"http://127.0.0.1:1234/?target=other"},
                       {"project":"another/v1"}):
            self.path.write_text(json.dumps({**self.slack.profile, **fields}))
            run = subprocess.run(["dotnet",str(test_slice.DLL)], cwd=test_slice.API, env=env, capture_output=True, timeout=6)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn(b"Invalid project, Slack scope or loopback fixture origin", run.stderr)
        self.assertEqual(self.slack.calls, 0)

    def test_restore_stays_quiet(self):
        from test_restore import backup
        self.configure()
        self.waiting()
        self.app.stop()
        with tempfile.TemporaryDirectory() as temp:
            bundle, restored = Path(temp) / "bundle", Path(temp) / "state"
            backup("create", bundle, "--state", self.app.folder)
            backup("restore", bundle, "--state", restored)
            self.app.folder = str(restored)
            try:
                self.app.start(manual=False)
                self.assertTrue(self.app.api("restore")["paused"])
                self.rejects(423, self.poll)
                self.rejects(423, self.slack.click, self.app.url, {})
                self.assertEqual(self.slack.calls, 0)
            finally:
                self.app.stop()

    def test_controller_stops_between_send_and_receipt(self):
        self.configure()
        tid = self.waiting()
        self.slack.mode = "hold"
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.poll)
            try:
                self.assertTrue(self.slack.entered.wait(3))
                self.app.stop()
            finally:
                self.slack.release.set()
            with self.assertRaises(OSError):
                pending.result()
        self.app.start(manual=True)
        self.assertEqual(self.history(tid)[0]["state"], "Unconfirmed")
        self.assertEqual(self.poll(), 0)
        self.assertEqual(self.slack.calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
