"""Prepare and exercise a local, operator-controlled factory-source Draft."""
import argparse
from contextlib import closing
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from uuid import uuid4
from zipfile import ZipFile

from github_fixture import GitHubFixture
import test_slice

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import factory_release as release
from environment_leases import Leases, IMAGE, fingerprint, output

DLL = ROOT / "tests/Factory.SelfDevelopmentFixture/bin/Debug/net10.0/Factory.SelfDevelopmentFixture.dll"
ARCHIVE = "docs/fct019/qualification.zip"
ENTRY = "task-summary.cjs"
# Candidate code runs only in a role lease. The supervisor evaluates its actual output.
RUN = """const fs=require('fs'), cp=require('child_process');
const child=cp.spawnSync(process.execPath,['/source/task-summary.cjs'],{
 input:fs.readFileSync(0),timeout:3000,maxBuffer:8192});
if (child.error || child.signal || child.status===null) process.exit(2);
process.stdout.write(child.stdout); process.exit(child.status);"""
WRITE = """const fs=require('fs');
fs.copyFileSync('/source/proposal.cjs','/work/task-summary.cjs',fs.constants.COPYFILE_EXCL);"""
REVIEW = """const fs=require('fs'), assert=require('node:assert/strict'), crypto=require('crypto');
const candidate=fs.readFileSync('/source/task-summary.cjs');
const evidence=JSON.parse(fs.readFileSync('/source/verification.json'));
const hash=crypto.createHash('sha256').update(candidate).digest('hex');
assert.equal(hash,evidence.candidateHash);
assert.ok(Object.values(evidence.checks).every(value=>value===true));
assert.throws(()=>fs.writeFileSync('/source/task-summary.cjs','x'));
process.stdout.write(hash);"""


class FactoryRepo(GitHubFixture):
    repo = "fixture/factory"
    extra_path = None

    def respond(self, method, path, query, obj):
        code, result = super().respond(method, path, query, obj)
        if method == "POST" and path == "git/trees" and self.extra_path:
            # Substitute an extra edit in a disposable tree to exercise the shared publisher's scope check.
            return super().respond(method, path, query, {"base_tree":result["sha"], "tree":[
                {"path":self.extra_path, "mode":"100644", "type":"blob", "content":"unapproved fixture edit"}]})
        return code, result


def export_at(directory, head):
    with ZipFile(BytesIO(release.git(directory, "cat-file", "blob", head + ":" + ARCHIVE))) as archive:
        return json.loads(archive.read("task.json")), archive.read("summary.md")


def expected(task):
    lines = ["# Task " + task["taskId"], "", "Activity: " + task["activity"],
             "Recorded decisions: " + str(len(task["decisions"])), "", "## Verification", ""]
    for i, receipt in enumerate(task["receipts"], 1):
        lines.append(f"- Receipt {i} · {receipt['status']} · {receipt['digest']}")
    if not task["receipts"]:
        lines.append("No verification receipts recorded.")
    return "\n".join(lines) + "\n"


def verify(leases, generation, task):
    mixed = {**task, "activity":"Blocked", "receipts":[{**task["receipts"][0], "status":"Failed"}, {**task["receipts"][0], "status":"Rejected"}, *task["receipts"]]}
    checks = {}
    for selector, sample in [("retained-export", task), ("mixed-receipts", mixed)]:
        result = leases.execute(generation, RUN, sample)
        checks[selector] = result.returncode == 0 and result.stdout.decode() == expected(sample)
    empty = {**task, "receipts":[]}
    result = leases.execute(generation, RUN, empty)
    invalid = leases.execute(generation, RUN, {**task, "format":"unknown"})
    checks["unverified-input"] = (result.returncode == 0 and result.stdout.decode() == expected(empty)
                                   and invalid.returncode == 1 and not invalid.stdout)
    if not all(checks.values()):
        raise ValueError("Isolated candidate failed required checks: " + str(checks))
    return checks


def prepare(directory):
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Preparation requires an empty rehearsal directory")
    profile, policy_hash = release.policy()
    base = profile["base"]
    task = str(uuid4())
    controller = test_slice.FactoryTest()
    try:
        controller.setUp()
        canary = controller.create("known-decision")["id"]
        def live_fingerprint():
            # Read the serving controller and its real persisted task without disclosing local credentials.
            with closing(sqlite3.connect((Path(controller.folder) / "slice.db").as_uri() + "?mode=ro", uri=True)) as db:
                record, = db.execute("SELECT document FROM tasks WHERE id=?", (canary,)).fetchone()
            return {"runtime":release.digest(test_slice.DLL.read_bytes()),
                    "installedPolicy":release.digest((test_slice.DLL.parent / "profiles/shipping-v1.json").read_bytes()),
                    "checkpoint":fingerprint(record), "task":fingerprint(json.dumps(controller.get(canary), sort_keys=True))}
        before = live_fingerprint()
        def isolation_evidence():
            if live_fingerprint() != before:
                raise ValueError("Independent controller or persisted task changed during candidate execution")
            return {"checkpointAndRuntimePreserved":True, **before}
        cert = prepare_draft(directory, profile, policy_hash, base, task, isolation_evidence)
        isolation_evidence()
        release.stage(directory, cert)
        return cert
    finally:
        controller.doCleanups()


def prepare_draft(directory, profile, policy_hash, base, task, isolation_evidence):
    fixture = FactoryRepo(directory, ROOT / "fixtures/shipping/repository")
    try:
        fixture.git("fetch", "--no-tags", str(ROOT), base)
        fixture.git("update-ref", "refs/heads/main", base, fixture.base)
        fixture.base = base
        fixture.add_issue(profile["issue"])
        fixture.issues[profile["issue"]].update(title=profile["title"], body=profile["body"])
        sample, summary = export_at(directory, base)
        if not summary or release.git(directory, "ls-tree", base, "--", profile["path"]):
            raise ValueError("The approved base must supply the existing export and lack the proposed command")
        origin = {"apiUrl":fixture.url, "repoId":profile["repositoryId"], "repository":profile["repository"],
                  "number":profile["issue"], "issueId":profile["issueId"], "base":"main", "baseSha":base,
                  "baseTree":fixture.git("rev-parse", base+"^{tree}"), "sourceHash":release.digest(release.git(directory, "ls-tree", "-rz", base)),
                  "path":profile["path"], "scenario":profile["version"], "policyHash":policy_hash,
                  "scopeHash":fingerprint(profile["title"]+"\n"+profile["body"])}
        def adapter(payload, *args):
            result = subprocess.run(["dotnet", str(DLL), *args], input=json.dumps(payload).encode(),
                                    capture_output=True, check=True, timeout=45)
            return json.loads(result.stdout)
        issue = fixture.issues[profile["issue"]]
        issue["labels"].append({"name":"hold"})
        try:
            adapter({"origin":origin}, "--admit")
        except subprocess.CalledProcessError as error:
            if b"Issue or base differs from admission" not in error.stderr:
                raise
        else:
            raise ValueError("Held self-development issue admitted a writer")
        issue["labels"].pop()
        if adapter({"origin":origin}, "--admit") is not True:
            raise ValueError("Factory-source issue admission did not complete")
        leases = Leases(directory / "leases.sqlite")
        proposal = (ROOT / "fixtures/self-development" / ENTRY).read_text(encoding="utf-8")
        roles = {}

        def execute(role, files, work):
            run = str(uuid4())
            generation = leases.acquire(task, run, role, profile["version"], policy_hash, files)
            try:
                row = leases.record(generation)
                info = leases.owned(row)
                host = info["HostConfig"]
                if (host["NetworkMode"] != "none" or not host["ReadonlyRootfs"] or host["Privileged"]
                        or host["Binds"] or any(m["Type"] != "tmpfs" for m in info["Mounts"])
                        or any(key.split("=")[0].startswith(("FACTORY_", "GITHUB_", "OPENAI_", "CODEX_"))
                               for key in info["Config"]["Env"])):
                    raise ValueError("Factory role has access outside its admitted environment")
                outcome = {"base":base, "isolated":True, **work(generation)}
                leases.finish(generation, outcome)
            finally:
                leases.release(generation)
            roles[role] = {"generation":generation, "run":run, "outcome":outcome}

        def write(generation):
            output(leases.execute(generation, WRITE))
            candidate = leases.snapshot(generation, ENTRY)
            return {"candidateHash":fingerprint(candidate)}

        execute("writer", {"proposal.cjs":proposal}, write)
        candidate = leases.restore_candidate(task, proposal)
        candidate_hash = fingerprint(candidate)
        execute("verifier", {ENTRY:candidate}, lambda g: {"candidateHash":candidate_hash, "checks":verify(leases,g,sample)})
        verified = roles["verifier"]
        def review(generation):
            observed = output(leases.execute(generation, REVIEW))
            if observed != fingerprint(proposal):
                raise ValueError("Sealed source differs from the bounded change reviewed by the fixture")
            return {"candidateHash":observed, "approved":True, "verifierRun":verified["run"]}
        execute("reviewer", {ENTRY:candidate, "verification.json":json.dumps(verified["outcome"])}, review)
        if fixture.git("rev-parse", "refs/heads/main") != base:
            raise ValueError("Factory source base moved during isolated execution")
        cert = {"profile":profile["version"], "policyHash":policy_hash, "task":task, "issue":profile["issue"],
                "path":profile["path"], "base":base, "candidateHash":candidate_hash, "transport":"loopback",
                "roles":roles, "controlPlane":isolation_evidence()}
        release.validate_roles(directory, cert)
        gate_hash = fingerprint(json.dumps(roles, sort_keys=True))
        intent = {"taskId":task,"branch":"factory/"+task, "marker":"<!-- factory-task:"+task+" -->",
                  "candidate":candidate, "candidateHash":candidate_hash, "verifier":verified["run"],
                  "reviewer":roles["reviewer"]["run"], "gateHash":gate_hash,
                  "openedAt":fixture.issues[profile["issue"]]["updated_at"],
                  "ci":{"executionId":verified["run"],"policyHash":policy_hash,"environment":IMAGE,
                        "receiptHash":fingerprint(json.dumps(verified["outcome"], sort_keys=True, separators=(",", ":")))}}
        def publish():
            return adapter({"origin":origin, "intent":intent})
        for unexpected in ("README.md", "scripts/factory_backup.py"):
            fixture.extra_path = unexpected
            try:
                publish()
            except subprocess.CalledProcessError as error:
                if b"differs outside the sealed change" not in error.stderr:
                    raise
            else:
                raise ValueError("Publisher accepted an additional factory-source edit")
            if fixture.pulls or "git/refs" in fixture.writes:
                raise ValueError("Publisher wrote a branch or Draft for an unreviewed tree")
        fixture.extra_path = None
        receipt = publish()
        if publish() != receipt or len(fixture.pulls) != 1 or not fixture.pulls[0]["draft"]:
            raise ValueError("Draft replay did not identify the original reviewed head")
        return {**cert, "head":receipt["headSha"], "draftNumber":receipt["number"]}
    finally:
        fixture.close()


def smoke(directory):
    state = release.inspect(directory)
    cert = state["certificate"]
    active = state["events"][-1]["active"]
    sample, summary = export_at(directory, active)
    if active == cert["base"]:
        if release.git(directory, "ls-tree", active, "--", cert["path"]) or not summary:
            raise ValueError("Known-good release failed its export smoke check")
        return {"active":active, "bundledMarkdown":release.digest(summary), "command":"absent"}
    if active != cert["head"]:
        raise ValueError("Active release is outside the reviewed pair")
    source = release.git(directory, "cat-file", "blob", active+":"+cert["path"]).decode()
    if fingerprint(source) != cert["candidateHash"]:
        raise ValueError("Active release contains different candidate bytes")
    leases = Leases(directory / "leases.sqlite")
    generation = leases.acquire(cert["task"], str(uuid4()), "verifier", "release-smoke", cert["policyHash"], {ENTRY:source})
    try:
        result = leases.execute(generation, RUN, sample)
        if result.returncode or result.stdout.decode() != expected(sample):
            raise ValueError("The selected release failed its Markdown export smoke check")
        return {"active":active, "command":"passed", "markdownHash":release.digest(result.stdout)}
    finally:
        leases.release(generation)


def exercise(directory):
    cert = prepare(directory)
    base, head = cert["base"], cert["head"]
    def rejected(call, *args):
        try:
            call(*args)
        except ValueError:
            return
        raise AssertionError("Unqualified release decision was accepted")
    initial = smoke(directory)
    piped = subprocess.run([sys.executable, str(ROOT / "scripts/factory_release.py"), str(directory),
                            "promote", "0", base, head], input=("promote " + head + "\n").encode(),
                           capture_output=True, timeout=10)
    if piped.returncode == 0 or b"operator confirmation required" not in piped.stderr:
        raise AssertionError("Unattended CLI request bypassed operator confirmation")
    for change in ("wrong-reviewer","failed-check","foreign-head"):
        altered = deepcopy(cert)
        if change == "wrong-reviewer":
            altered["roles"]["reviewer"] = altered["roles"]["writer"]
        elif change == "failed-check":
            altered["roles"]["verifier"]["outcome"]["checks"]["mixed-receipts"] = False
        else:
            altered["head"] = base
        rejected(release.validate, directory, altered)
    rejected(release.transition, directory, "promote", 0, base, base, "simulated operator")
    assert release.inspect(directory)["events"][-1]["active"] == base
    release.transition(directory, "promote", 0, base, head, "simulated operator")
    promoted = smoke(directory)
    rejected(release.transition, directory, "promote", 0, base, head, "simulated operator")
    # Recover the pinned good release even if the disposable Draft branch moves after activation.
    release.git(directory, "update-ref", "refs/heads/factory/" + cert["task"], base, head)
    rejected(release.validate, directory, cert)
    release.transition(directory, "rollback", 1, head, base, "simulated operator")
    rolled_back = smoke(directory)
    assert rolled_back == initial and len(release.inspect(directory)["events"]) == 3
    return {"evidenceClass":"controlled fixture", "humanApproval":None, **release.inspect(directory),
            "initial":initial, "promoted":promoted, "rolledBack":rolled_back}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path, help="Retain an operator target at the original base")
    parser.add_argument("--smoke", type=Path, help="Check the selected version of a retained rehearsal")
    parser.add_argument("--evidence", type=Path, help="Save the controlled round trip")
    args = parser.parse_args()
    if args.prepare:
        report = prepare(args.prepare.resolve())
    elif args.smoke:
        report = smoke(args.smoke.resolve())
    else:
        with tempfile.TemporaryDirectory() as scratch:
            report = exercise(Path(scratch))
        if args.evidence:
            with args.evidence.open("x", encoding="utf-8") as file:
                json.dump(report, file, indent=2)
                file.write("\n")
    if args.prepare or args.smoke:
        print(json.dumps(report, indent=2))
    else:
        print("Passed isolated factory-source Draft, scope checks, manual gate, stale decision and rollback smoke checks.")
