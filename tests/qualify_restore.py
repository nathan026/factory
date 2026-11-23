"""AT25 PostgreSQL restore into a separate, loopback-only Compose project."""
from contextlib import ExitStack, closing
import argparse
import json
import os
import time
from pathlib import Path
import secrets
import subprocess
import tempfile
from urllib.error import HTTPError
from uuid import uuid4

from qualify_dispatch import API, DLL, ROOT, docker, port, request, stop, wait_for
from test_restore import backup
from environment_leases import Leases, output


def deployment(cleanup, scheduler):
    name = "fct014-" + uuid4().hex[:12]
    env = {**os.environ, "FACTORY_DB_ADMIN": secrets.token_hex(24), "FACTORY_DB_PASSWORD": secrets.token_hex(24),
           "HATCHET_DB_PASSWORD": secrets.token_hex(24), "FACTORY_SCHEDULER_ADMIN": "Aa0!" + secrets.token_hex(24),
           "FACTORY_PG_PORT": str(port()), "FACTORY_GRPC_PORT": str(port()), "FACTORY_SCHEDULER_PORT": str(port())}
    def compose(*args):
        return docker("compose", "-p", name, "-f", str(ROOT / "deploy/laptop.compose.yaml"), "--profile", "dispatch", *args, env=env)
    cleanup.callback(compose, "down", "-v")
    def up():
        compose("up", "-d", "--wait", *(["db", "scheduler"] if scheduler else ["db"]))
        return compose("ps", "-q", "db")
    pg = up()
    assert docker("port", pg, "5432/tcp") == "127.0.0.1:" + env["FACTORY_PG_PORT"]
    denied = docker("exec", pg, "psql", "-XAtq", "-U", "postgres", "-c",
                    "SELECT has_database_privilege('hatchet','factory','connect') OR has_database_privilege('factory','hatchet','connect')")
    assert denied == "f"
    return env, compose, up, pg


def expect(code, call, *args):
    try:
        call(*args)
        raise AssertionError(f"Expected HTTP {code}")
    except HTTPError as error:
        with closing(error):
            assert error.code == code, error.code


def run(scheduler_loss=False,report=None):
    started = time.monotonic()
    with ExitStack() as cleanup:
        temp = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
        source, compose, up, pg = deployment(cleanup, scheduler=True)
        target, target_compose, _, target_pg = deployment(cleanup, scheduler=scheduler_loss)
        assert bool(target_compose("ps", "-q", "scheduler")) == scheduler_loss
        scheduler = compose("ps", "-q", "scheduler")
        def mint(instance):
            token = docker("exec", instance, "./hatchet-admin", "token", "create", "--config", "./config",
                           "--name", "restore-drill").splitlines()[-1].strip()
            return token if token.startswith("eyJ") else None
        tenant = wait_for(lambda:mint(scheduler), "scheduler login", 120)
        def register(settings=source, credential=tenant):
            subprocess.run(["node", "--experimental-strip-types", "bridge.ts", "register"],
                           cwd=ROOT / "src/Factory.Dispatch", check=True, capture_output=True, timeout=45,
                           env={**os.environ, "HATCHET_CLIENT_TOKEN": credential, "HATCHET_CLIENT_TLS_STRATEGY": "none",
                                "HATCHET_CLIENT_HOST_PORT": "127.0.0.1:" + settings["FACTORY_GRPC_PORT"],
                                "HATCHET_CLIENT_API_URL": "http://127.0.0.1:" + settings["FACTORY_SCHEDULER_PORT"],
                                "FACTORY_DISPATCH_TOKEN": "unused-register-only"})
        register()
        def bridge(mode, settings, credential, endpoint):
            proc = subprocess.Popen(["node","--experimental-strip-types","bridge.ts",mode],
                cwd=ROOT/"src/Factory.Dispatch",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                env={**os.environ,**endpoint,"HATCHET_CLIENT_TOKEN":credential,
                     "HATCHET_CLIENT_TLS_STRATEGY":"none","HATCHET_CLIENT_HOST_PORT":"127.0.0.1:"+settings["FACTORY_GRPC_PORT"],
                     "HATCHET_CLIENT_API_URL":"http://127.0.0.1:"+settings["FACTORY_SCHEDULER_PORT"]})
            cleanup.callback(stop,proc)
            return proc
        def start(env, database):
            token, service, http_port = secrets.token_hex(24), secrets.token_hex(24), port()
            url = f"http://127.0.0.1:{http_port}"
            settings = {k: v for k, v in os.environ.items() if not k.startswith("FACTORY_")}
            settings.update(FACTORY_DISPATCH="hatchet", FACTORY_PORT=str(http_port), FACTORY_LOCAL_TOKEN=token,
                            FACTORY_DISPATCH_TOKEN=service, FACTORY_STATE=str(temp), FACTORY_POSTGRES=(
                            f"Host=127.0.0.1;Port={env['FACTORY_PG_PORT']};Database={database};"
                            f"Username=factory;Password={env['FACTORY_DB_PASSWORD']}"))
            proc = subprocess.Popen(["dotnet", str(DLL)], cwd=API, env=settings,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cleanup.callback(stop, proc)
            wait_for(lambda: request(url, "/api/tasks", token) is not None, "controller")
            return proc, lambda path, body=None: request(url, "/api/" + path, token, body), (
                lambda path, body=None: request(url, "/internal/dispatch/" + path, service, body or {})), {"FACTORY_CONTROLLER_URL":url,"FACTORY_DISPATCH_TOKEN":service}
        controller, api, dispatch, _ = start(source, "factory")
        task = str(uuid4())
        api("tasks", {"id": task, "scenario": "scope-change"})
        # Idle reboot of both data services and controller, preserving named volumes and tenant identity.
        stop(controller)
        compose("down")
        pg = up()
        register()
        controller, api, dispatch, endpoint = start(source, "factory")
        assert api("tasks")[0]["id"] == task
        op = dispatch("next")["id"]
        assert op
        # Cut the snapshot after the dispatch intent and before a scheduler acknowledgement.
        run_id = None
        if scheduler_loss:
            sender = bridge("dispatch",source,tenant,endpoint)
            def admitted():
                return docker("exec",pg,"psql","-XAtq","-U","factory","-d","factory","-c",
                    "SELECT scheduler_id FROM dispatch_outbox WHERE id='"+op+"'")
            run_id = wait_for(admitted,"actual Hatchet submission",60)
            stop(sender)
        saved = api("tasks")
        bundle = temp / "backup"
        create_args,restore_args = [],[]
        if scheduler_loss:
            leases = Leases(temp/"leases.sqlite")
            generation = leases.acquire(task,str(uuid4()),"verifier","restore-probe","controlled",{"proof.txt":"pinned"})
            try:
                assert output(leases.execute(generation,"process.stdout.write(require('fs').readFileSync('/source/proof.txt'))"))=="pinned"
                assert "released environment leases" in backup("create",bundle,"--pg-container",pg,
                                                                "--lease-db",leases.db,success=False)
                leases.finish(generation,{"controlled":"pinned"})
            finally:
                leases.release(generation)
            create_args = ["--lease-db",leases.db]
            restore_args = ["--lease-state",temp/"restored-state"]
        backup("create", bundle, "--pg-container", pg,*create_args)
        restored = backup("restore", bundle, "--pg-container", target_pg,*restore_args)
        if scheduler_loss:
            copied = Leases(restored["leaseDb"])
            row = copied.record(generation)
            assert row["state"]=="released" and row["outcome_hash"]==leases.record(generation)["outcome_hash"]
            try:
                copied.finish(generation,{"controlled":"late"})
            except RuntimeError as err:
                assert "Retired" in str(err)
            else:
                raise AssertionError("Restored generation accepted a late result")
        dest_db = restored["location"]
        assert dest_db.startswith("restore_")
        stop(controller)
        if scheduler_loss:
            # Only this drill's two separately named temporary Compose projects are in scope.
            assert docker("exec", pg, "psql", "-XAtq", "-U", "factory", "-d", "factory", "-c",
                         "SELECT count(*) FROM dispatch_inbox WHERE id='"+op+"' AND claim IS NOT NULL")=="0"
            compose("down","-v")
        reader, read, callbacks, _ = start(target, dest_db)
        expected = [{"kind": "Dispatch", "id": op}]
        assert read("tasks") == saved and read("restore")["blockers"] == expected
        expect(423, callbacks, "next")
        expect(423, callbacks, op + "/submitted", {"runId": "old-scheduler"})
        release = {"restoreId": restored["restoreId"], "sourceFenced": True, "note": "Stopped source. No sender or worker was started; retained the original operation ID."}
        if scheduler_loss:
            release["note"] = f"Scheduler accepted {run_id}. No worker claimed {op}. Source services and volumes retired."
        expect(409, read, "restore/release", release)
        retire = {**release, "operationId": op}
        expect(400, read, "restore/retire", {**retire, "sourceFenced": False})
        assert read("restore/retire", retire) and read("restore/retire", retire)
        assert read("restore")["blockers"] == []
        assert read("restore/release", release)["reconciliation"] == release["note"]
        expect(423, callbacks, "next")
        stop(reader)
        _, resumed, callbacks, endpoint = start(target, dest_db)
        assert not resumed("restore")["paused"]
        assert resumed("tasks")[0]["activity"] == "Blocked"
        expect(409, callbacks, op + "/submitted", {"runId": "old-scheduler"})
        hold = resumed(f"tasks/{task}/workspace")["recovery"]
        resumed(f"tasks/{task}/recover", {"operationId": op, "gateSequence": hold["gateSequence"], "note": "Resume the inspected fixture checkpoint."})
        fresh = callbacks("next")["id"]
        assert fresh and fresh != op
        if scheduler_loss:
            expect(409, callbacks, op+"/submitted", {"runId":run_id})
            new_tenant = wait_for(lambda:mint(target_compose("ps","-q","scheduler")), "replacement scheduler login")
            register(target,new_tenant)
            bridge("worker",target,new_tenant,endpoint)
            bridge("dispatch",target,new_tenant,endpoint)
            def awaiting_decision():
                current, = resumed("tasks")
                if current["activity"] in ("Blocked","Delivered"):
                    raise AssertionError("Restored task stopped before its reserved decision: "+current["history"][-1]["text"])
                return current["activity"] == "Waiting"
            wait_for(awaiting_decision, "restored task reached reserved decision",90)
            if report:
                with report.open("x",encoding="utf-8") as f:
                    json.dump({"evidenceClass":"controlled local PostgreSQL, two Hatchet schedulers, scripted workers",
                        "task":task,"retiredOperation":op,"lostSchedulerRun":run_id,"replacementOperation":fresh,
                        "restoreId":restored["restoreId"],"restoredLease":generation,"leaseOutcome":row["outcome_hash"],"staleAcknowledgementHttp":409,"terminalActivity":"Waiting",
                        "elapsedSeconds":time.monotonic()-started,"inTaskHumanActions":0,"authenticatedCodexSessions":0},f,indent=2)
        else:
            assert docker("exec", pg, "psql", "-XAtq", "-U", "factory", "-d", "factory",
                          "-c", "SELECT count(*) FROM restore_history") == "0"
        print("Passed loopback Compose recreation with retained scheduler login, isolated PostgreSQL restore, paused callbacks, fenced retirement and fresh dispatch.", flush=True)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduler-loss",action="store_true")
    parser.add_argument("--report",type=Path)
    args=parser.parse_args()
    run(args.scheduler_loss,args.report)
