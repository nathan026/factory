"""Desk approval through GitHubSync and durable state against an isolated loopback Git repository."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from github_fixture import GitHubFixture

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "tests" / "Factory.ContractTests"


class IssuePickerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run(["dotnet", "build", str(CONTRACT), "--nologo", "--verbosity", "quiet"], check=True, timeout=120)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        sources = ROOT / "fixtures" / "shipping" / "repository"
        self.git = GitHubFixture(self.directory, sources)
        self.addCleanup(self.git.close)
        installed = self.git.add_issue(1)
        for number in range(2, 55):
            self.git.add_issue(number)
        self.git.issues[54]["pull_request"] = {}
        self.git.issues[53]["state"] = "closed"
        self.git.issues[5].update(body="", labels=[])
        self.approvals = self.directory / "approvals"
        self.approvals.mkdir()
        rules = json.loads((ROOT / "fixtures/checkout-scope/v2-policy.json").read_text())
        files = {"source": "shipping-rule.json", "decisions": "decisions.json", "cases": "acceptance.json"}
        for name in files.values():
            shutil.copyfile(sources / name, self.directory / name)
        script = b"console.log('test');"
        (self.directory / "verify.cjs").write_bytes(script)
        rules.update(goal="Apply the installed verification criteria.",
                     policy={"version":"picker/v1","requiredChecks":["bounded-source","theme"],"maxRepairs":1},
                     settings={"repository":self.git.repo,"repositoryId":self.git.repo_id,"base":self.git.base},
                     hostedGitHub={"baseBranch":"main","issue":installed},script="verify.cjs",
                     scriptHash=hashlib.sha256(script).hexdigest(),
                     repositoryLayout={**files,"additionalReads":[],"commands":[["node","verify.cjs"]]})
        rules["blueprint"]["earlyChecks"] = ["bounded-source"]
        (self.directory / "v1-policy.json").write_text(json.dumps(rules))
        self.dll = CONTRACT / "bin/Debug/net10.0/Factory.ContractTests.dll"

    def invoke(self, command, number=5, body=None, enable=True):
        raw = subprocess.check_output(["dotnet", self.dll, "--issue-picker", self.git.url, self.directory],
                                     input=json.dumps(dict(command=command, number=number, body=body, enable=enable)),
                                     text=True, timeout=18,
                                     env={**os.environ, "FACTORY_FOLLOWUP_APPROVALS":str(self.approvals)})
        return json.loads(raw)

    def conflict(self, *args, **kwargs):
        self.assertIn("conflict", self.invoke(*args, **kwargs))

    def test_review_approve_begin_and_poll(self):
        first = self.invoke("list", 1)
        self.assertEqual(first["nextPage"], 2)
        second = self.invoke("list", 2)
        self.assertIsNone(second["nextPage"])
        issues = {i["number"]:i for i in first["items"] + second["items"]}
        self.assertEqual(set(issues), set(range(1,53)))
        self.assertTrue(issues[1]["configured"])
        self.assertTrue(issues[5]["canReview"])
        self.assertFalse(issues[5]["configured"])
        self.conflict("preview", enable=False)
        self.conflict("preview", 54)
        self.conflict("preview", 53)
        self.git.issues[5]["labels"] = [{"name":"factory:hold"}]
        self.conflict("preview")
        self.git.issues[5]["labels"] = []
        view = self.invoke("preview")
        request = {k:view[k] for k in ("repositoryId","issueId","issueHash","baseSha","policyHash","paths","checks")}
        request.update(previous=None, goal="Apply the approved change to this issue.")
        for field, value in (("issueHash","0"*64), ("baseSha","0"*40), ("policyHash","0"*64),("issueId",9999)):
            self.conflict("approve", body={**request,field:value})

        def selection(saved):
            return dict(repositoryId=request["repositoryId"], issueId=request["issueId"],
                        scopeHash=request["issueHash"], scopeApproval=saved["id"])
        custom = self.invoke("approve", body={**request, "paths":["new-file.md"]})
        self.assertFalse(self.invoke("preview")["canBegin"])
        self.conflict("start",body=selection(custom))
        self.conflict("approve",body=request)
        custom = self.invoke("approve", body={**request, "checks":["Use a different verifier."], "previous":custom["id"]})
        self.conflict("start",body=selection(custom))
        approved = self.invoke("approve", body={**request, "previous":custom["id"]})
        self.assertTrue(self.invoke("preview")["canBegin"])
        self.assertEqual(self.invoke("approve",body={**request,"previous":custom["id"]})["id"], approved["id"])
        self.conflict("start",body=selection(custom))

        self.git.issues[5]["title"] += " edited"
        self.assertFalse(self.invoke("preview")["canBegin"])
        self.conflict("start",body=selection(approved))
        self.git.issues[5]["title"] = self.git.issues[5]["title"].removesuffix(" edited")
        advanced = self.git.git("commit-tree", self.git.git("rev-parse", "main^{tree}"), "-p", self.git.base, "-m", "Advance main")
        self.git.git("update-ref","refs/heads/main",advanced,self.git.base)
        self.assertFalse(self.invoke("preview")["canBegin"])
        self.conflict("start",body=selection(approved))
        self.git.git("update-ref","refs/heads/main",self.git.base,advanced)
        self.assertEqual(self.invoke("tasks"), [])
        self.assertEqual(list(self.approvals.iterdir()), [])
        self.assertEqual(self.invoke("pollNumbers"), [1])

        with ThreadPoolExecutor(max_workers=2) as simultaneous:
            results = list(simultaneous.map(lambda _:self.invoke("start",body=selection(approved)),range(2)))
        self.assertEqual(results[0],results[1])
        task_id = results[0]
        self.assertIsInstance(task_id,str)
        self.assertEqual(self.invoke("start",body=selection(approved)),task_id)
        self.assertCountEqual(self.invoke("pollNumbers"),[1,5])
        polled = self.invoke("poll")
        self.assertEqual(polled["taskId"],task_id)
        self.assertEqual(polled["origin"]["deskApproval"],approved["id"])
        self.assertEqual(polled["origin"]["baseSha"],request["baseSha"])
        self.assertEqual(polled["origin"]["policyHash"],request["policyHash"])
        task, = self.invoke("tasks")
        self.assertEqual(task["goal"],request["goal"])
        self.assertEqual(task["memory"]["briefs"][-1]["requirements"]["constraints"],[request["goal"]])
        self.assertEqual(task["phase"],"Planning")
        self.assertEqual(task["followUp"]["taskId"],task_id)
        self.assertEqual(json.loads(next(self.approvals.glob("*.json")).read_text()),task["followUp"])
        self.assertEqual([e["actor"] for e in task["history"]].count("Controller"),1)
        self.assertEqual(self.git.writes,[])
