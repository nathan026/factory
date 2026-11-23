"""Exercise dashboard discussion on a copy of the delegated and reserved TD13 checkpoints."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.error import HTTPError
from uuid import uuid4

from qualify_dispatch import BRIDGE, DLL, ROOT, laptop_stack, request, stop, wait_for

sys.path.insert(0, str(ROOT / "scripts"))
from codex_worker import allowance, environment


def qualify(checkpoint, evidence, worker_question=False):
    started = time.monotonic()
    evidence.mkdir(parents=True, exist_ok=False)

    def save(name, value):
        (evidence / name).write_text(json.dumps(value, indent=2), encoding="utf-8")

    with ExitStack() as cleanup:
        pgport, grpc, port, base, http, _, password, _, dashboard, service, pg, sql, tenant = laptop_stack(cleanup)
        with (checkpoint / "private-latest.sql").open("rb") as source:
            subprocess.run(["docker", "exec", "-i", pg, "psql", "-Xq", "-v", "ON_ERROR_STOP=1", "-U", "factory",
                            "-d", "factory"], stdin=source, stdout=subprocess.DEVNULL, check=True, timeout=60)
        journal = evidence / "private-leases.sqlite"
        with closing(sqlite3.connect((checkpoint / "private-leases.sqlite").as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(journal)) as copy:
                source.backup(copy)

        def process(label, args, variables):
            env = {k:v for k,v in os.environ.items() if not k.startswith(("FACTORY_", "HATCHET_"))}
            log = cleanup.enter_context((evidence / ("private-" + label + ".log")).open("ab"))
            proc = subprocess.Popen(args, cwd=ROOT, env={**env, **variables}, stdout=log, stderr=log)
            cleanup.callback(stop, proc)
            return proc

        observation_file = evidence / "subscription.json"
        save("funding.json",{"subscriptionFile":str(observation_file)})
        def controller():
            proc = process("controller", ["dotnet", DLL], {
                "FACTORY_PORT":str(port), "FACTORY_POSTGRES":f"Host=127.0.0.1;Port={pgport};Database=factory;Username=factory;Password={password}",
                "FACTORY_STATE":str(evidence), "FACTORY_DISPATCH":"hatchet", "FACTORY_LOCAL_TOKEN":dashboard,
                "FACTORY_FUNDING_PROFILE":str(evidence / "funding.json"),
                "FACTORY_DISPATCH_TOKEN":service, "FACTORY_OWNER_RUNTIME":"codex-subscription", "FACTORY_LEASE_DB":str(journal)})
            wait_for(lambda: request(base, "/api/tasks", dashboard), "restored controller")
            return proc

        def snapshot():
            with (evidence / "private-latest.sql").open("wb") as out:
                subprocess.run(["docker", "exec", pg, "pg_dump", "-U", "factory", "-d", "factory"],
                               stdout=out, check=True, timeout=40)
        cleanup.callback(snapshot)
        api = controller()
        tasks = request(base, "/api/tasks", dashboard)
        accepted, = [t for t in tasks if t["activity"] == "Delivered" and t["scenario"] == "expert-comparison"]
        reserved, = [t for t in tasks if t["activity"] == "Waiting" and t["question"]["reserved"]]
        def route(task, path, body=None):
            return request(base, "/api/tasks/" + task["id"] + "/" + path, dashboard, body)

        message = {"id":str(uuid4()), "text":"When a basket totals precisely fifty dollars, which comparison governs the freight charge?"}
        reserved_before = route(reserved, "workspace")
        guarded = route(reserved, "messages", {**message, "id":str(uuid4())})
        assert "reserved for the operator" in guarded["reply"] and guarded.get("operation") is None
        reserved_after = route(reserved, "workspace")
        assert reserved_after["questionKey"] == reserved_before["questionKey"] and reserved_after["activity"] == "Waiting"
        assert reserved_after["decisions"] == reserved_before["decisions"]
        node = ["node", "--experimental-strip-types", BRIDGE]
        bridge_env = {"FACTORY_LEASE_DB":str(journal), "HATCHET_CLIENT_TOKEN":tenant, "HATCHET_CLIENT_TLS_STRATEGY":"none",
            "HATCHET_CLIENT_HOST_PORT":f"127.0.0.1:{grpc}", "HATCHET_CLIENT_API_URL":http,
            "FACTORY_DISPATCH_TOKEN":service, "FACTORY_CONTROLLER_URL":base}
        assert process("register", [*node,"register"], bridge_env).wait(timeout=45) == 0
        process("worker", [*node,"worker"], bridge_env)
        sender = process("dispatch", [*node,"dispatch"], bridge_env)
        observed, wait = allowance(environment())
        save("admission.json", {"observation":observed, "wait":wait})
        if observed:
            save(observation_file.name,observed)
        if wait:
            save("deferred.json", {"task":accepted["id"], "owner":accepted["ownerId"], "reservedTask":reserved["id"],
                "reservedActivity":reserved_after["activity"], "reservedQuestionKey":reserved_after["questionKey"],
                "guardedReply":guarded["reply"], "question":message["text"], "wait":wait, "nativeAttempts":0,
                "inTaskHumanInterventions":0, "apiSpend":0, "qualificationSeconds":time.monotonic()-started})
            print("Admission deferred before live discussion:", wait)
            return
        admitted = time.monotonic()
        if worker_question:
            task_id = str(uuid4())
            task = request(base, "/api/tasks", dashboard, {"id":task_id,"scenario":"restated-decision"})
            def state():
                return next(t for t in request(base,"/api/tasks",dashboard) if t["id"] == task_id)
            def attempt():
                with closing(sqlite3.connect(journal)) as db:
                    db.row_factory = sqlite3.Row
                    row = db.execute("""SELECT run,thread,generation,status,started,ended,usage,events,source,policy
                        FROM codex_attempts WHERE task=? AND action='AnswerWorker'""",(task_id,)).fetchone()
                    return dict(row) if row else {}
            def retain_wait(row):
                if sender.poll() is not None:
                    process("dispatch-wait", [*node,"dispatch"], bridge_env)
                held = wait_for(lambda: state() if state()["activity"] == "Blocked" else None, "persisted owner wait", 140)
                save("worker-hold.json",{"attempt":row,"recovery":route(held,"workspace")["recovery"]})
                raise AssertionError("Owner answer parked or interrupted")
            def claimed():
                row = attempt()
                if row.get("status") in ("Quota","Authentication","interrupted"):
                    retain_wait(row)
                return row.get("thread")
            wait_for(claimed, "authenticated answering session", 140)
            stop(sender)
            def settled():
                row = attempt()
                if row.get("status") in ("Quota","Authentication","interrupted"):
                    retain_wait(row)
                return row if row.get("status") == "sealed" else None
            native = wait_for(settled, "sealed owner turn", 80)
            save("worker-native.json",native)
            before = state()
            assert before["ownerId"] == task["ownerId"] and before.get("ownerReply") is None
            asking = before["askingRun"]
            assert asking == before["run"]["runId"] and not before.get("humanChoice")
            asked = before["question"]["text"]
            assert asked == "When a basket totals precisely fifty dollars, does it qualify for free shipping?"
            stop(api)
            api = controller()
            assert state() == before
            process("dispatch-recovered", [*node,"dispatch"], bridge_env)
            def terminal_worker():
                current = state()
                return current if current["activity"] in ("Delivered","Blocked","Waiting") else None
            done = wait_for(terminal_worker, "continued implementation, checks and review", 180)
            save("private-worker-task.json",done)
            assert done["activity"] == "Delivered"
            reply = done["ownerReply"]
            assert reply["question"] == asked and reply["askingRun"] == reply["recipientRun"] == asking
            assert done["answer"]["text"] in reply["answer"]
            dispatches = [json.loads(line) for line in sql(f"""
                SELECT json_build_object('action',o.request::json->>'action','run',o.run_id,'hatchet',o.scheduler_id,
                  'session',s.session_id,'applied',i.applied,'reply',o.request::json->'context'->'ownerReply')
                FROM dispatch_outbox o JOIN dispatch_inbox i ON i.id=o.id
                JOIN dispatch_sessions s ON s.operation_id=o.id WHERE o.task_id='{task_id}' AND o.run_id='{asking}'
                """).splitlines()]
            actions = {d["action"]:d for d in dispatches}
            assert len(dispatches) == len(actions) == 3 and set(actions) == {"Ask","AnswerWorker","Implement"}
            assert all(d["applied"] == 1 and d["hatchet"] for d in dispatches)
            assert len({d["session"] for d in dispatches}) == 3 and actions["Implement"]["reply"] == reply
            assert actions["AnswerWorker"]["session"] == native["run"] and native["thread"]
            stop(api)
            controller()
            assert state()["ownerReply"] == reply and route(reserved,"workspace") == reserved_after
            with closing(sqlite3.connect(journal)) as db:
                assert db.execute("SELECT count(*) FROM codex_attempts WHERE task=? AND action='AnswerWorker'",(task_id,)).fetchone()[0] == 1
            native_seconds = native["ended"] - native["started"]
            elapsed = time.monotonic()-admitted
            save("worker-result.json", {"task":task_id,"owner":task["ownerId"],"workerReply":reply,"dispatches":dispatches,
                "nativeOwner":native,"phaseWorkers":"controlled","reservedTask":reserved["id"],"activity":done["activity"],
                "nativeSeconds":native_seconds,"elapsedSeconds":elapsed,"outsideNativeSeconds":elapsed-native_seconds,
                "repairRounds":done["repairs"],"inTaskHumanInterventions":0,"injectedDispatcherStops":1,
                "injectedControllerRestarts":2,"apiSpend":0})
            print("Authenticated owner answered the scheduled worker. Restart and exact-assignment continuation passed.")
            return
        pending = route(accepted, "messages", message)
        assert pending["status"] == "Pending" and route(accepted, "messages", message)["operation"] == pending["operation"]
        def terminal():
            exchange = next(m for m in route(accepted, "workspace")["messages"] if m["id"] == message["id"])
            return exchange if exchange["status"] != "Pending" else None
        answer = wait_for(terminal, "owner discussion resolution", 145)
        elapsed = time.monotonic() - admitted
        save("exchange.json", answer)
        with closing(sqlite3.connect(journal)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute("""
                SELECT a.run,a.thread,a.status,a.started,a.ended,a.usage,a.events,a.source,a.policy,
                       l.generation,l.role,l.state FROM codex_attempts a LEFT JOIN leases l ON l.generation=a.generation
                WHERE a.task=? AND a.action='Discuss' ORDER BY a.started
                """, (accepted["id"],))]
        save("native.json", rows)
        assert answer["status"] == "Answered" and accepted["answer"]["text"] in answer["reply"], answer
        row, = rows
        assert row["role"] == "owner" and row["state"] == "released" and row["status"] == "sealed" and row["thread"]
        stop(api)
        controller()
        assert route(accepted, "messages", message) == answer
        after = route(accepted, "workspace")
        assert after["activity"] == "Delivered" and after["ownerId"] == accepted["ownerId"]
        assert sum(m["id"] == message["id"] for m in after["messages"]) == 1
        assert route(reserved, "workspace") == reserved_after
        repeat = route(accepted, "messages", {**message, "id":str(uuid4())})
        assert (repeat["reply"], repeat["operation"], repeat["status"]) == (answer["reply"], answer["operation"], "Answered")
        try:
            route(accepted, "messages", {**message, "text":"Different question?"})
            raise AssertionError("A replay changed its question")
        except HTTPError as err:
            assert err.code == 409
        with closing(sqlite3.connect(journal)) as db:
            assert db.execute("SELECT count(*) FROM codex_attempts WHERE action='Discuss' AND task=?", (accepted["id"],)).fetchone()[0] == 1
        dispatch = json.loads(sql("""
            SELECT json_build_object('operation',o.id,'hatchetRun',o.scheduler_id,'session',s.session_id,
                'briefVersion',o.request::json->'discussion'->'briefVersion',
                'briefHash',o.request::json->'discussion'->>'briefHash',
                'sourceRevision',o.request::json->'discussion'->>'sourceRevision')
            FROM dispatch_outbox o JOIN dispatch_sessions s ON s.operation_id=o.id
            WHERE o.request::json->>'action'='Discuss'
            """))
        assert dispatch["operation"] == answer["operation"] and dispatch["session"] == row["run"] and dispatch["hatchetRun"]
        native = row["ended"] - row["started"]
        save("result.json", {"observedUtc":datetime.now(timezone.utc).isoformat(), "task":accepted["id"],
            "owner":accepted["ownerId"], "reservedTask":reserved["id"], "reservedQuestionKey":reserved_after["questionKey"],
            "reservedActivity":reserved_after["activity"], "dispatch":dispatch,
            "restartReplayIdentical":True, "conflictingReplayHttp":409, "nativeAttempts":1, "nativeSeconds":native,
            "discussionElapsedSeconds":elapsed, "discussionWaitingSeconds":elapsed-native, "qualificationSeconds":time.monotonic()-started,
            "inTaskHumanInterventions":0, "apiSpend":0})
        print("Authenticated owner discussion, restart replay and reserved authority passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--worker-question", action="store_true", help="Qualify the live owner on an active implementation assignment")
    args = parser.parse_args()
    qualify(args.checkpoint.resolve(), args.evidence.resolve(), args.worker_question)
