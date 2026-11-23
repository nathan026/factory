"""Exercise operator intake through the browser, local Git, PostgreSQL and Hatchet."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from github_fixture import GitHubFixture
from qualify_dispatch import API, BRIDGE, DLL, ROOT, laptop_stack, request, stop, wait_for


def qualify():
    started = time.monotonic()
    with ExitStack() as cleanup:
        tmp = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
        git = GitHubFixture(tmp, ROOT / "fixtures/shipping/repository")
        cleanup.callback(git.close)
        number = 41
        profile = tmp / "intake.json"
        profile.write_text(json.dumps({"repository":git.repo, "repositoryId":git.repo_id, "baseBranch":"main",
                                      "issues":[git.add_issue(number)], "operatorStart":True, "apiUrl":git.url}))
        pg, grpc, port, url, http, _, role, _, desk, dispatcher, _, sql, tenant = laptop_stack(cleanup)
        env = {k:v for k,v in os.environ.items() if not k.startswith(("FACTORY_", "HATCHET_", "GITHUB_", "OPENAI_", "CODEX_"))}
        env.update(FACTORY_PORT=str(port), FACTORY_PROJECT="shipping/v1", FACTORY_STATE=str(tmp),
                   FACTORY_DISPATCH="hatchet", FACTORY_LOCAL_TOKEN=desk, FACTORY_DISPATCH_TOKEN=dispatcher,
                   FACTORY_POSTGRES=f"Host=127.0.0.1;Port={pg};Database=factory;Username=factory;Password={role}",
                   FACTORY_GITHUB_PROJECT=str(profile))
        log = cleanup.enter_context((tmp / "controller.log").open("w"))
        controller = subprocess.Popen(["dotnet", str(DLL)], cwd=API, env=env, stdout=log, stderr=log)
        cleanup.callback(stop, controller)
        wait_for(lambda: request(url, "/api/dashboard/scenarios", desk), "task desk")
        assert request(url, f"/api/github/issues/{number}/sync", desk, {})["taskId"] is None
        assert request(url, "/api/tasks", desk) == []
        assert sql("select count(*) from dispatch_outbox") == "0"
        subprocess.run(["node", "browser-issue-start.mjs", url, git.repo, str(number)], cwd=ROOT/"src/Factory.Web",
                       env=env, check=True, timeout=75)
        task, = request(url, "/api/tasks", desk)
        assert task["phase"] == "Planning"
        bridge = ["node", "--experimental-strip-types", BRIDGE.name]
        bridge_env = {**env, "HATCHET_CLIENT_TOKEN":tenant, "HATCHET_CLIENT_TLS_STRATEGY":"none",
                      "HATCHET_CLIENT_HOST_PORT":f"127.0.0.1:{grpc}", "HATCHET_CLIENT_API_URL":http,
                      "FACTORY_CONTROLLER_URL":url}
        subprocess.run([*bridge,"register"], cwd=BRIDGE.parent, env=bridge_env, check=True,
                       stdout=log, stderr=log, timeout=30)
        dispatch = subprocess.Popen([*bridge,"dispatch"], cwd=BRIDGE.parent, env=bridge_env, stdout=log, stderr=log)
        cleanup.callback(stop, dispatch)
        run = wait_for(lambda: sql("select scheduler_id from dispatch_outbox where scheduler_id is not null"),
                       "planning submission", 30)
        query = 'import {Hatchet} from "@hatchet-dev/typescript-sdk"; const {run}=await Hatchet.init().runs.get(process.argv[1]); console.log(JSON.stringify([run.metadata.id,run.status]));'
        output = subprocess.check_output(["node","--input-type=module","-e",query,run], cwd=BRIDGE.parent,
                                         env=bridge_env, timeout=12, text=True)
        run_id, state = json.loads(output.strip().splitlines()[-1])
        assert run_id == run and state in ("QUEUED","PENDING","RUNNING")
        assert sql("select count(*) from dispatch_sessions") == "0"
        assert not git.writes
        return {"fixture":f"{git.repo}#{number}", "task":task["id"], "phase":task["phase"], "schedulerRun":run,
                "schedulerStatus":state, "workerSessions":0, "providerWrites":0,
                "elapsedSeconds":round(time.monotonic()-started, 2)}


if __name__ == "__main__":
    print(json.dumps(qualify()))
