"""FCT011 qualification. All provider reads and writes target a loopback fixture."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from threading import Event
from urllib.error import HTTPError
import hashlib
import json
import os
import sqlite3
import subprocess
import time
import tempfile
import unittest

from github_fixture import GitHubFixture
import test_slice


class GitHubTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gh = GitHubFixture(tmp.name, test_slice.ROOT / "fixtures/shipping/repository")
        self.addCleanup(self.gh.close)
        self.profile = Path(tmp.name) / "profile.json"
        self.profile.write_text(json.dumps({"apiUrl":self.gh.url, "repository":self.gh.repo, "repositoryId":self.gh.repo_id,
                                            "baseBranch":"main", "issues":[self.gh.add_issue(n) for n in (1, 2)]}))
        self.controller = test_slice.FactoryTest()
        self.controller.github_profile = self.profile
        self.addCleanup(self.controller.doCleanups)
        self.controller.setUp()

    def poll(self, n=1):
        try:
            return self.controller.api(f"github/issues/{n}/sync", {})
        except HTTPError as error:
            error.add_note(f"Controller: {error.read().decode()}. Fixture: {self.gh.error}")
            error.close()
            raise

    def ready(self):
        tid = self.poll()["taskId"]
        return self.controller.until(tid, lambda t: t["activity"] == "Delivered")

    def enable_issue_actions(self):
        from unittest.mock import patch
        env = patch.dict(os.environ, FACTORY_FOLLOWUP_APPROVALS=str(self.profile.parent))
        env.start()
        self.addCleanup(env.stop)
        config = json.loads(self.profile.read_text())
        config["issueActionBot"] = "fixture[bot]"
        self.profile.write_text(json.dumps(config))
        self.controller.stop()
        self.controller.start(manual=True)

    def test_operator_followup_and_finish(self):
        self.enable_issue_actions()
        app, gh = self.controller, self.gh
        route = "github/issues/1/actions"
        original = self.ready()
        tid = original["id"]
        first = self.poll()["draft"]
        previous, = gh.pulls
        if os.getenv("FACTORY_BROWSER_TEST") == "1":
            subprocess.run(["node","browser-issue-actions.mjs",app.url,gh.base], cwd=test_slice.ROOT/"src/Factory.Web",
                           timeout=60,check=True,env=dict(os.environ,FACTORY_LOCAL_TOKEN=app.token))
        command = {"taskId":tid, "headSha":first["headSha"], "baseSha":gh.base, "scope":"Check the exact shipping boundary again."}
        def rejected(path, data, code):
            with self.assertRaises(HTTPError) as caught:
                app.api(route+"/"+path,data)
            self.assertEqual(caught.exception.code, code, caught.exception.read())
            caught.exception.close()
        rejected("finish", {**command,"scope":""},409)
        rejected("follow-up",{**command,"baseSha":"f"*40},409)
        self.assertEqual(gh.comments, [])
        gh.drop_comment = True
        rejected("follow-up",command,502)
        app.stop()
        app.start(manual=True)
        result = app.api(route+"/follow-up",command)
        self.assertEqual(result["stage"], "started")
        self.assertEqual(result, app.api(route+"/follow-up",command))
        self.assertEqual(len(gh.comments),1)
        current, = app.api("tasks")
        self.assertEqual((current["id"],current["ownerId"],current["phase"],current["goal"]),
                         (tid,original["ownerId"],"Planning",command["scope"]))
        self.assertEqual(current["memory"]["briefs"][:-1],original["memory"]["briefs"])
        again = app.until(tid,lambda t:t["activity"]=="Delivered")
        self.assertNotEqual(again["review"]["runId"],original["review"]["runId"])
        final = self.poll()["draft"]
        self.assertNotEqual(first["headSha"], final["headSha"])
        last = gh.pulls[-1]
        last.update(state="closed",merged=True)
        gh.git("update-ref","refs/heads/main",final["headSha"],gh.base)
        finish = {**command, "scope":"", "headSha":final["headSha"],"baseSha":final["headSha"]}
        gh.drop_close = True
        rejected("finish",finish,502)
        app.stop()
        app.start(manual=True)
        result = app.api(route+"/finish",finish)
        self.assertEqual(result["stage"],"finished")
        self.assertEqual(result,app.api(route+"/finish",finish))
        self.assertEqual(len(gh.comments),2)
        for term in ("PR #20","PR #21","independent reviewer"):
            self.assertIn(term,gh.comments[-1]["body"])
        self.assertEqual(gh.writes.count("close-issue"),1)
        self.assertEqual(gh.writes.count("delete-ref"),1)
        self.assertEqual(gh.git("rev-parse","refs/heads/"+previous["head"]["ref"]),first["headSha"])
        self.assertEqual(gh.git("rev-parse","refs/heads/main"),final["headSha"])
        self.assertEqual(app.api(f"tasks/{tid}/workspace")["issueAction"]["stage"],"finished")
        self.assertIsNone(gh.error)

    def test_finish_keeps_protected_and_moved_refs(self):
        self.enable_issue_actions()
        app, gh = self.controller, self.gh
        for issue in (1,2):
            task = self.poll(issue)["taskId"]
            app.until(task,lambda t:t["activity"]=="Delivered")
            receipt = self.poll(issue)["draft"]
            pr = gh.pulls[-1]
            pr.update(state="closed",merged=True)
            branch = pr["head"]["ref"]
            ref = "refs/heads/"+branch
            if issue==1:
                gh.protected.add(branch)
            else:
                gh.git("update-ref",ref,gh.base,receipt["headSha"])
            if issue==1 and os.getenv("FACTORY_BROWSER_TEST") == "1":
                subprocess.run(["node","browser-issue-actions.mjs",app.url,gh.base,"finish"],cwd=test_slice.ROOT/"src/Factory.Web",
                               timeout=60,check=True,env=dict(os.environ,FACTORY_LOCAL_TOKEN=app.token))
            result = app.api(f"github/issues/{issue}/actions/finish",
                             {"taskId":task,"headSha":receipt["headSha"],"baseSha":gh.base,"scope":""})
            self.assertEqual(result["stage"],"finished")
            self.assertEqual(gh.git("rev-parse",ref),receipt["headSha"] if issue==1 else gh.base)
            self.assertIn("Kept "+branch,app.get(task)["history"][-1]["text"])
        self.assertNotIn("delete-ref",gh.writes)

    def test_merged_followup_uses_approved_base(self):
        app, gh = self.controller, self.gh
        task = self.ready()
        receipt = self.poll()["draft"]
        route = "github/issues/1/actions"
        query = "?taskId="+task["id"]+"&headSha="+receipt["headSha"]
        with self.assertRaises(HTTPError) as disabled:
            app.api(route+query)
        self.assertEqual(disabled.exception.code,409)
        disabled.exception.close()
        self.enable_issue_actions()
        pr, = gh.pulls
        pr.update(merged=True,state="closed")
        gh.git("update-ref","refs/heads/main",receipt["headSha"],gh.base)
        preview = app.api(route+query)
        self.assertTrue(preview["merged"])
        self.assertEqual(preview["baseSha"],receipt["headSha"])
        command = {"taskId":task["id"],"headSha":receipt["headSha"],"baseSha":preview["baseSha"],"scope":"Check the merged rule."}
        # Persist a comment before the response is lost. Advance main before admission.
        gh.drop_comment = True
        with self.assertRaises(HTTPError) as lost:
            app.api(route+"/follow-up",command)
        lost.exception.close()
        next_base = gh.git("commit-tree",gh.git("rev-parse","main^{tree}"),"-p",preview["baseSha"],"-m","Another accepted revision")
        gh.git("update-ref","refs/heads/main",next_base,preview["baseSha"])
        with self.assertRaises(HTTPError) as stale:
            app.api(route+"/follow-up",command)
        self.assertEqual(stale.exception.code,409)
        stale.exception.close()
        self.assertEqual(app.api(route+"/withdraw",{**command,"actionId":app.api(route+query)["state"]["id"]})["stage"],"withdrawn")
        if os.getenv("FACTORY_BROWSER_TEST") == "1":
            subprocess.run(["node","browser-issue-actions.mjs",app.url,next_base,"start",command["scope"]],
                           cwd=test_slice.ROOT/"src/Factory.Web",timeout=60,check=True,env=dict(os.environ,FACTORY_LOCAL_TOKEN=app.token))
        result = app.api(route+"/follow-up",{**command,"baseSha":next_base})
        self.assertEqual(result["stage"],"started")
        current, = app.api("tasks")
        self.assertEqual(current["id"],task["id"])
        self.assertEqual(current["followUp"]["baseSha"],next_base)
        self.assertEqual(current["source"]["source"],task["candidate"])
        self.assertEqual(len(gh.comments),2)
        self.assertEqual(self.poll()["origin"]["baseSha"],next_base)
        # The trusted adapter must reject both a modified and a missing grant.
        app.until(task["id"],lambda t:t["phase"]=="Implementation")
        with closing(sqlite3.connect(Path(app.folder)/"slice.db")) as db:
            orders = [json.loads(r[0]) for r in db.execute("SELECT request FROM dispatch_outbox")]
        order = next(o for o in orders if (o.get("context") or {}).get("followUp"))
        def worker(packet):
            return subprocess.run(["dotnet",str(test_slice.DLL),"--fixture-worker"],input=json.dumps(packet),
                                  text=True,capture_output=True,timeout=10)
        self.assertEqual(worker(order).returncode,0)
        forged = deepcopy(order)
        forged["context"]["followUp"]["baseSha"]=gh.base
        self.assertNotEqual(worker(forged).returncode,0)
        grant = self.profile.parent / (task["id"].replace("-","")+"-"+result["id"].replace("-","")+".json")
        grant.unlink()
        self.assertNotEqual(worker(order).returncode,0)

    def test_issue_waits_for_operator_start(self):
        profile = json.loads(self.profile.read_text())
        profile["operatorStart"] = True
        self.profile.write_text(json.dumps(profile))
        app = self.controller
        app.stop()
        app.start(manual=False)
        intake = app.api("dashboard/scenarios")["githubIntake"]
        issue = intake["issues"][0]
        selection = {"repositoryId":intake["repositoryId"], "issueId":issue["id"], "scopeHash":issue["scopeHash"]}
        route = f"github/issues/{issue['number']}/start"
        self.assertIsNone(self.poll()["taskId"])
        self.assertEqual(app.api("tasks"), [])
        app.stop()
        app.start(manual=True)
        self.assertIsNone(self.poll()["taskId"])
        for path, body, token, expected in [
            (route, selection, "foreign", 401), ("github/issues/33/start", selection, app.token, 409),
            (route, {**selection, "scopeHash":"old"}, app.token, 409),
            (route, {**selection, "repositoryId":99}, app.token, 409),
            ("tasks", {"id":"aaaa0000-0000-0000-0000-000000000001","scenario":"known-decision"}, app.token, 400)
        ]:
            with self.assertRaises(HTTPError) as error:
                app.api(path, body, token)
            self.assertEqual(error.exception.code, expected)
            error.exception.close()
        self.gh.issues[1]["state"] = "closed"
        with self.assertRaises(HTTPError) as error:
            app.api(route, selection)
        self.assertIn("closed", error.exception.read().decode())
        error.exception.close()
        self.assertEqual(app.api("tasks"), [])
        self.gh.issues[1]["state"] = "open"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _:app.api(route, selection), range(2)))
        self.assertEqual(results[0], results[1])
        tid = results[0]["taskId"]
        app.stop()
        app.start(manual=True)
        self.assertEqual(app.api(route, selection)["taskId"], tid)
        self.assertEqual(self.poll()["taskId"], tid)
        task, = app.api("tasks")
        self.assertEqual(task["phase"], "Planning")
        self.assertEqual(sum(entry["text"].startswith("Requested start of ") for entry in task["history"]), 1)
        self.assertFalse(self.gh.writes)
        if os.getenv("FACTORY_BROWSER_TEST") == "1":
            subprocess.run(["node", "browser-issue-start.mjs", app.url, self.gh.repo, "2"],
                           cwd=test_slice.ROOT/"src/Factory.Web", timeout=60, check=True,
                           env=dict(os.environ, FACTORY_LOCAL_TOKEN=app.token))
            self.assertEqual(len(app.api("tasks")), 2)

    def test_eligibility_and_duplicate_intake(self):
        with self.assertRaises(HTTPError) as error:
            self.poll(3)
        self.assertEqual(error.exception.code, 409)
        with self.assertRaises(HTTPError) as unauthorized:
            self.controller.api("github/issues/1/sync", {}, token="invalid")
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()
        gh = self.gh
        original = deepcopy(gh.issues[1])
        for change, reason in [({"labels":[{"name":"bug"},{"name":"factory:hold"}]}, "held"),
                               ({"labels":[{"name":"factory:covered"}]}, "covered"),
                               ({"state":"closed"}, "closed"), ({"labels":[]}, "eligible bug"),
                               ({"body":"Change pricing policy instead"}, "scope"),
                               ({"id":9556}, "identity"), ({"pull_request":{}}, "identity")]:
            gh.issues[1] = {**original, **change}
            observed = self.poll()
            self.assertIn(reason, observed["observation"]["reason"])
            self.assertIsNone(observed["taskId"])
        self.assertEqual(self.controller.api("tasks"), [])
        gh.issues[1] = original

        def simultaneous(_):
            try:
                return self.poll()["taskId"]
            except HTTPError as e:
                if e.code != 409:
                    raise
                return self.poll()["taskId"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(simultaneous, range(2)))
        self.assertEqual(ids[0], ids[1])
        task, = self.controller.api("tasks")
        self.assertIn(original["body"], task["openingMessage"])
        self.assertEqual(self.poll()["origin"]["baseSha"], gh.base)
        # A notification carries no issue authority. Even an obsolete payload polls the current issue.
        gh.issues[1]["labels"].append({"name":"factory:hold"})
        result = self.controller.api("github/issues/1/sync", {"state":"open", "labels":["bug"], "body":"old event"})
        self.assertIn("held", result["observation"]["reason"])
        self.assertEqual(result["taskId"], task["id"])
        self.assertFalse(gh.writes)
        self.assertFalse(self.controller.api("step", {})["advanced"])
        gh.issues[1]["labels"] = [{"name":"bug"}]
        gh.issues[1]["updated_at"] = "2001-01-01T00:00:00Z"
        with self.assertRaises(HTTPError) as stale:
            self.poll()
        self.assertEqual(stale.exception.code, 409)
        self.assertFalse(self.controller.api("step", {})["advanced"])
        gh.issues[1]["updated_at"] = result["observation"]["updatedAt"]
        self.assertEqual(self.poll()["taskId"], task["id"])
        self.assertTrue(self.controller.api("step", {})["advanced"])

    def test_lost_publication_responses(self):
        task = self.ready()
        gh = self.gh
        gh.drop_ref = True
        with self.assertRaises(HTTPError) as first:
            self.poll()
        self.assertEqual(first.exception.code, 502)
        self.assertEqual(len(gh.pulls), 0)
        gh.drop_pr = True
        with self.assertRaises(HTTPError) as second:
            self.poll()
        self.assertEqual(second.exception.code, 502)
        self.assertEqual(gh.writes.count("pulls"), 1)
        self.controller.stop()
        self.controller.start(manual=True)
        gh.hide_pulls = True
        with self.assertRaises(HTTPError) as unknown:
            self.poll()
        self.assertEqual(unknown.exception.code, 409)
        repository = self.controller.api(f"tasks/{task['id']}/workspace")["repository"]
        self.assertTrue(repository["requiresReconciliation"])
        self.assertFalse(repository["awaitingConfirmation"])
        self.controller.stop()
        self.controller.start(manual=True)
        self.assertTrue(self.controller.api(f"tasks/{task['id']}/workspace")["repository"]["requiresReconciliation"])
        gh.hide_pulls = False
        pr, = gh.pulls
        self.assertEqual(pr["title"], "Reviewed fixture change for issue #1")
        summary = self.controller.api(f"tasks/{task['id']}/workspace")["summary"]
        self.assertEqual(summary["issue"], "Boundary correction 1")
        self.assertEqual(summary["request"], gh.issues[1]["body"])
        self.assertIn("The factory changed shipping-rule.json.", summary["work"])
        self.assertTrue(any("greater" in line for line in summary["work"]))
        self.assertIn("The independent reviewer approved these changes.", summary["checks"])
        for heading in ("## Issue", "## Work done", "## Checks and review", "## Next action"):
            self.assertLess(pr["body"].index(heading), pr["body"].index("<details>"))
        self.assertIn(summary["request"], pr["body"])
        self.assertIn(summary["work"][0], pr["body"])
        self.assertIn("Controlled fixture evidence. Local GitHub fixture.",pr["body"])
        self.assertNotIn("Codex",pr["body"])
        original = deepcopy(pr)
        for key, value in [("draft", False), ("state", "closed"), ("body", "different marker"),
                           ("head", {**pr["head"], "sha":gh.base}), ("base", {**pr["base"],"repo":{"id":42}})]:
            pr[key] = value
            with self.assertRaises(HTTPError) as conflict:
                self.poll()
            self.assertEqual(conflict.exception.code, 409)
            pr[key] = original[key]
        # The issue may have been held after POST committed. Recover the receipt without another provider write.
        gh.issues[1]["labels"].append({"name":"factory:hold"})
        if os.getenv("FACTORY_BROWSER_TEST") == "1":
            subprocess.run(["node","browser-github.mjs",self.controller.url,pr["html_url"],str(pr["number"]),"reconcile"],
                           cwd=test_slice.ROOT/"src/Factory.Web",timeout=60,check=True,
                           env=dict(os.environ,FACTORY_LOCAL_TOKEN=self.controller.token))
        ack = self.poll()["draft"]
        self.assertEqual(ack, self.poll()["draft"])
        self.assertFalse(self.controller.api(f"tasks/{task['id']}/workspace")["repository"]["requiresReconciliation"])
        self.assertEqual(ack["headSha"], pr["head"]["sha"])
        self.assertEqual(gh.writes.count("git/refs"), 1)
        self.assertEqual(gh.writes.count("pulls"), 1)
        self.assertEqual(gh.git("rev-parse", "refs/heads/main"), gh.base)
        self.assertEqual(gh.git("rev-parse", ack["headSha"] + "^"), gh.base)
        for evidence in (task["receipts"][-1]["runId"], task["review"]["runId"], gh.base,
                         hashlib.sha256(task["candidate"].encode()).hexdigest(), "Hosted checks and human review pending"):
            self.assertIn(evidence, pr["body"])
        self.assertEqual(gh.git("diff", "--name-only", gh.base, ack["headSha"]), "shipping-rule.json")
        self.assertEqual(gh.git("rev-parse", ack["headSha"] + ":shipping-rule.json"),
                         gh.git("hash-object", "--stdin", data=task["candidate"].encode()))
        self.assertEqual(gh.git("for-each-ref", "--format=%(refname)", "refs/heads/factory").count("refs/heads/"), 1)
        self.assertEqual(sum(e["actor"] == "GitHub publisher" for e in self.controller.get(task["id"])["history"]), 1)
        self.assertIsNone(gh.error)

    def atomic_crash(self, after_commit):
        profile = json.loads(self.profile.read_text())
        profile["localAtomicDraft"] = True
        self.profile.write_text(json.dumps(profile))
        self.controller.stop()
        self.controller.start(manual=True)
        task = self.ready()
        reached, resume = Event(), Event()
        original = self.gh.atomic_draft
        def observe(method, key, obj):
            result = original(method,key,obj)
            if method==("POST" if after_commit else "GET"):
                reached.set()
                resume.wait(15)
            return result
        self.gh.atomic_draft = observe
        with ThreadPoolExecutor(max_workers=1) as pool:
            attempt = pool.submit(self.poll)
            try:
                self.assertTrue(reached.wait(15))
                with closing(sqlite3.connect(Path(self.controller.folder)/"slice.db")) as db:
                    row = db.execute("SELECT head_sha,pr_attempted,receipt FROM github_publications WHERE task_id=?",
                                     (task["id"],)).fetchone()
                head, reserved, receipt = row
                self.assertEqual((reserved,receipt),(1,None))
                self.controller.stop()
            finally:
                resume.set()
            with self.assertRaises((ConnectionError,OSError)):
                attempt.result()
        self.gh.atomic_draft = original
        self.assertEqual(len(self.gh.pulls),int(after_commit))
        self.controller.start(manual=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _:self.poll()["draft"],range(2)))
        self.assertEqual(receipts[0],receipts[1])
        self.assertEqual(receipts[0]["headSha"],head)
        pr, = self.gh.pulls
        self.assertEqual(self.gh.git("rev-parse","refs/heads/"+pr["head"]["ref"]),head)
        self.assertEqual(self.gh.git("rev-parse","refs/heads/main"),self.gh.base)
        self.assertEqual(self.gh.writes.count("atomic-draft"),1)
        self.assertEqual(self.poll()["draft"],receipts[0])

    def test_journal_epoch_changed_after_lookup(self):
        profile = json.loads(self.profile.read_text())
        profile["localAtomicDraft"] = True
        self.profile.write_text(json.dumps(profile))
        self.controller.stop()
        self.controller.start(manual=True)
        task = self.ready()
        original = self.gh.atomic_draft
        def change_epoch(method, key, obj):
            if method == "POST":
                self.gh.git("update-ref","refs/factory/journal",self.gh.blob(b"1"*32),obj["journalOid"])
            return original(method,key,obj)
        self.gh.atomic_draft = change_epoch
        with self.assertRaises(HTTPError):
            self.poll()
        with self.assertRaises(HTTPError):
            self.poll()
        self.assertEqual(self.gh.pulls, [])
        self.assertEqual(self.gh.git("for-each-ref","refs/heads/factory"), "")
        self.assertIsNone(self.controller.api(f"tasks/{task['id']}/workspace")["repository"]["draft"])

    def test_atomic_crash_before_transmission(self):
        self.atomic_crash(False)

    def test_atomic_crash_after_remote_commit(self):
        self.atomic_crash(True)

    def test_background_publication_and_desk(self):
        task = self.ready()
        profile = json.loads(self.profile.read_text())
        profile["issues"] = profile["issues"][:1]
        self.profile.write_text(json.dumps(profile))
        self.controller.stop()
        self.controller.start(manual=False)
        for _ in range(100):
            workspace = self.controller.api(f"tasks/{task['id']}/workspace")
            if workspace["repository"]["draft"]:
                break
            time.sleep(.1)
        draft = workspace["repository"]["draft"]
        self.assertIsNotNone(draft)
        self.assertEqual(workspace["draftNumber"], draft["number"])
        self.assertEqual(len(self.gh.pulls), 1)
        if os.getenv("FACTORY_BROWSER_TEST") == "1":
            subprocess.run(["node", "browser-github.mjs", self.controller.url, draft["url"], str(draft["number"])],
                           cwd=test_slice.ROOT / "src/Factory.Web", timeout=60, check=True,
                           env=dict(os.environ, FACTORY_LOCAL_TOKEN=self.controller.token))

    def test_gate_and_scope_block_writes(self):
        task = self.ready()
        original = deepcopy(task)
        task["candidate"] = "{}"
        self.controller.save_checkpoint(task)
        with self.assertRaises(HTTPError) as invalid:
            self.poll()
        self.assertEqual(invalid.exception.code, 409)
        changed_review = deepcopy(original)
        changed_review["review"]["evidence"]["findings"] = []
        changed_review["review"]["approved"] = False
        self.controller.save_checkpoint(changed_review)
        with self.assertRaises(HTTPError) as rejected_review:
            self.poll()
        self.assertEqual(rejected_review.exception.code, 409)
        self.controller.save_checkpoint(original)
        self.gh.issues[1]["body"] = "Material scope edit"
        self.assertIn("scope", self.poll()["observation"]["reason"])
        self.assertEqual(self.gh.writes, [])

    def test_branch_conflict_and_source_drift(self):
        task = self.ready()
        gh = self.gh
        branch = "refs/heads/factory/" + task["id"].replace("-", "")
        gh.git("update-ref", branch, gh.base)
        with self.assertRaises(HTTPError) as conflict:
            self.poll()
        self.assertEqual(conflict.exception.code, 409)
        self.assertEqual(gh.git("rev-parse", branch), gh.base)
        self.assertEqual(gh.writes.count("pulls"), 0)
        # Even if someone independently sets that branch to the exact candidate it is still unowned.
        with closing(sqlite3.connect(Path(self.controller.folder) / "slice.db")) as db:
            head, = db.execute("SELECT head_sha FROM github_publications WHERE task_id=?", (task["id"],)).fetchone()
        gh.git("update-ref", branch, head, gh.base)
        with self.assertRaises(HTTPError) as unowned:
            self.poll()
        self.assertEqual(unowned.exception.code, 409)
        self.assertEqual(gh.writes.count("pulls"), 0)
        tree = gh.tree({**gh.files, "unqualified-file":gh.blob(b"extra")})
        advanced = gh.git("commit-tree",tree,"-p",gh.base,"-m","advance")
        gh.git("update-ref", "refs/heads/main", advanced)
        with self.assertRaises(HTTPError) as stale:
            self.poll()
        self.assertEqual(stale.exception.code, 409)
        self.assertIn("fixture tree", self.poll(2)["observation"]["reason"])
        self.assertEqual(len(self.controller.api("tasks")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
