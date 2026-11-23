"""Native CLI transport regression. An unsigned loopback provider scripts each reply; no live journal is written."""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from codex_worker import CLI, environment, turn, turn_packet
from codex_workspace import Workspace
from environment_leases import Leases, fingerprint
from m0_lease_tool import tool_server

TARGET = "fixtures/self-development/task-summary.cjs"
REPORT = None


@contextmanager
def scripted_provider(steps, tools, markers, on_finish=None):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            index = len(requests)
            available = set()
            for tool in body["tools"]:
                if tool["type"]=="namespace":
                    available.update(tool["name"]+"."+name["name"] for name in tool["tools"])
                else:
                    available.add(tool.get("name",tool["type"]))
            safe = set(tools)|{"request_user_input","list_mcp_resource_templates","list_mcp_resources","read_mcp_resource"}
            if (index >= len(steps) or not set(tools)<=available<=safe or
                    index and markers[index-1] not in json.dumps([i for i in body["input"] if i["type"]=="function_call_output"])):
                self.send_error(400)
                return
            requests.append(index)
            if on_finish and index==len(steps)-1:
                on_finish()
                time.sleep(1.5)
            step = steps[index]
            if isinstance(step,tuple):
                name,args = step
                item = {"type":"function_call","namespace":"mcp__factory","name":name,"id":"fc_"+str(index),
                        "call_id":"call_"+str(index),"arguments":json.dumps(args),"status":"completed"}
            else:
                item = {"type":"message","id":"msg_done","role":"assistant","status":"completed",
                        "content":[{"type":"output_text","text":json.dumps(step),"annotations":[]}]}
            response = {"id":"resp_"+str(index),"object":"response","status":"completed","output":[item],
                        "usage":{"input_tokens":12,"output_tokens":3,"total_tokens":15,
                                 "input_tokens_details":{"cached_tokens":2},"output_tokens_details":{"reasoning_tokens":0}}}
            if on_finish and index==len(steps)-1:
                return
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.end_headers()
            for event in [{"type":"response.created","response":dict(response,status="in_progress",output=[])},
                          {"type":"response.output_item.added","output_index":0,"item":dict(item,content=[],status="in_progress")},
                          {"type":"response.output_item.done","output_index":0,"item":item},
                          {"type":"response.completed","response":response}]:
                self.wfile.write(("event: "+event["type"]+"\ndata: "+json.dumps(event)+"\n\n").encode())
    with HTTPServer(("127.0.0.1",0),Handler) as server:
        thread = Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            yield "http://127.0.0.1:"+str(server.server_port)+"/v1"
            assert len(requests)==len(steps)
        finally:
            server.shutdown()
            thread.join()


class TransportTest(unittest.TestCase):
    def test_scope_planning_packet_requests_current_assessment_only_for_new_turns(self):
        planning = {"baseSha":"a"*40,"files":[{"path":"desk.css"}]}
        question = {"text":"Prepare this issue.","evidence":{"path":"owner/accepted.json"},"scopePlanning":planning}
        workspace = SimpleNamespace(role="owner")
        legacy, _, _ = turn_packet(workspace, {"discussion":question}, {})
        self.assertEqual(legacy["required"],["reply","proposal"])
        planning["requireAssessment"] = True
        schema, prompt, instruction = turn_packet(workspace, {"discussion":question}, {})
        self.assertIn("assessment",schema["required"])
        assessment = schema["properties"]["assessment"]
        self.assertFalse(assessment["additionalProperties"])
        for field in ["investigations","decisions"]:
            self.assertIn(field,assessment["required"])
            self.assertEqual(assessment["properties"][field],{"type":"array","items":{"type":"string"}})
        self.assertEqual(prompt["repositoryFiles"],["desk.css"])
        self.assertIn("Read the pinned sources",instruction)
        self.assertIn("return proposal null",instruction)

    def test_fresh_native_cli_tools_with_unsigned_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {**environment(),"CODEX_HOME":str(root/"unsigned")}
            (root/"unsigned").mkdir()
            leases = Leases(root/"leases.sqlite")
            task = str(uuid4())
            base_sha = json.loads((ROOT/"fixtures/factory-summary/factory-policy.json").read_text())["settings"]["base"]
            base = subprocess.check_output(["git","show",base_sha+":"+TARGET],cwd=ROOT).decode()
            before = "const lines = ["
            after = "// unsigned transport probe\n"+before
            expected = base.replace(before,after)
            current = {"path":TARGET,"view":"current"}
            edit = {"path":TARGET,"before":before,"after":after,"sha256":fingerprint(base)}
            order = {"context":{"brief":{"goal":"Exercise the unsigned test transport."},"findings":[],"decisions":[],"blueprint":{"localFeedback":[]}}}
            threads, observations = [], []
            started = time.monotonic()
            for case in ("writer","reviewer","cancel","interrupt"):
                before_case = time.monotonic()
                fault = case in ("cancel","interrupt")
                role = "reviewer" if case=="reviewer" else "writer"
                writer = role=="writer"
                if fault:
                    task = str(uuid4())
                decision = {"decision":"implemented" if writer else "approve","summary":"scripted test response","criterion":"","remedy":""}
                files = {TARGET:base} if writer else {TARGET:base,"candidate.json":candidate}
                generation = leases.acquire(task,str(uuid4()),role,role,"test-only",files)
                try:
                    native_ids = []
                    def observe_transcript(raw):
                        for line in raw.splitlines():
                            event = json.loads(line)
                            if event["type"]=="thread.started":
                                native_ids.append(event["thread_id"])
                    workspace = Workspace(leases,generation,role,"unsigned-packet",base_sha,[TARGET],not writer)
                    steps = [("read_file",current),("replace_text",edit),decision] if writer else [
                             ("read_file",dict(current,view="base")),("read_file",current),decision]
                    tools = ["mcp__factory."+spec["name"] for spec in workspace.tools]
                    def interrupt():
                        self.assertEqual(len(native_ids),1, 'Native identity must already be recorded before interruption')
                        if case=="cancel":
                            (root/"cancelled").touch()
                        else:
                            stop.set()
                            heartbeat.join()
                            (root/"alive").unlink(missing_ok=True)
                    with scripted_provider(steps,tools,[fingerprint(base),fingerprint(expected)],interrupt if fault else None) as fixture, tool_server(workspace.dispatch) as endpoint:
                        popen = subprocess.Popen
                        def unsigned(args,**kwargs):
                            if args[0] != CLI:
                                return popen(args,**kwargs)
                            self.assertIn('model_provider="openai"',args)
                            self.assertIn("--ephemeral",args)
                            config = {"model":"unsigned-test","model_provider":"fixture","model_providers.fixture.name":"Fixture",
                                      "model_providers.fixture.wire_api":"responses","model_providers.fixture.base_url":fixture,
                                      "model_providers.fixture.request_max_retries":0}
                            overrides = [value for key,v in config.items() for value in ("-c",key+"="+json.dumps(v))]
                            return popen([*args[:-1],*overrides,args[-1]],**kwargs)
                        # Only this isolated test rewrites the CLI provider. It has no account or controller credentials.
                        (root/"cancelled").unlink(missing_ok=True)
                        (root/"alive").touch()
                        stop = Event()
                        def pulse():
                            while not stop.wait(1):
                                if (root/"alive").exists():
                                    (root/"alive").touch()
                        heartbeat = Thread(target=pulse)
                        heartbeat.start()
                        try:
                            with patch("codex_worker.subprocess.Popen",side_effect=unsigned):
                                args = (workspace,*endpoint,env,order,{"gitRevision":base_sha},root/"cancelled",observe_transcript)
                                if fault:
                                    with self.assertRaisesRegex(RuntimeError,"interrupted"):
                                        turn(*args)
                                else:
                                    thread,reply,usage = turn(*args)
                        finally:
                            stop.set()
                            heartbeat.join()
                        if not fault:
                            self.assertEqual(reply,decision)
                            self.assertGreater(usage["input_tokens"],0)
                            threads.append(thread)
                    self.assertEqual(len(native_ids),1)
                    self.assertEqual(len(workspace.events),2)
                    if fault:
                        self.assertEqual(workspace.events[-1]["sha256"],fingerprint(expected))
                        leases.release(generation)
                        with self.assertRaises(RuntimeError):
                            leases.snapshot(generation,workspace.work.split("/")[-1])
                        self.assertIsNone(leases.record(generation)["outcome_hash"])
                        continue
                    if writer:
                        candidate = leases.snapshot(generation,workspace.work.split("/")[-1])
                        self.assertEqual(candidate,expected)
                    leases.finish(generation,{"unsignedTest":True})
                finally:
                    leases.release(generation)
                    observations.append({"case":case,"wallSeconds":time.monotonic()-before_case,
                        "nativeThread":native_ids[0] if native_ids else None,
                        "released":leases.record(generation)["state"]=="released",
                        "completedRepositoryOperations":workspace.events,
                        "scriptedResponseUsage":None if fault else usage})
            self.assertEqual(len(set(threads)),2)
            self.assertEqual(len({r["nativeThread"] for r in observations}),4)
            if REPORT:
                report = {"evidenceClass":"real pinned native CLI and Docker; unsigned scripted loopback responses",
                          "authenticatedModelTurns":0,"incrementalApiSpend":0,"manualTaskInterventions":[],
                          "operatorWaitSeconds":0,"hostCost":None,"wallSeconds":time.monotonic()-started,
                          "faults":["cancel signal after observed edit","dispatch heartbeat loss after observed edit"],
                          "observations":observations}
                with REPORT.open("x",encoding="utf-8") as file:
                    json.dump(report,file,indent=2)
                    file.write("\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report",type=Path)
    args, rest = parser.parse_known_args()
    REPORT = args.report
    unittest.main(argv=[sys.argv[0],*rest])
