"""Pinned Windows CLI transport for laptop qualification. Only the fixed lease tool is registered."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from uuid import uuid4

from qualify_m0 import RUNTIME, require


def laptop_probe(root, tool_url, tool_token, marker, live, evidence):
    cli = RUNTIME / "node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    work = root / "empty-host-workspace"
    work.mkdir()
    state = root / "unsigned-codex-state"
    state.mkdir()
    schema = root / "turn-schema.json"
    schema.write_text(json.dumps({"type":"object", "properties":{"status":{"enum":["needs_input","completed"]},
                      "answer":{"type":"string"}}, "required":["status","answer"], "additionalProperties":False}))
    env = {k:v for k,v in os.environ.items() if k.upper() in {
        "PATH","SYSTEMROOT","WINDIR","COMSPEC","PATHEXT","TEMP","TMP","USERPROFILE","LOCALAPPDATA","APPDATA",
        "SYSTEMDRIVE","HOMEDRIVE","HOMEPATH","PROGRAMDATA","PROGRAMFILES","PROGRAMFILES(X86)"}}
    env["CODEX_EXEC_SERVER_URL"] = "none"
    env["FACTORY_QUALIFICATION_BEARER"] = tool_token
    expected = "codex-cli 0.171.0"
    version = subprocess.check_output([cli, "--version"], env=env, text=True).strip()
    require(version == expected, "Unexpected laptop CLI version")
    base = {
        "approval_policy":"never", "web_search":"disabled", "project_doc_max_bytes":0,
        "features.shell_tool":False, "features.view_image":False, "features.hooks":False, "features.plugins":False,
        "features.multi_agent":False, "features.multi_agent_v2":False, "features.goals":False,
        "features.apps":False, "analytics.enabled":False, "feedback.enabled":False,
        "mcp_servers.factory.url":tool_url, "mcp_servers.factory.bearer_token_env_var":"FACTORY_QUALIFICATION_BEARER",
        "mcp_servers.factory.enabled_tools":["verify_fixture"], "mcp_servers.factory.required":True,
        "mcp_servers.factory.default_tools_approval_mode":"approve",
    }
    def invoke(prompt, config, child_env, thread=None):
        args = [cli, "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
                "--sandbox", "read-only", "--json", "--output-schema", schema]
        for key, value in (base | config).items():
            args.extend(["-c", key + "=" + json.dumps(value)])
        if thread:
            args.extend(["resume", thread])
        p = subprocess.run(args + ["-"], cwd=work, env=child_env, text=True, encoding="utf-8",
                           errors="replace", input=prompt, capture_output=True, timeout=95)
        events = [json.loads(s) for s in p.stdout.splitlines() if s.startswith("{")]
        if p.returncode or not any(e.get("type") == "turn.completed" for e in events):
            reason = (p.stderr + p.stdout).lower()
            for phrase in ["quota", "rate limit", "401 unauthorized", "403 forbidden", "mcp startup failed",
                           "unknown variant", "stream disconnected", "model is not supported", "refresh token", "timed out"]:
                if phrase in reason:
                    raise RuntimeError("Pinned CLI turn failed: " + phrase)
            raise RuntimeError("Pinned CLI turn failed before completion (exit " + str(p.returncode) + ")")
        ids = [e["thread_id"] for e in events if e["type"] == "thread.started"]
        items = [e["item"] for e in events if e["type"] == "item.completed"]
        messages = [i["text"] for i in items if i["type"] == "agent_message"]
        require(len(ids) == 1 and messages, "Missing thread identity or structured output")
        for i in items:
            if i["type"] in {"command_execution","file_change","web_search","collab_tool_call"}:
                raise RuntimeError("Unexpected native tool on trusted host")
        usage = next(e for e in reversed(events) if e.get("type") == "turn.completed").get("usage") or {}
        def counter(name):
            value = usage.get(name)
            return value if type(value) is int and value >= 0 else None
        evidence.setdefault("turnUsage", []).append({
            "observedUtc":datetime.now(timezone.utc).isoformat(),
            "fundingMode":"subscription" if config.get("forced_login_method") == "chatgpt" else "loopback",
            "inputTokens":counter("input_tokens"), "cachedInputTokens":counter("cached_input_tokens"),
            "outputTokens":counter("output_tokens")})
        return ids[0], json.loads(messages[-1]), items

    observed = {"requests":0}
    allowed = {"mcp__factory.verify_fixture", "request_user_input", "list_mcp_resource_templates",
               "list_mcp_resources", "read_mcp_resource"}
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            items = payload.get("tools", [])
            catalog = []
            for t in items:
                if t["type"] == "namespace":
                    catalog.extend(t["name"] + "." + sub["name"] for sub in t["tools"])
                else:
                    catalog.append(t.get("name", t["type"]))
            observed["catalog"] = sorted(catalog)
            observed["requests"] += 1
            if not set(catalog) <= allowed or "mcp__factory.verify_fixture" not in catalog or observed["requests"] > 2:
                self.send_error(400)
                return
            if observed["requests"] == 1:
                item = {"type":"function_call","id":"fc_qualify","namespace":"mcp__factory",
                        "name":"verify_fixture","call_id":"call_qualify","arguments":"{}","status":"completed"}
            else:
                results = [x for x in payload["input"] if x.get("type") == "function_call_output"
                           and x.get("call_id") == "call_qualify"]
                observed["leaseReceipt"] = len(results) == 1 and marker in json.dumps(results[0])
                output = {"status":"completed","answer":"fixture"}
                item = {"type":"message","role":"assistant","id":"msg_qualify","status":"completed",
                        "content":[{"type":"output_text","text":json.dumps(output),"annotations":[]}]}
            response = {"id":"resp_qualify","object":"response","output":[item],"status":"completed",
                        "usage":{"input_tokens":12,"output_tokens":3,"total_tokens":15,
                                 "input_tokens_details":{"cached_tokens":2},"output_tokens_details":{"reasoning_tokens":0}}}
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.end_headers()
            for event in [{"type":"response.created","response":dict(response, status="in_progress", output=[])},
                          {"type":"response.output_item.added","output_index":0,"item":dict(item,content=[],status="in_progress")},
                          {"type":"response.output_item.done","output_index":0,"item":item},
                          {"type":"response.completed","response":response}]:
                self.wfile.write(("event: "+event["type"]+"\ndata: "+json.dumps(event)+"\n\n").encode())

    with HTTPServer(("127.0.0.1", 0), Fixture) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            invoke("Return the structured fixture response", {
                "model":"factory-test","model_provider":"qualification","model_providers.qualification.name":"Fixture",
                "model_providers.qualification.wire_api":"responses",
                "model_providers.qualification.base_url":f"http://127.0.0.1:{server.server_port}/v1",
                "model_providers.qualification.request_max_retries":0}, env | {"CODEX_HOME":str(state)})
        finally:
            server.shutdown()
            thread.join()
    evidence.update(version=version, offeredTools=observed["catalog"])
    require(observed.get("leaseReceipt") is True, "Pinned CLI did not receive the expected marker through the lease tool")
    evidence["fixtureToolRoundTrip"] = True
    usage = evidence["turnUsage"][-1]
    require(usage["fundingMode"] == "loopback" and usage["inputTokens"] > 0 and usage["outputTokens"] > 0,
            "Pinned CLI did not report input and output usage from the qualification provider")
    if not live:
        return
    if os.environ.get("CODEX_HOME"):
        env["CODEX_HOME"] = os.environ["CODEX_HOME"]  # Let the CLI use its own existing sign-in on this laptop.
    config = {"forced_login_method":"chatgpt", "model_provider":"openai"}
    def check_auth():
        status = subprocess.run([cli, "-c", 'forced_login_method="chatgpt"', "login","status"], env=env, cwd=work,
                                capture_output=True,text=True,timeout=12)
        require(status.returncode == 0 and (status.stdout+status.stderr).strip().lower()=="logged in using chatgpt",
                "Existing laptop CLI ChatGPT sign-in required")
    check_auth()
    evidence["authMode"] = "ChatGPT"
    secret = uuid4().hex
    first, answer, _ = invoke("Remember this opaque marker "+secret+
        ". Return status needs_input and ask whether to verify the sample. Use no tools.",config,env)
    require(answer["status"]=="needs_input" and answer["answer"].strip(), "Missing structured question")
    evidence["structuredQuestion"] = True
    check_auth()
    resumed, answer, items = invoke("Approved. Call the factory verify_fixture tool once. Return status completed and "
        "answer containing the original opaque marker, a single space, and the marker returned by that tool.",config,env,first)
    require(first == resumed and answer == {"status":"completed","answer":secret+" "+marker}, "Continuation or receipt mismatch")
    calls = [i for i in items if i["type"] == "mcp_tool_call"]
    require(len(calls)==1 and calls[0].get("server")=="factory" and calls[0].get("tool")=="verify_fixture"
            and not calls[0].get("error"), "Missing lease-bound tool call")
    evidence["resumedWithLeaseReceipt"] = True
    check_auth()
    fresh, answer, _ = invoke("If a previous user turn gave you an opaque marker, return it. Otherwise return "
        "status completed and answer fresh. Use no tools.",config,env)
    require(fresh != first and answer == {"status":"completed","answer":"fresh"}, "Fresh thread check failed")
    evidence["freshThread"] = True
