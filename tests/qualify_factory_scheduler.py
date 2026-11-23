"""Observe a normal GitHub poller, lifecycle owner, Hatchet worker/dispatcher and CI service.
Only the GitHub REST endpoint and the explicitly selected worker class are fixtures.
"""
import argparse
from contextlib import ExitStack, closing
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from uuid import UUID
from zipfile import ZipFile

from qualify_dispatch import ROOT, DLL, BRIDGE, laptop_stack, request, stop, wait_for
from github_fixture import GitHubFixture
from qualify_pilot import download
from hashlib import sha256


class FactoryRepository(GitHubFixture):
    repo = "fixture/factory"


def verify_deadline_trace(timeline, attempts, leases):
    expired = next(row for row in attempts if row["status"]=="interrupted")
    lease, = [row for row in leases if row["generation"]==expired["generation"]]
    assert lease["state"]=="released" and lease["outcome_hash"] is None
    assert expired["usage"] and any(e["tool"]=="replace_text" for e in expired["events"])
    for attempt in attempts:
        stages = [e for e in timeline["stages"] if e["generation"]==attempt["generation"] and e["run"]==attempt["run"]]
        names = [e["stage"] for e in stages]
        assert "native.completed" in names and "release.end" in names
        op = UUID(attempt["run"]).hex+"."+attempt["action"]
        ack = next(d for d in timeline["dispatch"] if d["dispatch"]==op)
        if attempt==expired:
            assert "lease.sealed" not in names and "result.ready" not in names
            assert ack["terminal"]=="FAILED" and any(record.get("run")==ack["schedulerId"] and
                any(e["eventType"]=="TIMED_OUT" for e in record["events"]) for record in timeline["deadlineEvents"])
        else:
            assert "lease.sealed" in names and names[-1]=="result.ready" and ack["terminal"]=="COMPLETED"
            assert stages[-1]["at"] <= ack["controllerAcknowledgedAt"]/1000 + .001


def run(destination, live=False, expect_quota=False, fault=None, issue_number=1, checkout=False, multi=False):
    destination.mkdir(parents=True, exist_ok=False)
    policy_name = "v2-policy.json" if live else "factory-policy.json"
    if live and not expect_quota:
        from codex_worker import allowance, environment
        observation, wait = allowance(environment())
        (destination / "pre-intake.json").write_text(json.dumps({"observation":observation,"wait":wait},indent=2),encoding="utf-8")
        if wait:
            raise RuntimeError("Subscription admission deferred before intake: "+wait)
    checkout = checkout or multi
    if checkout and not live:
        raise ValueError("The checkout repository requires subscription-backed workers")
    directory = ROOT / "fixtures" / ("checkout-scope" if checkout else "factory-summary")
    rules = json.loads((directory / ("v2-policy.json" if multi else "factory-policy.json" if checkout else policy_name)).read_text())
    base = rules["settings"]["base"]
    started = time.monotonic()
    with ExitStack() as cleanup:
        temp = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
        repo = FactoryRepository(temp, ROOT / "fixtures/shipping/repository")
        cleanup.callback(repo.close)
        repo.git("fetch", "--no-tags", str(ROOT), base)
        repo.git("update-ref", "refs/heads/main", base, repo.base)
        repo.base = base
        approval = repo.add_issue(issue_number)
        issue = repo.issues[issue_number]
        issue.update(title=rules["goal"] if checkout else "Display repairs in the factory evidence renderer", body=rules["goal"])
        approval.update(scenario="known-decision" if checkout else "render-repairs", scopeHash=sha256((issue["title"]+"\n"+issue["body"]).encode()).hexdigest())
        config = temp / "repository.json"
        config.write_text(json.dumps({"apiUrl":repo.url,"repository":repo.repo,"repositoryId":repo.repo_id,
                                      "baseBranch":"main","localAtomicDraft":True,"issues":[approval]}))
        pgport, grpc, apiport, url, http, _, dbkey, _, dashboard, service, _, sql, tenant = laptop_stack(cleanup)
        ci = secrets.token_hex(24)
        # Separate processes see only the credentials needed by their service.
        system = {k:v for k,v in os.environ.items() if not k.startswith(("FACTORY_", "HATCHET_", "GITHUB_", "OPENAI_", "CODEX_"))}
        def launch(label, command, env):
            log = cleanup.enter_context((temp / (label+".log")).open("wb"))
            p = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log, env={**system, **env})
            cleanup.callback(stop, p)
            return p
        common = {"FACTORY_CONTROLLER_URL":url,"FACTORY_LEASE_DB":str(temp/"leases.sqlite"),"FACTORY_OPERATION_TIMING":"1"}
        profile = {}
        if live:
            funding = temp/"funding.json"
            funding.write_text(json.dumps({"subscriptionFile":"subscription.json"}))
            profile = {"FACTORY_LEASE_DB":common["FACTORY_LEASE_DB"],"FACTORY_FUNDING_PROFILE":str(funding)}
        api = launch("controller", ["dotnet", str(DLL)], {**profile,"FACTORY_PORT":str(apiport),"FACTORY_LOCAL_TOKEN":dashboard,
            "FACTORY_STATE":str(temp),"FACTORY_DISPATCH":"hatchet","FACTORY_DISPATCH_TOKEN":service,
            "FACTORY_CI_TOKEN":ci,"FACTORY_PROJECT":rules["policy"]["version"],"FACTORY_GITHUB_PROJECT":str(config),
            "FACTORY_POSTGRES":f"Host=127.0.0.1;Port={pgport};Database=factory;Username=factory;Password={dbkey}"})
        def tasks():
            if api.poll() is not None:
                raise AssertionError("Factory controller stopped before qualification")
            return request(url,"/api/tasks",dashboard)
        task = wait_for(lambda: next(iter(tasks()), None), "normal issue intake", 70)
        tid = task["id"]
        env = {**common,"FACTORY_DISPATCH_TOKEN":service,"HATCHET_CLIENT_TOKEN":tenant,
               "HATCHET_CLIENT_TLS_STRATEGY":"none","HATCHET_CLIENT_HOST_PORT":f"127.0.0.1:{grpc}","HATCHET_CLIENT_API_URL":http}
        if live and os.environ.get("CODEX_HOME"):
            env["CODEX_HOME"] = os.environ["CODEX_HOME"]
        node = ["node","--experimental-strip-types",str(BRIDGE)]
        assert launch("register", [*node,"register"], env).wait(timeout=35) == 0
        worker = launch("worker", [*node,"worker"], env)
        launch("dispatch", [*node,"dispatch"], env)
        launch("ci", [sys.executable, "-u", str(ROOT/"scripts/run_ci.py")], {**common,"FACTORY_CI_TOKEN":ci})
        interventions = []
        fault_wait = 0.0
        deadline_events = []
        wait_intervals = []
        if live:
            # Capture interrupted attempts and holds before the disposable controller stops.
            def retain_native_evidence():
                archive = destination/"task.zip"
                if not archive.exists():
                    archive.write_bytes(download(url,dashboard,tid))
                with closing(sqlite3.connect(common["FACTORY_LEASE_DB"])) as audit:
                    audit.row_factory = sqlite3.Row
                    attempts = []
                    leases = []
                    if audit.execute("SELECT 1 FROM sqlite_master WHERE name='leases'").fetchone():
                        leases = [dict(row) for row in audit.execute(
                            "SELECT generation,run,role,state,candidate_hash,outcome_hash FROM leases WHERE task=?",(tid,))]
                    if audit.execute("SELECT 1 FROM sqlite_master WHERE name='codex_attempts'").fetchone():
                        for row in audit.execute("SELECT run,action,started,ended,status,source,policy,thread,generation,events,usage,observed "
                                                 "FROM codex_attempts WHERE task=? ORDER BY started", (tid,)):
                            record = dict(row)
                            for key in ("events","usage","observed"):
                                record[key] = json.loads(record[key]) if record[key] else None
                            attempts.append(record)
                stages = [json.loads(line) for attempt in attempts for path in [temp/("codex-"+attempt["run"]+".jsonl")]
                          if path.exists() for line in path.read_text().splitlines()]
                dispatch = [json.loads(line) for line in (temp/"dispatch.log").read_text(errors="replace").splitlines()
                            if line.startswith('{"dispatch":')]
                timeline = {"stages":stages,"dispatch":dispatch,"deadlineEvents":deadline_events}
                (destination/"timeline.json").write_text(json.dumps(timeline,indent=2)+"\n",encoding="utf-8")
                (destination/"attempts.json").write_text(json.dumps({"task":tid,"owner":task["ownerId"],
                    "elapsedSeconds":time.monotonic()-started,"interventions":interventions,"leases":leases,"attempts":attempts},indent=2)+"\n",
                    encoding="utf-8")
                if fault=="deadline" and (destination/"draft.bundle").exists():
                    verify_deadline_trace(timeline,attempts,leases)
            cleanup.callback(retain_native_evidence)
        if fault:
            def actual_edit():
                if tasks()[0]["activity"] in ("Waiting","Blocked"):
                    raise AssertionError("Live task stopped before a genuine repository edit")
                if not Path(common["FACTORY_LEASE_DB"]).exists():
                    return None
                with closing(sqlite3.connect(common["FACTORY_LEASE_DB"])) as audit:
                    if not audit.execute("SELECT 1 FROM sqlite_master WHERE name='codex_attempts'").fetchone():
                        return None
                    for run_id, generation, events in audit.execute(
                            "SELECT run,generation,events FROM codex_attempts WHERE task=? AND status='running' AND thread IS NOT NULL",(tid,)):
                        if any(e["tool"]=="replace_text" for e in json.loads(events or "[]")):
                            return run_id,generation
                return None
            run_id,generation = wait_for(actual_edit,"real Codex repository edit",165)
            op = UUID(run_id).hex+".Implement"
            scheduler,claim_token = sql("SELECT o.scheduler_id||' '||i.claim FROM dispatch_outbox o "
                                        "JOIN dispatch_inbox i ON i.id=o.id WHERE o.id='"+op+"'").split()
            before = time.monotonic()
            if fault == "deadline":
                # Contend only on this disposable stack's journal, after the native turn returned.
                # The controller and Hatchet use PostgreSQL and continue to enforce their own deadline.
                trace = temp/("codex-"+run_id+".jsonl")
                def reached_snapshot():
                    return trace.exists() and '"stage": "snapshot.start"' in trace.read_text()
                wait_for(reached_snapshot,"native completion and snapshot start",60)
                before = time.monotonic()
                with closing(sqlite3.connect(common["FACTORY_LEASE_DB"],timeout=2)) as lock:
                    lock.execute("BEGIN IMMEDIATE")
                    try:
                        state = lock.execute("SELECT state,outcome_hash FROM leases WHERE generation=?",(generation,)).fetchone()
                        assert state==("active",None), "Fault missed the unsealed writer"
                        acquired = time.time()
                        deadline_events.append({"event":"journal.locked","at":acquired,"generation":generation})
                        def deadline_hold():
                            recovery = request(url,"/api/tasks/"+tid+"/workspace",dashboard)["recovery"]
                            return recovery and recovery["operationId"]==op
                        wait_for(deadline_hold,"scheduler deadline hold",65)
                    finally:
                        lock.rollback()
                        released = time.time()
                        wait_intervals.append((acquired,released))
                        deadline_events.append({"event":"journal.unlocked","at":released,"generation":generation})
                js = """import {Hatchet} from '@hatchet-dev/typescript-sdk';
const h=Hatchet.init(), details=await h.runs.get(process.argv[1]), task=details.tasks[0];
const {data}=await h.api.v1TaskEventList(task.taskExternalId,{limit:50});
console.log(JSON.stringify({run:process.argv[1],status:details.run.status,
    events:data.rows.map(({eventType,timestamp})=>({eventType,timestamp}))}));"""
                output = subprocess.check_output(["node","--input-type=module","-e",js,str(UUID(scheduler))],
                                     env={**system,**env},cwd=BRIDGE.parent,timeout=12)
                terminal = json.loads(output)
                deadline_events.append(terminal)
                assert any(e["eventType"]=="TIMED_OUT" for e in terminal["events"]),terminal
            elif fault == "cancel":
                cancel_js = "import {Hatchet} from '@hatchet-dev/typescript-sdk'; await Hatchet.init().runs.cancel({ids:[process.argv[1]]});"
                subprocess.run(["node","--input-type=module","-e",cancel_js,str(UUID(scheduler))],
                               env={**system,**env},cwd=BRIDGE.parent,check=True,capture_output=True,timeout=12)
            else:
                stop(worker)
                launch("replacement-worker", [*node,"worker"], env)
            fault_seconds = time.monotonic()-before
            if fault=="deadline":
                fault_wait = released-acquired
                fault_seconds -= fault_wait
            interventions.append({"action":fault,"cause":"qualification fault after observed native edit",
                                  "activeSeconds":fault_seconds})
            before_wait = time.time()
            def safe_hold():
                recovery = request(url,"/api/tasks/"+tid+"/workspace",dashboard)["recovery"]
                with closing(sqlite3.connect(common["FACTORY_LEASE_DB"])) as audit:
                    lease = audit.execute("SELECT state,outcome_hash FROM leases WHERE generation=?",(generation,)).fetchone()
                return recovery if recovery and recovery["operationId"]==op and lease==("released",None) else None
            hold = wait_for(safe_hold,"parked operation and retired writer lease",170)
            end_wait = time.time()
            wait_intervals.append((before_wait,end_wait))
            fault_wait += end_wait-before_wait
            before = time.monotonic()
            route = "/api/tasks/"+tid+"/recover"
            request(url,route,dashboard,{"operationId":op,"gateSequence":hold["gateSequence"],
                                        "note":"Confirmed release of the interrupted native writer before a fresh assignment."})
            try:
                request(url,"/internal/dispatch/"+op+"/result",service,{"token":claim_token,"output":{}})
            except HTTPError as err:
                assert err.code==409
            else:
                raise AssertionError("Retired worker result accepted")
            interventions.append({"action":"recover","cause":hold["kind"],"activeSeconds":time.monotonic()-before,
                                  "retiredOperation":op,"generation":generation,"lateResultHttpStatus":409,
                                  "retiredLeaseState":"released","retiredOutcomeHash":None})
        def delivered():
            current, = tasks()
            if expect_quota and current["activity"] in ("Blocked","Waiting"):
                return current
            if current["activity"] in ("Blocked","Waiting"):
                print(current["phase"], [(h["actor"],h["text"][:160]) for h in current["history"][-5:]], flush=True)
                operations = sql("SELECT request FROM dispatch_outbox").splitlines()
                (destination/"failed-orders.json").write_text(json.dumps([json.loads(op) for op in operations]),encoding="utf-8")
                if (temp/"leases.sqlite").exists():
                    with closing(sqlite3.connect(temp/"leases.sqlite")) as db:
                        print(db.execute("SELECT role,state,operation FROM leases").fetchall(), flush=True)
                raise AssertionError("Factory task stopped: " + str(current["history"][-1]["text"]))
            return current if current["activity"] == "Delivered" else None
        done = wait_for(delivered, "gated delivery", 240)
        lifecycle_elapsed = time.monotonic()-started
        def timing():
            return [json.loads(line) for name in ("worker","ci","replacement-worker") if (temp/(name+".log")).exists() for line in
                    (temp/(name+".log")).read_text(errors="replace").splitlines() if line.startswith('{"operation":')]
        if expect_quota:
            recovery = request(url,"/api/tasks/"+tid+"/workspace",dashboard)["recovery"]
            assert done["phase"]=="Implementation" and done["activity"]=="Blocked" and recovery["kind"]=="Quota", recovery
            assert not repo.pulls and not done["receipts"] and done["candidate"] is None
            with closing(sqlite3.connect(common["FACTORY_LEASE_DB"])) as db:
                row, = db.execute("SELECT status,observed,started,ended,thread,generation FROM codex_attempts WHERE task=?", (tid,))
                assert row[0]=="Quota" and row[4:] == (None,None)
                observed = json.loads(row[1])
                assert observed["auth"]=="ChatGPT" and any(w["usedPercent"]==100 for w in observed["windows"])
            # Negative probes use the parked assignment and cannot advance its lifecycle.
            request_json = sql("SELECT request FROM dispatch_outbox WHERE id='"+recovery["operationId"]+"'")
            fixture = subprocess.run(["dotnet",str(DLL),"--fixture-worker"],input=request_json.encode(),
                                     capture_output=True,timeout=10)
            assert fixture.returncode and not fixture.stdout.strip()
            refreshed = subprocess.check_output(["node",str(ROOT/"scripts/m0-account.mjs")],env={**system,**env},timeout=35)
            (temp/"subscription.json").write_bytes(refreshed)
            assert any(w["usedPercent"]==100 for w in json.loads(refreshed)["windows"])
            try:
                request(url,"/api/tasks/"+tid+"/recover",dashboard,{"operationId":recovery["operationId"],
                        "gateSequence":recovery["gateSequence"],"note":"Negative test of recovery while the live quota is exhausted."})
            except HTTPError as err:
                assert err.code==409
            else:
                raise AssertionError("Recovery accepted an exhausted live allowance")
            assert tasks()[0]["activity"]=="Blocked"
            timings = timing()
            active = sum((t["ended"]-t["started"])/1000 for t in timings)
            elapsed = lifecycle_elapsed
            report = {"evidenceClass":"real local Codex account preflight; no model turn; loopback GitHub",
                      "task":tid,"owner":done["ownerId"],"operation":recovery["operationId"],"wait":"Quota",
                      "account":observed,"wallSecondsToObservedHold":elapsed,"workerSeconds":active,
                      "setupSchedulingAndPollingSeconds":elapsed-active,"preflightSeconds":row[3]-row[2],
                      "modelCalls":0,"tokens":0,"incrementalApiSpend":0,"hostCost":None,"manualTaskInterventions":[],
                      "negativeProbes":["scripted worker refused live order","recovery rejected still-exhausted account"],
                      "negativeProbeSeconds":time.monotonic()-started-elapsed,
                      "waitStarted":recovery["since"],"waitEnded":None,"draft":None}
            (destination/"metrics.json").write_text(json.dumps(report,indent=2)+"\n")
            archive = download(url,dashboard,tid)
            with ZipFile(BytesIO(archive)) as z:
                turn, = json.loads(z.read("codex-attempts.json"))["attempts"]
                assert turn["status"]=="Quota" and turn["threadId"] is None and turn["repositoryEdits"]==0
            (destination/"task.zip").write_bytes(archive)
            print("Confirmed real subscription quota hold on normal intake; no model turn or Draft.",flush=True)
            return
        def published():
            messages = (temp/"controller.log").read_text(errors="replace").splitlines()
            problems = [line.strip() for line in messages if f"GitHub poll for issue {issue_number} deferred:" in line]
            if problems:
                raise AssertionError(problems[-1])
            return next(iter(repo.pulls), None)
        pr = wait_for(published, "normal Draft publication poll", 65)
        assert pr["draft"] and len(repo.pulls) == 1 and repo.git("rev-parse","refs/heads/main") == base
        evidence = "Subscription-backed Codex implementation and review." if live else "Controlled fixture evidence."
        reviewer = "Independent Codex reviewer run" if live else "Independent fixture reviewer"
        assert evidence in pr["body"] and "Local GitHub fixture." in pr["body"]
        assert f"{reviewer}: {done['review']['runId']}" in pr["body"]
        expected_worker = "Codex" if live else "fixture"
        assert pr["title"] == f"Reviewed {expected_worker} change for issue #{issue_number}"
        head = pr["head"]["sha"]
        path = rules["repositoryLayout"]["source"] if checkout else "fixtures/self-development/task-summary.cjs"
        packed = json.loads(done["candidate"]) if multi else {path:done["candidate"]}
        expected_paths = [path, *rules["repositoryLayout"]["additionalWrites"]] if multi else [path]
        assert set(packed) == set(expected_paths) == set(repo.git("diff","--name-only",base,head).splitlines())
        for name,value in packed.items():
            blob = subprocess.check_output(["git","--git-dir",str(repo.git_dir),"cat-file","blob",head+":"+name])
            assert blob == value.encode()
        assert sha256(done["candidate"].encode()).hexdigest() == done["review"]["candidateHash"]
        if live:
            assert done["receipts"][-1]["passed"] and done["repairs"] <= rules["policy"]["maxRepairs"]
            with closing(sqlite3.connect(common["FACTORY_LEASE_DB"])) as db:
                native = db.execute("SELECT action,thread,generation,usage,events FROM codex_attempts WHERE task=? AND status='sealed'", (tid,)).fetchall()
            assert {r[0] for r in native} == {"Implement","Review"} and len({r[1] for r in native}) == len(native)
        else:
            assert done["repairs"] == 1 and [r["passed"] for r in done["receipts"]] == [False,True]
        archive = download(url,dashboard,tid)
        with ZipFile(BytesIO(archive)) as z:
            exported = json.loads(z.read("task.json"))
        assert exported["publication"]["headSha"] == head and exported["publication"]["transport"] == "loopback"
        schedules = sql("SELECT id||' '||scheduler_id FROM dispatch_outbox WHERE scheduler_id IS NOT NULL ORDER BY id").splitlines()
        assert all(any(action in row for row in schedules) for action in (".Plan", ".Ask", ".Implement", ".Review"))
        with closing(sqlite3.connect(temp/"leases.sqlite")) as db:
            leases = db.execute("SELECT role,run,state,candidate_hash FROM leases WHERE task=? ORDER BY rowid",(tid,)).fetchall()
        assert all(row[2]=="released" for row in leases) and {row[0] for row in leases} == {"writer","verifier","reviewer"}
        elapsed = time.monotonic() - started
        timings = timing()
        active = sum((t["ended"]-t["started"])/1000 for t in timings)
        assert len(schedules)-int(bool(fault)) <= len(timings) <= len(schedules)
        report = {"evidenceClass":"scripted workers / real local Docker CI and Hatchet / loopback GitHub",
            "task":tid,"issue":issue_number,"base":base,"head":head,"owner":done["ownerId"],"hatchetAndCiOperations":schedules,
            "leases":leases,"executionIntervalsUnixMs":timings,"wallSeconds":elapsed,"workerAndCiSeconds":active,
            "setupSchedulingAndPollingSeconds":elapsed-active,"operatorWaitSeconds":0,"manualTaskInterventions":[],
            "modelCalls":0,"tokens":0,"incrementalApiSpend":0,"hostCost":None}
        if live:
            report.update(evidenceClass="real subscription Codex writer and fresh reviewer / Docker CI and Hatchet / loopback GitHub",
                          modelCalls=None, tokens=None, manualTaskInterventions=interventions, operatorWaitSeconds=fault_wait,
                          nativeTurns=[{"role":r[0],"thread":r[1],"generation":r[2],
                          "usage":json.loads(r[3]),"events":json.loads(r[4])} for r in native])
            # A killed dispatcher may never log its interval end. Count only the observed work.
            report["automatedFaultActions"] = report.pop("manualTaskInterventions")
            report["manualTaskInterventions"] = []
            # Holding the journal can overlap an active worker and its cleanup.
            overlap = sum(max(0,min(end,t["ended"]/1000)-max(begin,t["started"]/1000))
                          for begin,end in wait_intervals for t in timings)
            report["workerOverlapWithOperatorWaitSeconds"] = overlap
            if fault and not any(t["operation"]==op for t in timings):
                with closing(sqlite3.connect(common["FACTORY_LEASE_DB"])) as audit:
                    began,events = audit.execute("SELECT started,events FROM codex_attempts WHERE run=?",(run_id,)).fetchone()
                partial = max(e["at"] for e in json.loads(events))-began
                report["interruptedWorkerObservedSeconds"] = partial
                report["unattributedSeconds"] = elapsed-active-partial-fault_wait+overlap
            else:
                report["unattributedSeconds"] = elapsed-active-fault_wait+overlap
            del report["setupSchedulingAndPollingSeconds"]
        (destination/"task.zip").write_bytes(archive)
        (destination/"metrics.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
        repo.git("bundle","create",str(destination/"draft.bundle"),"refs/heads/"+pr["head"]["ref"],"^"+base)
        print(f"Passed factory scheduler at {head} in {elapsed:.1f}s",flush=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("destination", type=Path, nargs="?")
    cli.add_argument("--live", action="store_true",help="Use only real subscription Codex writer and reviewer")
    cli.add_argument("--expect-quota", action="store_true",help="Require evidence of an actual subscription quota hold")
    cli.add_argument("--fault", choices=("cancel","interrupt","deadline"),help="Interrupt a live writer after an observed repository edit")
    cli.add_argument("--issue", type=int, default=1, help="Issue number on the local repository fixture")
    cli.add_argument("--checkout", action="store_true", help="Select the checkout repository layout and policy")
    cli.add_argument("--multi-file",action="store_true",help="Select the checkout policy with two editable paths")
    args = cli.parse_args()
    if args.issue < 1:
        cli.error("--issue must be positive")
    if args.fault and (not args.live or args.expect_quota):
        cli.error("--fault requires a live qualification with available capacity")
    if args.expect_quota and not args.live:
        cli.error("--expect-quota requires --live")
    destination = args.destination
    if destination:
        run(destination.resolve(),args.live,args.expect_quota,args.fault,args.issue,args.checkout,args.multi_file)
    else:
        with tempfile.TemporaryDirectory() as scratch:
            run(Path(scratch) / "evidence",args.live,args.expect_quota,args.fault,args.issue,args.checkout,args.multi_file)
