"""Desk approval through GitHubSync and durable state against an isolated loopback Git repository."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
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
        self.bindings = []
        self.dll = CONTRACT / "bin/Debug/net10.0/Factory.ContractTests.dll"

        view = self.invoke("preview")
        self.bindings.append({k:view[k] for k in ("issueId","issueHash","policyHash")} | {"number":5})

    def invoke(self, command, number=5, body=None, enable=True):
        raw = subprocess.check_output(["dotnet", self.dll, "--issue-picker", self.git.url, self.directory],
                                     input=json.dumps(dict(command=command, number=number, body=body, enable=enable, bindings=self.bindings)),
                                     text=True, timeout=18,
                                     env={**os.environ, "FACTORY_FOLLOWUP_APPROVALS":str(self.approvals),
                                          "FACTORY_LEASE_DB":str(self.directory/"synthetic-owner.sqlite")})
        return json.loads(raw)

    def conflict(self, *args, **kwargs):
        self.assertIn("conflict", self.invoke(*args, **kwargs))

    def seal_owner(self,claim,result):
        q = claim["question"]
        doc = q["scopePlanning"]["files"][0]
        events = [dict(tool="read_file",path=x["path"],sha256=x["digest"],view="base") for x in [q["evidence"],doc]]
        raw = json.dumps(result,separators=(",",":"))
        with closing(sqlite3.connect(self.directory/"synthetic-owner.sqlite")) as journal, journal:
            journal.execute("create table if not exists codex_attempts(run,task,action,status,thread,generation,result,events,source,policy)")
            journal.execute("create table if not exists leases(run,task,operation,role,state,generation,outcome_hash,candidate_hash,policy)")
            journal.execute("insert into codex_attempts values(?,?,'Discuss','sealed',?,?,?,?,?,?)",
                            (claim["session"],q["taskId"],str(uuid.uuid4()),claim["session"],raw,json.dumps(events),
                             q["scopePlanning"]["baseSha"],claim["requestHash"]))
            journal.execute("insert into leases values(?,?,'Discuss','owner','released',?,?,NULL,?)",
                            (claim["session"],q["taskId"],claim["session"],hashlib.sha256(raw.encode()).hexdigest(),claim["requestHash"]))

    def test_discuss_before_start_preserves_owner_and_operator_answers(self):
        view = self.invoke("discussion")
        self.assertIsNone(view["taskId"])
        self.assertTrue(view["ownerAvailable"])
        selection = {k:view[k] for k in ("repositoryId","issueId","issueHash","baseSha","policyHash")}
        message = dict(**selection, id=str(uuid.uuid4()), previous=None, text="Keep the shipping rule inclusive.")
        pending = self.invoke("message",body=message)
        self.assertEqual(pending,self.invoke("message",body=message))
        self.assertFalse(pending["canBegin"])
        self.assertEqual(pending,self.invoke("transcript"))
        self.assertEqual(self.invoke("tasks"), [])
        self.assertEqual(self.invoke("queue"), [])
        self.conflict("message",body={**message,"id":str(uuid.uuid4()),"previous":message["id"]})
        claim = self.invoke("ownerClaim")
        self.assertEqual(claim["workerMode"],"codex-subscription")
        selected = self.invoke("preview")
        proposal = dict(goal="Respect the approved shipping boundary.",paths=selected["paths"],checks=selected["checks"])
        result = dict(requestHash=claim["requestHash"],reply="I will test the exact threshold.",proposal=proposal)
        send = dict(id=claim["id"],token=claim["token"],result=result)
        self.conflict("receive",body=send)
        self.seal_owner(claim,result)
        self.assertTrue(self.invoke("receive",body=send))
        approved = self.invoke("discussion")
        self.assertTrue(approved["canBegin"])
        self.assertEqual(approved["proposal"],proposal)
        start = dict(repositoryId=view["repositoryId"],issueId=view["issueId"],scopeHash=view["issueHash"],scopeApproval=approved["scopeApproval"])
        self.git.issues[5]["title"] += " changed"
        self.assertTrue(self.invoke("discussion")["stale"])
        self.conflict("start",body=start)
        self.git.issues[5]["title"] = self.git.issues[5]["title"].removesuffix(" changed")
        task_id = self.invoke("start",body=start)
        self.assertEqual(task_id,pending["taskId"])
        task, = self.invoke("tasks")
        self.assertEqual(task["ownerId"],pending["ownerId"])
        self.assertEqual(task["goal"],proposal["goal"])
        self.assertIn(message["text"],str(task["memory"]["briefs"][-1]["requirements"]["constraints"]))
        self.assertEqual(self.invoke("start",body=start),task_id)
        plan = self.invoke("planningClaim")
        self.assertEqual(plan["context"]["ownerId"],pending["ownerId"])
        self.assertIn(message["text"],str(plan["context"]["brief"]["requirements"]["constraints"]))
        self.assertEqual(self.git.writes,[])

    def test_old_manual_approval_cannot_bypass_owner_clarification(self):
        view = self.invoke("preview")
        pins = {k:view[k] for k in ("repositoryId","issueId","issueHash","baseSha","policyHash")}
        manual = dict(**pins,goal="Use the earlier manual objective.",paths=view["paths"],checks=view["checks"],previous=None)
        old = self.invoke("approve",body=manual)
        self.assertTrue(old["approvedForExecution"])
        msg = dict(**pins,id=str(uuid.uuid4()),previous=None,text="Wait. Which choice matches the requirement?")
        self.invoke("message",body=msg)
        claim = self.invoke("ownerClaim")
        result = dict(requestHash=claim["requestHash"],reply="Please tell me which boundary to use.",proposal=None)
        self.seal_owner(claim,result)
        self.invoke("receive",body=dict(id=claim["id"],token=claim["token"],result=result))
        replay = self.invoke("approve",body=manual)
        self.assertIn("conflict",replay)
        draft = self.invoke("approve",body={**manual,"previous":old["id"]})
        self.assertFalse(draft["approvedForExecution"])
        self.assertFalse(self.invoke("discussion")["canBegin"])
        preview = self.invoke("preview")
        self.assertTrue(preview["discussing"])
        self.assertFalse(preview["canBegin"])
        for approval in [old,draft]:
            self.conflict("start",body=dict(repositoryId=view["repositoryId"],issueId=view["issueId"],
                                           scopeHash=view["issueHash"],scopeApproval=approval["id"]))
        self.assertEqual(self.invoke("tasks"),[])

    def test_request_broader_scope_is_bound_to_owner_turn(self):
        view = self.invoke("discussion",7)
        pins = {k:view[k] for k in ("repositoryId","issueId","issueHash","baseSha","policyHash")}
        msg = dict(**pins,id=str(uuid.uuid4()),previous=None,text="Discuss layout behavior.")
        self.invoke("message",7,msg)
        claim = self.invoke("ownerClaim",7)
        proposal = dict(goal="Improve layout.",paths=["src/app.tsx"],checks=["Show a progress spinner."])
        result = dict(requestHash=claim["requestHash"],reply="This needs a broader scope.",proposal=proposal)
        self.seal_owner(claim,result)
        self.assertTrue(self.invoke("receive",7,dict(id=claim["id"],token=claim["token"],result=result)))
        view = self.invoke("discussion",7)
        self.assertEqual(view["proposal"],proposal)
        self.assertFalse(view["canBegin"])
        request = dict(**pins,**proposal,previous=view["savedScopeId"],ownerMessage=msg["id"])
        self.conflict("approve",7,{**request,"goal":"Change the proposal."})
        saved = self.invoke("approve",7,request)
        self.assertFalse(saved["approvedForExecution"])
        recorded = self.invoke("discussion",7)
        self.assertEqual(saved["id"],recorded["savedScopeId"])
        self.assertTrue(recorded["requestRecorded"])
        self.conflict("start",7,dict(repositoryId=view["repositoryId"],issueId=view["issueId"],scopeHash=view["issueHash"],scopeApproval=saved["id"]))
        self.invoke("message",7,{**msg,"id":str(uuid.uuid4()),"previous":msg["id"],"text":"Use a simpler change."})
        self.conflict("approve",7,request)
        self.assertFalse(self.invoke("discussion",7)["requestRecorded"])
        self.assertEqual(self.invoke("queue"),[])

    def test_other_issue_cannot_inherit_or_start_with_copied_policy(self):
        assigned = self.invoke("preview", 5)
        self.git.issues[7].update(title="Reduce scrolling", body="- Keep the latest message visible.")
        view = self.invoke("preview", 7)
        self.assertFalse(view["assigned"])
        self.assertEqual((view["paths"], view["checks"]), ([], []))
        request = {k:view[k] for k in ("repositoryId","issueId","issueHash","baseSha","policyHash")}
        draft = self.invoke("approve",7,{**request,"goal":view["title"],"paths":[],"checks":["Keep the latest message visible."]})
        self.assertEqual(self.invoke("preview",7)["saved"], draft)
        saved = self.invoke("approve",7,{**request,"previous":draft["id"],"goal":view["title"],
                                         "paths":assigned["paths"],"checks":assigned["checks"]})
        start = dict(repositoryId=view["repositoryId"], issueId=view["issueId"], scopeHash=view["issueHash"], scopeApproval=saved["id"])
        for field, wrong in (("number",8), ("issueId",1005), ("issueHash","0"*64), ("policyHash","0"*64)):
            binding = dict(number=7,issueId=view["issueId"],issueHash=view["issueHash"],policyHash=view["policyHash"])
            self.bindings = [{**binding,field:wrong}]
            self.assertFalse(self.invoke("preview",7)["canBegin"])
            self.conflict("start",7,start)
        self.bindings = [binding]
        self.assertFalse(self.invoke("preview",7)["canBegin"])
        self.conflict("start",7,start)
        self.bindings = []
        self.conflict("start",7,start)
        self.assertEqual(self.invoke("tasks"), [])
        self.assertEqual(self.git.writes, [])

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
        self.assertIn(request["goal"],task["memory"]["briefs"][-1]["requirements"]["constraints"])
        self.assertNotIn("Apply the installed verification criteria.",task["memory"]["briefs"][-1]["requirements"]["constraints"])
        self.assertEqual(task["phase"],"Planning")
        self.assertEqual(task["followUp"]["taskId"],task_id)
        self.assertEqual(json.loads(next(self.approvals.glob("*.json")).read_text()),task["followUp"])
        self.assertEqual([e["actor"] for e in task["history"]].count("Controller"),1)
        self.assertEqual(self.git.writes,[])
