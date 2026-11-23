"""Offline audit of the controlled FCT018 acquisition study."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from benchmark_readiness import PROFILES, distribution, fingerprint, git, source_packets, summarize


class ReadinessTest(unittest.TestCase):
    def test_failures_stay_in_the_denominator(self):
        rows = [
            {"profile":"shipping", "mode":"warm", "status":"failed"},
            {"profile":"shipping", "mode":"warm", "status":"failed", "readyMs":5, "cleaned":False},
            {"profile":"shipping", "mode":"warm", "status":"passed", "readyMs":10, "cleaned":True},
        ]
        group = summarize(rows)["shipping/warm"]
        self.assertEqual((group["attempted"], group["passed"], group["readinessFailures"], group["cleaned"]),
                         (3, 1, 1, 1))
        self.assertEqual(group["readyMs"], {"n":1,"p50":10,"p95":10})
        self.assertIsNone(group["idleCpuPercent"])
        self.assertEqual(distribution(range(1,21)), {"n":20, "p50":10.5, "p95":19})

    def test_retained_matched_trials_and_sources(self):
        study = json.loads((ROOT / "docs/fct018-readiness.json").read_text(encoding="utf-8"))
        self.assertEqual(study["status"], "passed")
        self.assertEqual(study["outstanding"], [])
        policy = json.loads(git("show", study["sourceCommit"] + ":fixtures/retail/repository/factory-policy.json"))
        self.assertEqual(study["template"]["image"], policy["environment"])
        self.assertEqual(study["template"]["imagePull"], "never")
        self.assertTrue(study["sanitizationControl"]["dirtyMemoryRejected"])
        self.assertFalse(study["sanitizationControl"]["sourceSeeded"])
        self.assertTrue(study["sanitizationControl"]["cleaned"])
        oid, packets = source_packets(study["sourceCommit"])
        self.assertEqual(oid, study["sourceCommit"])
        for name, files in packets.items():
            self.assertEqual(study["sources"][name]["fileHashes"], {k:fingerprint(v) for k,v in files.items()})
            self.assertEqual(study["sources"][name]["bytes"], sum(len(v.encode()) for v in files.values()))
        trials = study["trials"]
        self.assertEqual(study["summary"], summarize(trials))
        keys = {(r["profile"],r["pair"],r["mode"]) for r in trials}
        expected = {(p,i,m) for p in PROFILES for i in range(1,study["pairsPerProfile"]+1) for m in ("cold","warm")}
        self.assertEqual(keys, expected)
        self.assertEqual(len(trials), len(keys))
        generations = [r["generation"] for r in trials + list(study["recovery"].values()) + [study["sanitizationControl"]]]
        self.assertEqual(len(set(generations)), len(generations))
        self.assertEqual(set(study["recovery"]), set(PROFILES))
        for row in trials:
            self.assertEqual(row["status"], "passed")
            self.assertTrue(row["cleanBeforeSeed"] and row["cleaned"] and row["imageCacheHit"])
            self.assertGreater(row["readyMs"], row["seedMs"])
            self.assertGreater(row["teardownMs"], 0)
            if row["mode"] == "warm":
                self.assertGreater(row["prepareMs"], 0)
                self.assertGreater(row["idleMs"], 0)
                self.assertGreater(row["idleMemoryBytes"], 0)
            else:
                self.assertNotIn("prepareMs", row)
        self.assertTrue(all(r["readyMs"] > 0 for r in study["recovery"].values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
