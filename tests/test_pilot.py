import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from urllib.error import HTTPError
from zipfile import ZipFile

import qualify_pilot
from qualify_pilot import evaluation as ev
import test_slice


class PilotTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.study = Path(temp.name)
        ev.initialize(self.study)

    def observe(self, workflow, i, minutes):
        path = ev.new_record(self.study, workflow, f"task-{i}", f"Matched task scope {i}", f"pair-{i}", "reviewed-draft")
        r = json.loads(path.read_text())
        # Arithmetic inputs are synthetic examples for this test only.
        r.update(reference=f"{workflow}/issue/{i}", authorization=f"allocation/{i}" if workflow == "pilot" else None,
                 outcome="delivered", acceptanceEvidence=f"checks/{i}", quality="pass", security="pass",
                 independentReview=f"review/{i}", acceptedDraftPr=f"{workflow}/pr/{i}", substantiveRepairs=0,
                 attention=None if minutes is None else [
                     {"minutes":minutes,"kind":"avoidable","reason":"Repeated a recorded choice","requiredAuthority":False},
                     {"minutes":3,"kind":"outcome-review","reason":"Reviewed acceptance evidence","requiredAuthority":True}])
        if i == 1 and workflow == "pilot":
            r["failedVerificationEvidence"] = "synthetic failing check / candidate A1"
            r.update(tags=["self-development","deliberate-blocker"], unattended=[{
                "start":"2026-01-01T08:00:00Z","end":"2026-01-01T08:30:00Z","completed":"Approved check finished",
                "progress":"Receipt A1","unresolved":"none","interruptions":[]}])
        path.write_text(json.dumps(r))
        return path

    def test_five_controlled_tasks_and_browser_export(self):
        report = qualify_pilot.run(self.study, browser=os.getenv("FACTORY_BROWSER_TEST") == "1")
        self.assertIsNone(report["comparison"]["matchedReductionPercent"])
        self.assertEqual(report["observations"], [])
        source = next((self.study / "controlled").glob("*.zip"))
        altered = self.study / "altered.zip"
        with ZipFile(source) as src, ZipFile(altered, "w") as dest:
            for name in src.namelist():
                dest.writestr(name, "{}" if name.startswith("sources/") else src.read(name))
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            ev.import_bundle(self.study, altered)

    def test_activity_and_avoidability_classify_the_same_minutes(self):
        path = self.observe("baseline",1,4)
        record = json.loads(path.read_text())
        record["attention"][0]["activity"] = "question-resolution"
        result = ev.validate(record)
        self.assertEqual(result["minutesByActivity"]["question-resolution"],4)
        self.assertEqual(result["minutesByReason"]["avoidable"],4)
        self.assertEqual(result["minutesByActivity"]["unclassified"],3)
        self.assertEqual(result["activeMinutes"],7)
        self.assertIsNone(ev.validate({**record,"attention":None})["minutesByActivity"])
        record["attention"][0]["activity"] = "untimed"
        with self.assertRaisesRegex(ValueError,"operator activity"):
            ev.validate(record)

    def test_complete_matched_cohort_and_read_only_report(self):
        for i in range(1,6):
            self.observe("baseline", i, 4)
            self.observe("pilot", i, 2)
        paths = list((self.study / "observations").glob("*.json"))
        before = {p:p.read_bytes() for p in paths}
        report = ev.evaluate(self.study)
        self.assertTrue(report["comparison"]["targetMet"])
        self.assertEqual(report["comparison"]["matchedReductionPercent"], 50)
        self.assertEqual(report["comparison"]["matchedBaselineAvoidableMinutes"], 20)
        self.assertEqual(report["observations"][0]["activeMinutes"], 7)
        self.assertEqual(report["proposal"]["status"], "proposed for independent review")
        cmd = [sys.executable, str(test_slice.ROOT / "scripts/pilot_evaluation.py"), "report", str(self.study)]
        self.assertEqual(json.loads(subprocess.check_output([*cmd, "--format", "json"])), report)
        self.assertIn("50% target: met", subprocess.check_output(cmd, text=True))
        self.assertEqual({p:p.read_bytes() for p in paths}, before)
        self.assertEqual(sorted(paths), sorted((self.study / "observations").glob("*.json")))

    def test_missing_zero_unmatched_and_failed_are_visible(self):
        self.observe("baseline", 1, 0)
        self.observe("pilot", 1, 1)
        self.observe("baseline", 2, None)
        failure = self.observe("pilot", 2, 0)
        r = json.loads(failure.read_text())
        r.update(outcome="failed", quality="fail", security="unknown", acceptedDraftPr=None)
        failure.write_text(json.dumps(r))
        mismatch = self.observe("baseline", 3, 5)
        r = json.loads(mismatch.read_text())
        r["boundary"] = "code-written"
        mismatch.write_text(json.dumps(r))
        self.observe("pilot", 3, 1)
        report = ev.evaluate(self.study)
        c = report["comparison"]
        self.assertEqual((c["measuredPairs"], c["matchedBaselineAvoidableMinutes"]), (1,0))
        self.assertIsNone(c["matchedReductionPercent"])
        self.assertIsNone(c["targetMet"])
        self.assertEqual(len(c["unmatched"]), 2)
        self.assertIn("failed", ev.markdown(report))
        self.assertEqual(c["qualityAndSecurity"], "failed")
        duplicate = self.observe("baseline", 4, 8)
        self.observe("pilot", 4, 1)
        record = json.loads(duplicate.read_text())
        record["reference"] = "baseline/issue/1"
        duplicate.write_text(json.dumps(record))
        ambiguous = ev.evaluate(self.study)["comparison"]
        self.assertEqual(ambiguous["measuredPairs"], 0)
        self.assertIsNone(ambiguous["matchedReductionPercent"])
        self.assertIn("Repeated baseline observation field 'reference' requires review.", ambiguous["gaps"])
        with self.assertRaises(FileExistsError):
            ev.new_record(self.study, "baseline", "task-1", "changed", None, None)

    def test_separate_subscription_and_api_measurements(self):
        record = json.loads(self.observe("pilot", 1, 2).read_text())
        record["measurementEvidence"] = "native usage and provider receipt"
        record["measurements"] = {"subscriptionInputTokens":100, "subscriptionCachedInputTokens":60,
                                  "subscriptionOutputTokens":10, "apiBilledUsd":0, "waitSeconds":2.5}
        self.assertEqual(ev.validate(record)["measurements"], record["measurements"])
        record["measurements"]["subscriptionCachedInputTokens"] = 101
        with self.assertRaisesRegex(ValueError, "Cached"):
            ev.validate(record)
        record["measurements"]["subscriptionCachedInputTokens"] = 0.5
        with self.assertRaisesRegex(ValueError, "whole count"):
            ev.validate(record)

    def test_invalid_minutes_or_unattended_intervals(self):
        path = self.observe("pilot", 1, 2)
        original = path.read_text()
        for value in (-1, float("nan"), True):
            record = json.loads(original)
            record["attention"][0]["minutes"] = value
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError, "attention"):
                ev.evaluate(self.study)
        record = json.loads(original)
        record["unattended"][0]["end"] = record["unattended"][0]["start"]
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "interval"):
            ev.evaluate(self.study)

    def test_auth_scope_and_known_simulated_cost(self):
        from funding_fixture import Provider
        from uuid import uuid4
        provider = Provider()
        self.addCleanup(provider.close)
        app = test_slice.FactoryTest()
        self.addCleanup(app.doCleanups)
        app.setUp()
        task = app.create("known-decision")["id"]
        other = app.create("known-decision")["id"]
        with self.assertRaises(HTTPError) as err:
            qualify_pilot.download(app.url, "incorrect", task)
        with closing(err.exception) as response:
            self.assertEqual(response.code, 401)
        app.stop()
        app.funding_profile = self.study / "funding.json"
        app.funding_profile.write_text(json.dumps({"fixtureApi":{
            "account":"pilot","model":"fixture-owner","endpoint":provider.url,"accountLimitMicros":1000,"runLimitMicros":600,
            "maxInputBytes":512,"maxOutputTokens":32,"inputMicrosPerToken":1,"outputMicrosPerToken":2,"taskLimitsMicros":{task:800}}}))
        app.start(manual=True)
        app.api(f"tasks/{task}/funding/advice", {"id":str(uuid4()),"question":"Inspect the receipt"})
        modified = app.get(other)
        modified["source"]["cases"] = json.dumps({"private":app.token})
        app.save_checkpoint(modified)
        for tid, expected in ((task,18),(other,0)):
            data = qualify_pilot.download(app.url,app.token,tid)
            facts = ev.read_bundle(BytesIO(data))
            self.assertEqual(facts["costs"]["simulatedEstimatedUsdMicros"], expected)
            self.assertIsNone(facts["costs"]["taskCostUsdMicros"])
            with ZipFile(BytesIO(data)) as archive:
                self.assertNotIn(b"Inspect the receipt", archive.read("task.json"))
                self.assertTrue(all(app.token.encode() not in archive.read(k) for k in archive.namelist()))
                if tid == other:
                    source = next(d for d in json.loads(archive.read("task.json"))["sources"] if d["part"] == "Cases")
                    self.assertIsNone(source["path"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
