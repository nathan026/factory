"""Qualification-only loopback tool. Its supervisor selects the lease, never the model."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import subprocess
import time
from threading import Thread

from qualify_m0 import LABEL


def check(context, lease, run, generation):
    if not re.fullmatch(r"[0-9a-f]{64}", lease) or not re.fullmatch(r"[0-9a-f]{32}", run):
        raise ValueError("Invalid lease identity")
    def docker(*args):
        p = subprocess.run(["docker", "--context", context, *args], capture_output=True, text=True, timeout=15)
        if p.returncode:
            raise RuntimeError("Lease unavailable")
        return p.stdout.strip()
    def validate():
        info = json.loads(docker("inspect", "--type", "container", lease))[0]
        labels = info["Config"]["Labels"]
        if (info["Id"] != lease or labels.get(LABEL) != run or labels.get(LABEL+".role") != "ro"
                or int(labels.get(LABEL+".expires", 0)) <= time.time() or info["State"]["StartedAt"] != generation
                or not info["State"]["Running"] or info["HostConfig"]["NetworkMode"] != "none"):
            raise RuntimeError("Lease unavailable")
    validate()
    result = json.loads(docker("exec", lease, "/bin/sh", "-c", "exec node /work/qualification-test.cjs"))
    validate()  # Discard a read-only result if cancellation or restart crossed the execution.
    if not isinstance(result, dict) or set(result) != {"verified", "marker"} or result["verified"] is not True:
        raise RuntimeError("Controlled repository check failed")
    if not re.fullmatch(r"[0-9a-f]{32}", result["marker"]):
        raise RuntimeError("Invalid qualification marker")
    return result


@contextmanager
def lease_tool(context, lease, run, generation):
    tool = {"name":"verify_fixture", "description":"Run the admitted read-only sample repository checks and return the lease marker.",
            "inputSchema":{"type":"object","properties":{},"additionalProperties":False}}
    def dispatch(req):
        msg = {"jsonrpc":"2.0", "id":req["id"]}
        method = req.get("method")
        if method == "initialize":
            msg["result"] = {"protocolVersion":req["params"]["protocolVersion"], "capabilities":{"tools":{}},
                             "serverInfo":{"name":"factory-qualification", "version":"1"}}
        elif method == "tools/list":
            msg["result"] = {"tools":[tool]}
        elif method == "resources/list":
            msg["result"] = {"resources":[]}
        elif method == "resources/templates/list":
            msg["result"] = {"resourceTemplates":[]}
        elif method == "tools/call":
            params = req["params"]
            try:
                if not isinstance(params, dict) or params.get("name") != tool["name"] or params.get("arguments", {}) != {}:
                    raise ValueError("Invalid assignment")
                result = check(context, lease, run, generation)
                msg["result"] = {"content":[{"type":"text", "text":json.dumps(result)}]}
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                msg["result"] = {"isError":True, "content":[{"type":"text", "text":"Lease or repository check rejected"}]}
        elif method == "ping":
            msg["result"] = {}
        else:
            msg["error"] = {"code":-32601, "message":"Unsupported method"}
        return msg

    with tool_server(dispatch) as endpoint:
        yield endpoint


@contextmanager
def tool_server(dispatch):
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if (self.path != "/mcp" or self.headers.get("Origin") is not None
                    or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
                    or not secrets.compare_digest(self.headers.get("Authorization",""), "Bearer "+token)):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length","0"))
                if not 0 < size <= 65536:
                    raise ValueError()
                req = json.loads(self.rfile.read(size))
                if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
                    raise ValueError()
                response = json.dumps(dispatch(req)).encode() if "id" in req else b""
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            self.send_response(200 if response else 202)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def do_GET(self):
            self.send_error(405)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/mcp", token
        finally:
            server.shutdown()
            thread.join()
