"""Exercise the production Hatchet bridge across the former 60-second wall without a model session."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from qualify_dispatch import BRIDGE, laptop_stack, stop, wait_for


def qualify():
    with ExitStack() as cleanup:
        _, grpc, port, controller, http, _, _, _, _, service, _, _, tenant = laptop_stack(cleanup)
        temp = Path(cleanup.enter_context(tempfile.TemporaryDirectory(dir=BRIDGE.parent)))
        code = temp / "src/Factory.Dispatch/bridge.ts"
        code.parent.mkdir(parents=True)
        shutil.copyfile(BRIDGE, code)
        adapter = temp / "scripts/codex_worker.py"
        adapter.parent.mkdir()
        # Exercise the deadline and closed-out wait protocol. The stub cannot emit a worker result.
        adapter.write_text("""import json,os,sys,time
order=json.load(sys.stdin)
native_end=int(sys.argv[2])/1000
hard_end=int(os.environ['FACTORY_CODING_END_MS'])/1000
assert order['action']=='Implement' and 10 <= hard_end-native_end <= 20
assert 65 < native_end-time.time() <= 80
time.sleep(64)
assert time.time()<native_end
print(json.dumps({'wait':'Coding timeout','fundingMode':'Subscription'}))
""")
        operation = uuid4().hex + ".Implement"
        state = {"scheduler":None, "terminal":None}

        class Controller(BaseHTTPRequestHandler):
            def do_POST(self):
                assert self.headers["Authorization"] == "Bearer " + service
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                route = self.path.removeprefix("/internal/dispatch/")
                if route == "next":
                    result = {"id":None if state["terminal"] else operation, "schedulerId":state["scheduler"]}
                elif route == operation + "/claim":
                    result = {"claim":{"token":"test-claim", "order":{"action":"Implement"},
                                       "workerMode":"codex-subscription", "codingSeconds":90}}
                elif route == operation + "/submitted":
                    state["scheduler"] = msg["runId"]
                    result = True
                elif route in (operation + "/wait", operation + "/failed", operation + "/result"):
                    state["terminal"] = (route.split("/")[-1],msg)
                    result = True
                else:
                    raise AssertionError(route)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1",port),Controller)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cleanup.callback(server.server_close)
        cleanup.callback(server.shutdown)
        env = {k:v for k,v in os.environ.items() if not k.startswith(("OPENAI_", "CODEX_", "FACTORY_", "HATCHET_"))}
        env.update(FACTORY_LEASE_DB=str(temp/"unused.sqlite"), FACTORY_CONTROLLER_URL=controller, FACTORY_DISPATCH_TOKEN=service,
                   HATCHET_CLIENT_TOKEN=tenant, HATCHET_CLIENT_TLS_STRATEGY="none",
                   HATCHET_CLIENT_HOST_PORT=f"127.0.0.1:{grpc}", HATCHET_CLIENT_API_URL=http)
        subprocess.run(["node","--experimental-strip-types",str(code),"register"],env=env,check=True,
                       stdout=subprocess.DEVNULL,timeout=30)
        for mode in ("worker","dispatch"):
            log = cleanup.enter_context((temp/(mode+".log")).open("wb"))
            proc = subprocess.Popen(["node","--experimental-strip-types",str(code),mode],env=env,stdout=log,stderr=log)
            cleanup.callback(stop,proc)
        assert wait_for(lambda: state["terminal"],"bounded terminal callback",120) == (
            "wait",{"token":"test-claim","kind":"Coding timeout","fundingMode":"Subscription"})
        print("Real Hatchet bridge passed the former 60-second boundary and delivered the controlled wait.")


if __name__ == "__main__":
    qualify()
