"""FCT004 laptop integration test. Uses the normal controller and Hatchet. The optional expert qualification uses the development subscription."""
from contextlib import ExitStack, closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
from datetime import datetime, timezone
from funding_fixture import Provider
from slack_fixture import SlackFixture
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "src/Factory.Api"
DLL = API / "bin/Debug/net10.0/Factory.Api.dll"
BRIDGE = ROOT / "src/Factory.Dispatch/bridge.ts"
PG_IMAGE = "postgres:16-alpine@sha256:4e6e670bb069649261c9c18031f0aded7bb249a5b6664ddec29c013a89310d50"
HATCHET_IMAGE = "ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.114.2@sha256:a205018e4ec2c35af4df9c86cf1e36d4b74b36ebbc849c3b33ca18815cf22d6b"


def docker(*args, env=None, data=None):
    result = subprocess.run(["docker", *args], input=data, capture_output=True, text=True, env=env, timeout=150)
    if result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check, name, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, RuntimeError):
            pass
        time.sleep(.25)
    raise AssertionError(f"Timed out waiting for {name}")


def request(base, path, token, body=None):
    req = Request(base + path, data=None if body is None else json.dumps(body).encode(),
                  headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(req, timeout=6) as response:
        return json.load(response)


def stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=15)


def laptop_stack(cleanup):
    name = "fct004-" + uuid4().hex[:12]
    network = docker("network", "create", name)
    cleanup.callback(docker, "network", "rm", network)
    pgport, httpport, grpcport, apiport = (port() for _ in range(4))
    controller = f"http://127.0.0.1:{apiport}"
    http = f"http://127.0.0.1:{httpport}"
    admin, factory, scheduler, dashboard, service = [secrets.token_hex(24) for _ in range(5)]

    def container(image, variables, *args):
        opts = [item for key in variables for item in ("-e", key)]
        cid = docker("run", "-d", "--network", network, *args, *opts, image,
                     env={**os.environ, **variables})
        cleanup.callback(docker, "rm", "-fv", cid)
        return cid

    pg = container(PG_IMAGE, {"POSTGRES_PASSWORD": admin}, "--network-alias", "db",
                   "-p", f"127.0.0.1:{pgport}:5432")

    def sql(statement, db="factory", user="postgres"):
        return docker("exec", "-i", pg, "psql", "-XAtq", "-v", "ON_ERROR_STOP=1", "-U", user,
                      "-d", db, data=statement)

    # The init server accepts Unix sockets before shutting down. Only the final server listens on TCP.
    wait_for(lambda: "accepting connections" in docker("exec", pg, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"),
             "PostgreSQL TCP listener")
    sql(f"""
        CREATE ROLE factory LOGIN PASSWORD '{factory}';
        CREATE ROLE hatchet LOGIN PASSWORD '{scheduler}';
        CREATE DATABASE factory OWNER factory;
        CREATE DATABASE hatchet OWNER hatchet;
        REVOKE CONNECT ON DATABASE factory FROM PUBLIC;
        REVOKE CONNECT ON DATABASE hatchet FROM PUBLIC;
    """, "postgres")
    assert sql("SELECT has_database_privilege('hatchet','factory','connect') OR "
               "has_database_privilege('factory','hatchet','connect')", "postgres") == "f"
    engine = container(HATCHET_IMAGE, {
        "DATABASE_URL": f"postgresql://hatchet:{scheduler}@db:5432/hatchet?sslmode=disable",
        "SERVER_GRPC_BIND_ADDRESS": "0.0.0.0", "SERVER_GRPC_INSECURE": "t",
        "SERVER_GRPC_BROADCAST_ADDRESS": f"127.0.0.1:{grpcport}",
        "SERVER_INTERNAL_CLIENT_INTERNAL_GRPC_BROADCAST_ADDRESS": "127.0.0.1:7070",
        "SERVER_URL": http, "SERVER_AUTH_COOKIE_INSECURE": "t", "SERVER_AUTH_COOKIE_DOMAIN": "localhost",
        "SERVER_AUTH_SET_EMAIL_VERIFIED": "t", "SERVER_SECURITY_CHECK_ENABLED": "false",
        "ADMIN_EMAIL": "factory@example.invalid", "ADMIN_PASSWORD": "Aa0!" + secrets.token_hex(24),
    }, "-p", f"127.0.0.1:{httpport}:8888", "-p", f"127.0.0.1:{grpcport}:7070")
    print("Started isolated PostgreSQL and Hatchet Lite.", flush=True)

    def mint():
        token = docker("exec", engine, "./hatchet-admin", "token", "create", "--config", "./config",
                       "--name", "factory-qualification").splitlines()[-1].strip()
        return token if token.startswith("eyJ") else None

    tenant = wait_for(mint, "authenticated scheduler", 120)
    return pgport, grpcport, apiport, controller, http, admin, factory, scheduler, dashboard, service, pg, sql, tenant


def run(expert_evidence=None):
    started = time.monotonic()
    if expert_evidence:
        sys.path.insert(0,str(ROOT/"scripts"))
        from codex_worker import allowance, environment
        expert_evidence.mkdir(parents=True, exist_ok=False)
        observed, wait = allowance(environment())
        (expert_evidence/"pre-intake.json").write_text(json.dumps({"observation":observed,"wait":wait},indent=2))
        if wait:
            raise RuntimeError("Subscription admission deferred before intake: " + wait)
    with ExitStack() as cleanup:
        temp = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
        (pgport, grpcport, apiport, controller, http, admin, factory, scheduler, dashboard, service, pg, sql, tenant) = laptop_stack(cleanup)
        observation = temp / "subscription.json"
        funding_profile = temp / "funding.json"
        provider = Provider()
        cleanup.callback(provider.close)
        funded_task = str(uuid4())
        funding_profile.write_text(json.dumps({"subscriptionFile":str(observation), "fixtureApi":{
            "account":"pilot","model":"fixture-owner","endpoint":provider.url,"accountLimitMicros":1000,
            "runLimitMicros":600,"maxInputBytes":512,"maxOutputTokens":32,"inputMicrosPerToken":1,
            "outputMicrosPerToken":2,"taskLimitsMicros":{funded_task:800}}}))
        slack = SlackFixture()
        cleanup.callback(slack.close)
        slack_file = temp / "slack.json"
        slack_file.write_text(json.dumps(slack.profile))
        apienv = {"FACTORY_SLACK_PROFILE":str(slack_file), "FACTORY_SLACK_SIGNING_SECRET":slack.secret,"FACTORY_FUNDING_PROFILE":str(funding_profile), "FACTORY_PORT": str(apiport), "FACTORY_LOCAL_TOKEN": dashboard, "FACTORY_DISPATCH_TOKEN": service,
                  "FACTORY_DISPATCH": "hatchet", "FACTORY_STATE": str(temp),
                  "FACTORY_POSTGRES": f"Host=127.0.0.1;Port={pgport};Database=factory;Username=factory;Password={factory}"}
        worker_env = {"FACTORY_LEASE_DB":str(temp / "leases.sqlite"), "HATCHET_CLIENT_TOKEN": tenant, "HATCHET_CLIENT_TLS_STRATEGY": "none",
                      "HATCHET_CLIENT_HOST_PORT": f"127.0.0.1:{grpcport}", "HATCHET_CLIENT_API_URL": http,
                      "FACTORY_DISPATCH_TOKEN": service, "FACTORY_CONTROLLER_URL": controller}

        if expert_evidence:
            apienv.update(FACTORY_EXPERT_RUNTIME="codex-subscription", FACTORY_LEASE_DB=worker_env["FACTORY_LEASE_DB"])
            worker_env["FACTORY_OPERATION_TIMING"] = "1"

        def process(label, args, env):
            log = cleanup.enter_context((temp / (label + ".log")).open("ab"))
            proc = subprocess.Popen(args, cwd=ROOT, env={**os.environ, "FACTORY_SLACK_PROFILE":"", "FACTORY_SLACK_SIGNING_SECRET":"", "FACTORY_FUNDING_PROFILE":"", **env, "FACTORY_GITHUB_PROJECT":"", "FACTORY_GITHUB_TOKEN":"", "FACTORY_PROJECT":"shipping/v1", "FACTORY_CI_TOKEN":""}, stdout=log, stderr=log)
            cleanup.callback(stop, proc)
            return proc

        def controller_start():
            proc = process("controller", ["dotnet", str(DLL)], apienv)
            wait_for(lambda: request(controller, "/api/tasks", dashboard) is not None, "controller")
            return proc

        api = controller_start()
        for path, token, body in [("/api/tasks", service, None), ("/internal/dispatch/next", dashboard, {})]:
            try:
                request(controller, path, token, body)
                raise AssertionError("credential crossed the controller boundary")
            except HTTPError as error:
                assert error.code == 401

        def dispatch(path, body=None):
            return request(controller, "/internal/dispatch/" + path, service, {} if body is None else body)

        def create(scenario):
            return request(controller, "/api/tasks", dashboard, {"id": str(uuid4()), "scenario": scenario})

        def state(task):
            return next(t for t in request(controller, "/api/tasks", dashboard) if t["id"] == task["id"])

        def expert_activity(task):
            current = state(task)
            if expert_evidence and current["activity"] == "Blocked":
                route = "/api/tasks/"+task["id"]
                hold = request(controller,route+"/workspace",dashboard)["recovery"]
                if hold and hold["kind"] in ("Quota","Authentication"):
                    from qualify_pilot import download
                    (expert_evidence/"held-task.zip").write_bytes(download(controller,dashboard,task["id"]))
                    (expert_evidence/"hold.json").write_text(json.dumps({
                        "task":task["id"],"owner":current["ownerId"],"recovery":hold},indent=2))
                    raise AssertionError("Subscription expert parked: "+hold["kind"])
            return current["activity"]

        def owner(task):
            return request(controller, "/api/tasks/" + task["id"] + "/owner", dashboard)

        a = request(controller, "/api/tasks", dashboard, {"id":funded_task,"scenario":"scope-change"})
        funding_route = "/api/tasks/" + funded_task + "/funding"
        advisory = {"id":str(uuid4()), "question":"Inspect scope evidence."}
        provider.mode = "drop"
        pending = request(controller, funding_route + "/advice", dashboard, advisory)
        assert pending["latest"]["state"] == "Unconfirmed"
        assert request(controller, funding_route, dashboard)["fixture"]["accountUse"]["heldMicros"] > 0
        ticket = dispatch("next")
        op = ticket["id"]
        assert op and sql("SELECT count(*) FROM dispatch_outbox") == "1"
        stop(api)
        api = controller_start()
        assert dispatch("next")["id"] == op
        assert request(controller, funding_route + "/advice", dashboard, advisory) == pending
        receipt = request(controller, funding_route + "/advice/" + advisory["id"] + "/reconcile", dashboard, {"attempt":1})
        assert receipt["latest"]["state"] == "Completed" and len(provider.calls) == 1
        assert request(controller, funding_route, dashboard)["fixture"]["accountUse"] == {"heldMicros":0,"estimatedMicros":18}

        # Stop delivery between the real scheduler accepting a run and the controller learning its ID.
        # Register the workflow first and start its consumer only after the restarted sender recovers the ID.
        submitted, release, result_saved = Event(), Event(), Event()
        expert_reply, replay_expert = Event(), Event()
        expert_path = []
        observed = []
        claims = []
        writer_sessions = []
        refused_writer = Event()
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/submitted") and not release.is_set():
                    observed.append(data["runId"])
                    submitted.set()
                    release.wait(30)
                    self.close_connection = True
                    return
                if self.path.endswith(".Consult/result") and not replay_expert.is_set():
                    expert_path.append(self.path)
                    expert_reply.set()
                    self.send_error(503)
                    return
                try:
                    if self.path.endswith(".Review/claim"):
                        try:
                            request(controller, self.path, service, {**data, "sessionId": writer_sessions[-1]})
                            raise AssertionError("writer session acquired independent review")
                        except HTTPError as error:
                            assert error.code == 409
                            refused_writer.set()
                    out = request(controller, self.path, service, data)
                    if self.path.endswith("/claim") and out["claim"]:
                        claims.append(self.path)
                        if self.path.endswith(".Implement/claim"):
                            writer_sessions.append(data["sessionId"])
                    if self.path.endswith("/result") and not result_saved.is_set():
                        observed.append(data)
                        result_saved.set()
                        self.send_error(503)
                        return
                    payload = json.dumps(out).encode()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(payload)
                except HTTPError as error:
                    self.send_error(error.code)
                except OSError:
                    pass

        proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        Thread(target=proxy.serve_forever, daemon=True).start()
        cleanup.callback(proxy.server_close)
        cleanup.callback(proxy.shutdown)
        cleanup.callback(release.set)
        env = {**worker_env, "FACTORY_CONTROLLER_URL": f"http://127.0.0.1:{proxy.server_port}"}
        node = ["node", "--experimental-strip-types", str(BRIDGE)]
        registration = process("worker", [*node, "register"], env)
        assert registration.wait(timeout=45) == 0, "workflow registration failed"
        sender = process("dispatch", [*node, "dispatch"], env)
        try:
            assert submitted.wait(60), "scheduler never accepted submission"
            stop(sender)
            assert sql("SELECT count(*) FROM dispatch_outbox WHERE scheduler_id IS NOT NULL") == "0"
            release.set()
            sender = process("dispatch", [*node, "dispatch"], env)
            wait_for(lambda: sql("SELECT scheduler_id FROM dispatch_outbox") == observed[0], "original scheduler ID")
            process("worker", [*node, "worker"], env)
            assert result_saved.wait(70), "durable task output never arrived"
            stop(sender)
            with_result = dispatch(op + "/result", observed[1])
            assert with_result
            assert dispatch(op + "/claim", {"runId": observed[0]})["claim"] is None
            assert claims.count("/internal/dispatch/" + op + "/claim") == 1
            try:
                dispatch(op + "/result", {**observed[1], "output": {}})
                raise AssertionError("changed result accepted")
            except HTTPError as error:
                assert error.code == 409
            sender = process("dispatch", [*node, "dispatch"], env)
            def expert_reply_ready():
                if expert_evidence:
                    expert_activity(a)
                return expert_reply.is_set()
            wait_for(expert_reply_ready, "recoverable scheduler expert reply", 70)
            stop(sender)
            stop(api)
            expert_id = expert_path[0].split("/")[-2]
            assert sql(f"SELECT count(*) FROM dispatch_inbox WHERE id='{expert_id}' AND output IS NULL") == "1"
            scheduler_id = sql(f"SELECT scheduler_id FROM dispatch_outbox WHERE id='{expert_id}'")
            api = controller_start()
            assert dispatch("next")["id"] == expert_id
            replay_expert.set()
            sender = process("dispatch", [*node, "dispatch"], env)
            wait_for(lambda: state(a)["activity"] == "Waiting", "reserved decision")
            assert sql(f"SELECT scheduler_id FROM dispatch_outbox WHERE id='{expert_id}'") == scheduler_id
            assert claims.count("/internal/dispatch/" + expert_id + "/claim") == 1
            assert sum(e["actor"] == "Scoped expert" for e in state(a)["history"]) == 1
            if expert_evidence:
                from qualify_pilot import download
                (expert_evidence/"reserved-task.zip").write_bytes(download(controller,dashboard,a["id"]))
            b = create("known-decision")
            wait_for(lambda: len(state(b)["receipts"]) == 1, "failed verification receipt")
            stop(sender)
            pending = dispatch("next")
            message_path = "/api/tasks/" + a["id"] + "/messages"
            workspace_path = "/api/tasks/" + a["id"] + "/workspace"
            message = {"id": str(uuid4()), "text": "What are my decision options?"}
            exchange = request(controller, message_path, dashboard, message)
            workspace = request(controller, workspace_path, dashboard)
            assert workspace["messages"] == [exchange] and workspace["activity"] == "Waiting"
            queue = request(controller, "/api/dashboard/queue", dashboard)
            request(controller, "/api/notifications/poll", dashboard, {})
            notices = "/api/tasks/" + a["id"] + "/notices"
            wait_for(lambda: any(n["state"] == "Sent" for n in request(controller, notices, dashboard)), "notice receipt")
            checkpoint_a, checkpoint_b = state(a), state(b)
            waiting_owner, repair_owner = owner(a), owner(b)
            assert waiting_owner["openQuestion"]["reserved"] and not waiting_owner["decisions"]
            answer, = repair_owner["decisions"]
            assert answer["decision"]["id"] == "SHIP-01" and answer["scope"] == b["id"]
            advice = waiting_owner["advice"]["consultation"]
            assert advice["requiredAuthority"] == "Operator" and advice["taskId"] == a["id"]
            assert advice["profileVersion"] == "checkout-policy/v1"
            assert {s["path"] for s in advice["sources"]} == {"decisions.json", "acceptance.json"}
            assert repair_owner["operationId"] == pending["id"]
            packet = json.loads(sql("SELECT request FROM dispatch_outbox WHERE id='" + pending["id"] + "'"))["context"]
            assert packet["brief"] == repair_owner["briefs"][-1] and packet["decisions"] == [answer]
            assert packet["findings"] == repair_owner["findings"]
            assert checkpoint_a["question"] and not checkpoint_b["receipts"][0]["passed"] and pending["id"]
            stop(api)
            # Terminate a backend after it changes the task and its intent in one open transaction.
            # Its rollback must restore both sides before restart.
            interrupted = subprocess.Popen(["docker", "exec", "-i", pg, "psql", "-Xq", "-U", "factory",
                                            "-d", "factory", "-v", "ON_ERROR_STOP=1"],
                                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           text=True)
            cleanup.callback(stop, interrupted)
            interrupted.stdin.write(f"SET application_name='fct004-interrupt'; BEGIN; "
                                    f"UPDATE tasks SET activity='Blocked' WHERE id='{b['id']}'; "
                                    f"UPDATE dispatch_outbox SET request='uncommitted' WHERE id='{pending['id']}'; "
                                    "SELECT pg_sleep(120);\n")
            interrupted.stdin.close()
            victim = wait_for(lambda: sql("SELECT pid FROM pg_stat_activity WHERE application_name='fct004-interrupt' "
                                          "AND wait_event='PgSleep'"), "uncommitted task and outbox writes")
            assert sql(f"SELECT pg_terminate_backend({int(victim)})") == "t"
            assert interrupted.wait(timeout=10) != 0
            docker("restart", pg)
            wait_for(lambda: sql("SELECT 1") == "1", "database restart")
            api = controller_start()
            assert request(controller, message_path, dashboard, message) == exchange
            assert request(controller, workspace_path, dashboard) == workspace
            assert request(controller, "/api/dashboard/queue", dashboard) == queue
            assert state(a) == checkpoint_a and state(b) == checkpoint_b
            assert owner(a) == waiting_owner and owner(b) == repair_owner
            assert dispatch("next")["id"] == pending["id"]
            sender = process("dispatch", [*node, "dispatch"], env)
            wait_for(lambda: state(b)["activity"] == "Delivered", "repaired delivery", 120)
            final = state(b)
            assert refused_writer.is_set()
            gates = request(controller, "/api/tasks/" + b["id"] + "/gates", dashboard)
            assert gates == final["gates"] and gates[:len(checkpoint_b["gates"])] == checkpoint_b["gates"]
            verify = [g for g in gates if g["input"]["phase"] == "Verification"]
            assert [g["outcome"] for g in verify] == ["Revise", "Pass"]
            assert gates[-1]["nextActivity"] == "Delivered"
            sessions = [g["input"]["sessionId"] for g in gates]
            assert None not in sessions and len(set(sessions)) == len(sessions)
            assert sql("SELECT COUNT(*) FROM gate_evaluations") == str(len(gates) + len(checkpoint_a["gates"]))
            assert [r["passed"] for r in final["receipts"]] == [False, True]
            assert len({r["runId"] for r in state(a)["assignments"]}) == len(state(a)["assignments"])
            assert sum(e["actor"] == "Planner" for e in state(a)["history"]) == 1
            assert sql("SELECT COUNT(*) FROM dispatch_outbox o LEFT JOIN dispatch_inbox i USING(id) "
                       "WHERE o.scheduler_id IS NULL OR i.applied IS DISTINCT FROM 1") == "0"
            assert sql("SELECT COUNT(*) FROM task_events") == str(len(state(a)["history"]) + len(final["history"]))
            assert dispatch(op + "/result", observed[1])
            assert state(a) == checkpoint_a
            assert len(claims) == len(set(claims)) == int(sql("SELECT COUNT(*) FROM dispatch_inbox"))
            routine = create("expert-comparison")
            wait_for(lambda: expert_activity(routine) == "Delivered", "expert-assisted implementation")
            record, = owner(routine)["decisions"]
            assert record["authority"] == "Delegated implementation decision" and record["scope"] == routine["id"]
            assert owner(routine)["advice"]["consultation"]["requiredAuthority"] == "Lifecycle owner"
            assert state(a) == checkpoint_a
            if expert_evidence:
                report = {"evidenceClass":"subscription Codex experts; controlled coding workers and loopback services",
                          "cases":[], "manualTaskInterventions":[], "incrementalApiSpend":0, "hostCost":None}
                with closing(sqlite3.connect(temp/"leases.sqlite")) as journal:
                    for item in (a,routine):
                        t = state(item)
                        gate, = [g for g in t["gates"] if (g["input"]["operationId"] or "").endswith(".Consult")]
                        operation = gate["input"]["operationId"]
                        row, = journal.execute("""SELECT a.run,a.thread,a.generation,a.started,a.ended,a.events,a.usage,l.state
                            FROM codex_attempts a JOIN leases l ON l.generation=a.generation
                            WHERE a.task=? AND a.status='sealed' AND a.action='Consult' AND l.role='expert'""",(t["id"],))
                        execution,thread,generation,begin,end,events,usage,lease = row
                        assert execution == gate["input"]["sessionId"] and lease == "released"
                        assert all(e["tool"]=="read_file" for e in json.loads(events))
                        assert gate["adviceHash"] and claims.count("/internal/dispatch/"+operation+"/claim")==1
                        report["cases"].append({"task":t["id"],"owner":t["ownerId"],"scenario":t["scenario"],
                            "activity":t["activity"],"outcome":gate["outcome"],"operation":operation,"session":execution,
                            "scheduler":sql(f"SELECT scheduler_id FROM dispatch_outbox WHERE id='{operation}'"),
                            "thread":thread,"generation":generation,"lease":lease,"started":begin,"ended":end,
                            "events":json.loads(events),"usage":json.loads(usage),"advice":t["advice"],
                            "decision":t["answer"],"gate":gate})
                assert len({c["thread"] for c in report["cases"]})==2
                report.update(wallSeconds=time.monotonic()-started, recoveredOperation=expert_id,
                              recoveredScheduler=scheduler_id, lostExpertResultReplayed=True)
                (expert_evidence/"expert-runs.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
                print("Passed subscription expert consultation, reserved and delegated routes, and lost result recovery.",flush=True)
                return
            decision_path = "/api/tasks/" + a["id"] + "/decision"
            choice = {"value": "approve", "questionKey": workspace["questionKey"]}
            click = slack.payload(a["id"])
            assert slack.click(controller, click) == 200 and slack.click(controller, click) == 200
            request(controller, decision_path, dashboard, choice)
            assert sum(a["id"] in m["text"] for _, m in slack.messages) == 1
            wait_for(lambda: state(a)["activity"] == "Delivered", "reserved decision continuation")
            record, = owner(a)["decisions"]
            assert record["actor"] == "Slack TLOCAL/UOPERATOR" and record["decision"]["value"] == "4000"
            from io import BytesIO
            from qualify_pilot import download, evaluation
            portable = evaluation.read_bundle(BytesIO(download(controller, dashboard, a["id"])))
            assert portable["activity"] == "Delivered" and portable["interventions"]["recordedOperatorDecisions"] == 1
            assert portable["costs"]["simulatedEstimatedUsdMicros"] == 18 and portable["waits"]
            assert owner(a)["advice"] == waiting_owner["advice"]
            stop(sender)
            c = create("scope-change")
            stale = dispatch("next")
            sql("UPDATE dispatch_outbox SET first_submission=0 WHERE id='" + stale["id"] + "'")
            assert dispatch("next")["id"] is None and state(c)["activity"] == "Blocked"
            request(controller, "/api/notifications/poll", dashboard, {})
            blocked = wait_for(lambda: next((m for _, m in slack.messages if c["id"] in m["text"]), None), "blocked notice")
            assert all(b["type"] != "actions" for b in blocked["blocks"])
            assert dispatch(stale["id"] + "/claim", {"runId": str(uuid4())})["claim"] is None
            route = "/api/tasks/" + c["id"]
            def recover(note):
                hold = request(controller, route + "/workspace", dashboard)["recovery"]
                body = {"operationId": hold["operationId"], "gateSequence": hold["gateSequence"], "note": note}
                first = request(controller, route + "/recover", dashboard, body)
                assert request(controller, route + "/recover", dashboard, body) == first
                assert first["gates"][-1]["outcome"] == "Resume"
                return hold

            assert recover("Checked the unacknowledged fixture submission.")["kind"] == "Outcome unknown"
            missing = dispatch("next")
            fake_scheduler = str(uuid4())
            dispatch(missing["id"] + "/submitted", {"runId": fake_scheduler})
            sentinel = int(time.time()) + 3600
            sql(f"UPDATE dispatch_watch SET deadline={sentinel} WHERE id='{missing['id']}'")
            # A replay cannot move a recorded deadline. Expire it after this assertion so
            # the independent reconciler cannot race the duplicate acknowledgement.
            dispatch(missing["id"] + "/submitted", {"runId": fake_scheduler})
            assert sql("SELECT deadline FROM dispatch_watch WHERE id='" + missing["id"] + "'") == str(sentinel)
            sql("UPDATE dispatch_watch SET deadline=0 WHERE id='" + missing["id"] + "'")
            stop(api)
            api = controller_start()
            wait_for(lambda: state(c)["activity"] == "Blocked", "controller stall inspection")
            assert recover("Checked the missing scheduler run and retained checkpoint.")["schedulerId"] == fake_scheduler

            for kind in ("Authentication", "Quota", "Budget"):
                ticket = dispatch("next")
                scheduler_id = str(uuid4())
                claim = dispatch(ticket["id"] + "/claim", {"runId": scheduler_id, "sessionId": str(uuid4())})["claim"]
                wait = {"token": claim["token"], "kind": kind}
                if kind in ("Quota", "Authentication"):
                    wait["fundingMode"] = "Subscription"
                try:
                    dispatch(ticket["id"] + "/wait", {**wait, "token": "invalid"})
                    raise AssertionError("Unauthenticated claim parked a task")
                except HTTPError as error:
                    assert error.code == 409
                assert dispatch(ticket["id"] + "/wait", wait)
                assert dispatch(ticket["id"] + "/wait", wait)
                checkpoint = state(c)
                assert checkpoint["run"] is None and checkpoint["activity"] == "Blocked"
                for _ in range(3):
                    assert dispatch("next")["id"] is None
                assert state(c) == checkpoint
                if kind == "Quota":
                    stop(api)
                    api = controller_start()
                    assert state(c) == checkpoint
                if kind in ("Quota", "Authentication"):
                    try:
                        recover("A note alone cannot clear this subscription wait.")
                        raise AssertionError("Missing or stale allowance observation resumed work")
                    except HTTPError as error:
                        assert error.code == 409
                    observed = {"observedUtc":datetime.now(timezone.utc).isoformat(), "auth":"ChatGPT",
                                "windows":[{"minutes":300, "usedPercent":100}]}
                    if kind == "Quota":
                        observation.write_text(json.dumps(observed))
                        try:
                            recover("The window is still exhausted.")
                            raise AssertionError("Exhausted allowance resumed work")
                        except HTTPError as error:
                            assert error.code == 409
                    observed["windows"][0]["usedPercent"] = 20
                    observation.write_text(json.dumps(observed))
                assert recover("Checked the simulated " + kind + " condition.")["kind"] == kind
                try:
                    dispatch(ticket["id"] + "/result", {"token": claim["token"], "output": {}})
                    raise AssertionError("Retired claim supplied a result")
                except HTTPError as error:
                    assert error.code == 409
                assert dispatch(ticket["id"] + "/claim", {"runId": scheduler_id, "sessionId": str(uuid4())})["claim"] is None

            sender = process("dispatch", [*node, "dispatch"], env)
            wait_for(lambda: state(c)["activity"] == "Waiting", "fresh Hatchet attempt after recovery")
            assert len({a["runId"] for a in state(c)["assignments"]}) == len(state(c)["assignments"])
            assert sum(g["outcome"] == "Resume" for g in state(c)["gates"]) == 5
            stop(sender)
            with closing(sqlite3.connect(temp / "leases.sqlite")) as journal:
                rows = journal.execute("SELECT role,state,candidate_hash,files FROM leases WHERE task=?", (b["id"],)).fetchall()
                snapshots = {r[2] for r in rows if r[0] == "writer"}
                assert len(snapshots) == 2 and all(r[1] == "released" for r in rows), rows
                reviews = [json.loads(r[3])["candidate.json"] for r in rows if r[0] == "reviewer"]
                assert reviews == [state(b)["receipts"][-1]["candidateHash"]] and reviews[0] in snapshots, rows
            print("Passed FCT017 leased writer, captured repair and fresh read-only review on Hatchet.", flush=True)
            print("Passed FCT010 stall recovery, runtime waits, retired claims and resumed Hatchet execution.", flush=True)
            print("Passed lost submission, result replay/conflict, controller and PostgreSQL restart, "
                  "waiting-task recovery, repair and single application.", flush=True)
        except Exception:
            if expert_evidence and (temp/"leases.sqlite").exists():
                # The disposable cluster is removed on exit. Keep the authentic task/outbox and lease
                # state locally for recovery. These private dumps are never copied to the progress ledger.
                for name,role in (("factory","factory"),("hatchet","hatchet")):
                    with (expert_evidence/("private-"+name+".sql")).open("wb") as dump:
                        subprocess.run(["docker","exec",pg,"pg_dump","--no-owner","--no-acl","-U",role,"-d",name],
                                       stdout=dump,check=True,timeout=45)
                with closing(sqlite3.connect(temp/"leases.sqlite")) as journal:
                    with closing(sqlite3.connect(expert_evidence/"private-leases.sqlite")) as copy:
                        journal.backup(copy)
                    if journal.execute("SELECT 1 FROM sqlite_master WHERE name='codex_attempts'").fetchone():
                        journal.row_factory = sqlite3.Row
                        attempts = journal.execute("SELECT task,run,action,status,started,ended,thread,generation,usage,events FROM codex_attempts")
                        (expert_evidence/"failed-attempts.json").write_text(json.dumps([dict(r) for r in attempts],indent=2))
            # Diagnostic tails are scrubbed because SDK or HTTP errors can contain credentials.
            for label in ["worker", "dispatch", "controller"]:
                tail = (temp / (label + ".log")).read_text(errors="replace")[-2000:]
                for secret in [factory, scheduler, dashboard, service, tenant, admin]:
                    tail = tail.replace(secret, "[redacted]")
                print(label, tail, flush=True)
            raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-experts",type=Path,metavar="EVIDENCE_DIRECTORY")
    run(parser.parse_args().live_experts)
