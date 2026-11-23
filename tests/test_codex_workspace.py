"""Real Docker and Git bytes, exercised by a scripted tool client."""
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from blueprint_tools import FACTORY_FILES
from codex_workspace import open_workspace
from environment_leases import Leases, fingerprint


class WorkspaceTest(unittest.TestCase):
    def test_digest_qualified_worker_when_short_reference_is_missing(self):
        from unittest.mock import patch
        from environment_leases import ENGINE_IMAGE, IMAGE, output
        with tempfile.TemporaryDirectory() as tmp:
            leases = Leases(Path(tmp)/"leases.sqlite")
            engine = leases.docker
            def docker(*args, **kwargs):
                if IMAGE in args:
                    return subprocess.CompletedProcess(args,1,b"",b"No such image")
                self.assertNotIn("pull",args)
                return engine(*args,**kwargs)
            with patch.object(leases,"docker",side_effect=docker):
                generation = leases.acquire(str(uuid4()),str(uuid4()),"verifier","Verify","test",{"example.txt":"pinned"})
                try:
                    row = leases.record(generation)
                    info = leases.owned(row)
                    self.assertEqual(row["image"], IMAGE)
                    self.assertEqual(info["Config"]["Image"], ENGINE_IMAGE)
                    self.assertEqual(info["Image"], output(engine("image","inspect",ENGINE_IMAGE,"--format","{{.Id}}")))
                    self.assertEqual(output(leases.execute(generation,"process.stdout.write(require('fs').readFileSync('/source/example.txt'))")), "pinned")
                finally:
                    leases.release(generation)

    def test_owner_discussion_reads_only_its_checkpoint(self):
        path = "owner/accepted.json"
        facts = json.dumps(["Use the inclusive comparison."])
        order = {"action":"Discuss","assignment":{"taskId":str(uuid4()),"sourceHash":"source"},
                 "discussion":{"evidence":{"path":path,"digest":fingerprint(facts),"content":facts}}}
        with tempfile.TemporaryDirectory() as tmp:
            leases = Leases(Path(tmp)/"leases.sqlite")
            with open_workspace(leases,order,None,expert_session=str(uuid4()),request_hash="test-request") as workspace:
                self.assertEqual([t["name"] for t in workspace.tools], ["read_file"])
                self.assertEqual(workspace.call("read_file", {"path":path,"view":"base"})["sha256"], fingerprint(facts))
                with self.assertRaises(ValueError):
                    workspace.call("read_file", {"path":"another/task.json","view":"base"})
                with self.assertRaises(ValueError):
                    workspace.call("replace_text", {"path":path})
                row = leases.record(workspace.generation)
                self.assertEqual((row["role"],row["operation"]), ("owner","Discuss"))
            self.assertEqual(leases.record(workspace.generation)["state"], "released")

    def test_configured_checkout_layout_and_suite(self):
        from copy import deepcopy
        import time
        from blueprint_tools import lease_sources
        from run_ci import execute
        manifest = (ROOT / "fixtures/checkout-scope/factory-policy.json").read_text()
        rules = json.loads(manifest)
        layout, policy = rules["repositoryLayout"], rules["policy"]
        base = rules["settings"]["base"]
        def contents(path):
            return subprocess.check_output(["git","show",base+":"+path],cwd=ROOT).decode("utf-8")
        source = {key:contents(layout[key]) for key in ("source","decisions","cases")}
        extras = {path:contents(path) for path in layout["additionalReads"]}
        source["additionalFiles"] = json.dumps(extras)
        documents = [{"part":part,"path":path,"digest":fingerprint(contents(path)),"revision":"scoped-test"}
                     for part,path in [("Source",layout["source"]),("Decisions",layout["decisions"]),
                                       ("Cases",layout["cases"]), *(("Cases",p) for p in extras)]]
        task = str(uuid4())
        a = {"policyVersion":policy["version"],"runId":str(uuid4()),"taskId":task,"sourceHash":"scoped-test",
             "requiredChecks":policy["requiredChecks"]}
        grant = {"tools":rules["blueprint"]["tools"]["Implement"],"manifestHash":fingerprint(manifest)}
        order = {"source":source,"action":"Implement","assignment":a,"context":{"documents":documents,"blueprint":grant}}
        with tempfile.TemporaryDirectory() as tmp:
            leases = Leases(Path(tmp)/"leases.sqlite")
            with open_workspace(leases,order,base) as writer:
                target = layout["source"]
                self.assertEqual(set(writer.paths), {d["path"] for d in documents})
                suite = next(iter(extras))
                writer.call("read_file",{"path":suite,"view":"base"})
                old = writer.call("read_file",{"path":target,"view":"current"})
                edit = {"path":target,"before":'"greater"',"after":'"greaterOrEqual"',"sha256":old["sha256"]}
                with self.assertRaises(ValueError):
                    writer.call("replace_text",{**edit,"path":suite})
                writer.call("replace_text",edit)
                candidate = leases.snapshot(writer.generation,writer.work.split("/")[-1])
                leases.finish(writer.generation,{"scriptedTransport":True})
            order.update(action="Review",candidate=candidate)
            a.update(runId=str(uuid4()),inputHash=fingerprint(candidate))
            grant["tools"] = rules["blueprint"]["tools"]["Review"]
            with open_workspace(leases,order,base) as reviewer:
                self.assertNotEqual(reviewer.generation,writer.generation)
                self.assertEqual(reviewer.call("read_file",{"path":target,"view":"current"})["content"],candidate)
                with self.assertRaises(ValueError):
                    reviewer.call("replace_text",edit)
            grant["tools"] = rules["blueprint"]["tools"]["Verify"]
            order.update(action="Verify",answer={"value":"5000"})
            binding = {"taskId":task,"executionId":str(uuid4()),"candidateHash":fingerprint(candidate),
                       "environment":rules["environment"],"revisionKind":"git","revision":base,
                       "scriptHash":rules["scriptHash"],"policyHash":fingerprint(manifest)}
            job = {"order":order,"binding":binding,"script":contents(suite),"deadlineUnix":time.time()+90,"token":"fixture"}
            self.assertEqual([c["exitCode"] for c in execute(job,leases)["checks"]],[0,0,0])
            bad = deepcopy(job)
            bad["binding"]["revision"] = "0"*40
            with self.assertRaisesRegex(ValueError,"Git revision"):
                execute(bad,leases)
            bad = deepcopy(order)
            bad["source"]["additionalFiles"] = "{}"
            with self.assertRaisesRegex(ValueError,"Supplemental"):
                lease_sources(bad,"checks.run")
            bad = deepcopy(order)
            bad["context"]["blueprint"]["manifestHash"]="0"*64
            with self.assertRaisesRegex(ValueError,"policy"):
                lease_sources(bad,"checks.run")


    def test_two_file_sealing_review_ci_and_git_tree(self):
        from copy import deepcopy
        from github_fixture import GitHubFixture
        from blueprint_tools import candidate_files
        from run_ci import execute
        import time
        started = time.monotonic()
        manifest = (ROOT/"fixtures/checkout-scope/v2-policy.json").read_text()
        rules = json.loads(manifest)
        base = rules["settings"]["base"]
        layout = rules["repositoryLayout"]
        targets = [layout["source"],*layout["additionalWrites"]]
        with tempfile.TemporaryDirectory() as folder:
            class Checkout(GitHubFixture):
                repo = "fixture/factory"
            git = Checkout(folder,ROOT/"fixtures/shipping/repository")
            try:
                git.git("fetch","--no-tags",str(ROOT),base)
                def contents(path):
                    return subprocess.check_output(["git","--git-dir",str(git.git_dir),"show",base+":"+path]).decode()
                source = {k:contents(layout[k]) for k in ("source","decisions","cases")}
                extras = {p:contents(p) for p in layout["additionalReads"]+layout["additionalWrites"]}
                source["additionalFiles"] = json.dumps(extras)
                files = [("Source",p) for p in targets]+[("Decisions",layout["decisions"]),("Cases",layout["cases"])]
                files.extend(("Cases",p) for p in layout["additionalReads"])
                docs = [{"part":part,"path":p,"digest":fingerprint(contents(p)),"revision":"test-packet"} for part,p in files]
                a = {"policyVersion":rules["policy"]["version"],"runId":str(uuid4()),"taskId":str(uuid4()),
                     "sourceHash":"test-packet","requiredChecks":rules["policy"]["requiredChecks"]}
                grant = {"tools":rules["blueprint"]["tools"]["Implement"],"manifestHash":fingerprint(manifest)}
                order = {"action":"Implement","source":source,"assignment":a,"context":{"documents":docs,"blueprint":grant}}
                leases = Leases(Path(folder)/"leases.sqlite")
                with open_workspace(leases,order,base) as writer:
                    with self.assertRaises(ValueError):
                        writer.checkpoint()
                    for path in targets:
                        original = writer.call("read_file",{"path":path,"view":"current"})
                        writer.call("replace_text",{"path":path,"before":'"greater"',"after":'"greaterOrEqual"',
                                                    "sha256":original["sha256"]})
                    packed = writer.checkpoint()
                    leases.finish(writer.generation,{"test":"controlled"})
                accepted = candidate_files(layout,packed)
                a.update(runId=str(uuid4()),inputHash=fingerprint(packed))
                order.update(action="Review",candidate=packed)
                grant["tools"] = rules["blueprint"]["tools"]["Review"]
                with open_workspace(leases,order,base) as reviewer:
                    self.assertNotEqual(writer.generation,reviewer.generation)
                    for path in targets:
                        self.assertEqual(reviewer.call("read_file",{"path":path,"view":"base"})["content"],contents(path))
                        self.assertEqual(reviewer.call("read_file",{"path":path,"view":"current"})["content"],accepted[path])
                for invalid in ({targets[0]:accepted[targets[0]]},{**accepted,layout["decisions"]:"changed"}):
                    with self.assertRaises(ValueError):
                        candidate_files(layout,json.dumps(invalid))
                suite = layout["additionalReads"][0]
                grant["tools"] = rules["blueprint"]["tools"]["Verify"]
                order.update(action="Verify",answer={"value":"5000"})
                binding = {"taskId":a["taskId"],"executionId":str(uuid4()),"candidateHash":fingerprint(packed),
                           "environment":rules["environment"],"revisionKind":"git","revision":base,
                           "scriptHash":rules["scriptHash"],"policyHash":fingerprint(manifest)}
                job = {"order":order,"binding":binding,"script":contents(suite),"deadlineUnix":time.time()+90,"token":"fixture"}
                green = execute(job,leases)["checks"]
                self.assertEqual([c["exitCode"] for c in green],[0,0,0])
                broken = deepcopy(job)
                broken["order"]["candidate"] = json.dumps({**accepted,targets[1]:contents(targets[1])})
                broken["binding"].update(executionId=str(uuid4()),candidateHash=fingerprint(broken["order"]["candidate"]))
                broken["order"]["assignment"]["inputHash"] = broken["binding"]["candidateHash"]
                broken["deadlineUnix"] = time.time()+90
                red = execute(broken,leases)["checks"]
                self.assertEqual([c["exitCode"] for c in red],[0,1,0])
                dll = ROOT/"tests/Factory.ContractTests/bin/Debug/net10.0/Factory.ContractTests.dll"
                head = subprocess.check_output(["dotnet",dll,"--check-scoped-tree",git.url],input=packed.encode(),timeout=30).decode().strip()
                self.assertEqual(git.git("rev-parse",head+"^"),base)
                self.assertEqual(set(git.git("diff","--name-only",base,head).splitlines()),set(targets))
                for path in targets:
                    blob = subprocess.check_output(["git","--git-dir",str(git.git_dir),"cat-file","blob",head+":"+path])
                    self.assertEqual(blob,accepted[path].encode())
                self.assertEqual(git.pulls,[])
                if dest := os.getenv("FACTORY_CHECKOUT_DRILL_REPORT"):
                    with Path(dest).open("x",encoding="utf-8") as stream:
                        json.dump({"evidenceClass":"scripted repository tools, real Docker verification and local Git objects",
                                   "task":a["taskId"],"sourceGit":base,"candidateGit":head,
                                   "candidateHash":fingerprint(packed),"paths":sorted(accepted),
                                   "writerGeneration":writer.generation,"reviewerGeneration":reviewer.generation,
                                   "passing":green,"failedControl":red,"modelSessions":0,"drafts":0,
                                   "elapsedSeconds":time.monotonic()-started,"humanTaskInterventions":0},stream,indent=2)
            finally:
                git.close()

    def test_scoped_reads_edits_review_and_retirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root/"repo.git"
            subprocess.run(["git","init","--bare","-q",repo],check=True)
            def git(*args):
                return subprocess.check_output(["git","--git-dir",repo,*args])
            rules = json.loads((ROOT/"fixtures/factory-summary/factory-policy.json").read_text())
            base = rules["settings"]["base"]
            git("fetch","-q","--no-tags",str(ROOT),"HEAD")
            git("update-ref","refs/heads/test",base)
            source, docs = {}, []
            for part, (_,key,path) in FACTORY_FILES.items():
                data = git("show","test:"+path).decode()
                source[key] = data
                docs.append({"path":path,"part":part,"revision":"test-packet","digest":fingerprint(data),
                             "excerpt":data[:512],"complete":len(data)<=512})
            order = {"action":"Implement","assignment":{"policyVersion":"factory-summary/v1","runId":str(uuid4()),
                     "taskId":str(uuid4()),"sourceHash":"test-packet"},"source":source,"context":{"documents":docs,
                     "blueprint":{"tools":rules["blueprint"]["tools"]["Implement"],"manifestHash":"test-manifest"}}}
            leases = Leases(root/"leases.sqlite")
            path = FACTORY_FILES["Source"][2]
            read = {"path":path,"view":"current"}
            with open_workspace(leases,order,base) as writer:
                doc = next(d for d in docs if not d["complete"])
                env = {**os.environ,"GIT_INDEX_FILE":str(root/"index")}
                def index(*args, data=None):
                    return subprocess.check_output(["git","--git-dir",repo,*args],input=data,env=env).decode().strip()
                index("read-tree",base)
                changed = index("hash-object","-w","--stdin",data=b"advanced checkout")
                index("update-index","--cacheinfo","100644,"+changed+","+doc["path"])
                newer = index("-c","user.name=Fixture","-c","user.email=fixture@localhost","commit-tree",
                              index("write-tree"),"-p",base,"-m","Change the previously admitted file")
                git("update-ref","refs/heads/test",newer,base)
                self.assertEqual(git("show","test:"+doc["path"]),b"advanced checkout")
                full = writer.call("read_file",{"path":doc["path"],"view":"base"})
                self.assertEqual(full["content"].encode(),git("show",base+":"+doc["path"]))
                self.assertGreater(len(full["content"]),len(doc["excerpt"]))
                self.assertEqual(full["gitCommit"],base)
                original = writer.call("read_file",read)
                edit = {"path":path,"before":original["content"],"after":original["content"]+"\n// proof\n","sha256":original["sha256"]}
                writer.call("replace_text",edit)
                with self.assertRaises(RuntimeError):
                    writer.call("replace_text",edit)
                with self.assertRaises(ValueError):
                    writer.call("read_file",{"path":"../.git/config","view":"base"})
                candidate = leases.snapshot(writer.generation,writer.work.split("/")[-1])
                self.assertEqual(writer.call("read_file",read)["content"],candidate)
                def retired_during_seal():
                    with closing(sqlite3.connect(leases.db,timeout=0)) as competing:
                        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                            competing.execute("BEGIN IMMEDIATE")
                    raise RuntimeError("Retired dispatch")
                with self.assertRaisesRegex(RuntimeError,"Retired dispatch"):
                    leases.finish(writer.generation,{"output":candidate},retired_during_seal)
                self.assertIsNone(leases.record(writer.generation)["outcome_hash"])
                leases.finish(writer.generation,{"output":candidate})
            order["assignment"].update(runId=str(uuid4()),inputHash=fingerprint(candidate))
            order.update(action="Review",candidate=candidate)
            order["context"]["blueprint"]["tools"] = rules["blueprint"]["tools"]["Review"]
            with open_workspace(leases,order,base) as reviewer:
                self.assertNotEqual(reviewer.generation,writer.generation)
                self.assertEqual(reviewer.call("read_file",read)["content"],candidate)
                with self.assertRaises(ValueError):
                    reviewer.call("replace_text",edit)
                leases.release(reviewer.generation)
                with self.assertRaises(RuntimeError):
                    reviewer.call("read_file",read)
                with self.assertRaises(RuntimeError):
                    leases.finish(reviewer.generation,{"approved":True})
            order["candidate"] += "stale"
            with self.assertRaises(ValueError), open_workspace(leases,order,base):
                self.fail("Substituted candidate admitted")


if __name__ == "__main__":
    unittest.main()

