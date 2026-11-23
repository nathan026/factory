"""Expose the admitted revision and one editable candidate through a generation-bound MCP server."""
from contextlib import contextmanager
import json
import re
import subprocess
import time
from threading import Lock

from blueprint_tools import FACTORY_FILES, candidate_files, catalog, lease_sources, repository_layout
from environment_leases import fingerprint, output

READ = """const fs = require('node:fs');
const path = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(fs.readFileSync(path, 'utf8')));"""
EDIT = """const fs = require('node:fs'), crypto = require('node:crypto');
const p = JSON.parse(fs.readFileSync(0,'utf8'));
const text = fs.readFileSync(fs.existsSync(p.work) ? p.work : p.seed, 'utf8');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const start = text.indexOf(p.before);
if (p.sha256 !== hash(text) || start < 0 || text.indexOf(p.before,start+1) !== -1) process.exit(2);
const next = text.slice(0,start) + p.after + text.slice(start+p.before.length);
if (!next || next === text || next.includes('\\0') || Buffer.byteLength(next) > 16384) process.exit(2);
fs.writeFileSync(p.work,next,{mode:0o600});
process.stdout.write(hash(next));"""


def spec(name, description, **fields):
    return {"name":name,"description":description,"inputSchema":{"type":"object","properties":fields,
            "required":list(fields),"additionalProperties":False}}


class Workspace:
    def __init__(self, leases, generation, role, source_hash, git_commit, paths, has_candidate, target=FACTORY_FILES["Source"][2], additional_writes=()):
        self.leases, self.generation, self.role = leases, generation, role
        if role not in ("expert","owner") and not re.fullmatch(r"[0-9a-f]{40}",git_commit):
            raise ValueError("Invalid pinned Git revision")
        self.source_hash, self.git_commit, self.paths = source_hash, git_commit, sorted(paths)
        self.target = target
        self.targets = [target, *additional_writes]
        self.locations = {}
        for index, path in enumerate(self.targets):
            single = len(self.targets) == 1
            seed = ("/source/candidate.json" if single else f"/source/factory-candidate/{index}") if has_candidate else "/source/"+path
            work = "/work/"+path.rsplit("/",1)[-1] if single else f"/work/edited-{index}"
            self.locations[path] = (seed,work)
        self.work = self.locations[target][1]
        self.lock, self.events = Lock(), []
        self.edited = set()
        self.observe = None
        self.tools = [spec("read_file","Read the pinned baseline or the current working candidate. Returns content and SHA-256.",
            path={"enum":self.paths},view={"enum":["base","current"]})]
        if role == "writer":
            self.tools.append(spec("replace_text","Edit the current candidate. Supply its current SHA-256 and a unique literal substring.",
                path={"enum":self.targets},sha256={"type":"string"},before={"type":"string"},after={"type":"string"}))

    @property
    def changed(self):
        return bool(self.edited)

    def checkpoint(self):
        if self.role != "writer" or not self.changed:
            raise ValueError("Candidate checkpoint requires an observed edit")
        if len(self.targets) == 1:
            return self.leases.snapshot(self.generation, self.work.rsplit("/", 1)[-1])
        program = """const fs = require('node:fs');
const files = JSON.parse(fs.readFileSync(0,'utf8'));
const packed = Object.fromEntries(files.map(([path,work]) => [path,fs.readFileSync(work,'utf8')]));
fs.writeFileSync('/work/candidate-pack.json',JSON.stringify(packed),{flag:'wx',mode:0o600});"""
        with self.lock:
            output(self.leases.execute(self.generation,program,[(p,self.locations[p][1 if p in self.edited else 0]) for p in sorted(self.targets)]))
            return self.leases.snapshot(self.generation, "candidate-pack.json")

    def call(self, name, args):
        with self.lock:
            tool = next((t for t in self.tools if t["name"] == name), None)
            if (not tool or not isinstance(args,dict) or set(args) != set(tool["inputSchema"]["required"])
                    or not all(isinstance(v,str) for v in args.values()) or args["path"] not in self.paths):
                raise ValueError("Tool or path outside assignment")
            path = args["path"]
            if name == "read_file":
                if args["view"] not in ("current","base"):
                    raise ValueError("Invalid file view")
                location = "/source/" + path
                if path in self.locations and args["view"] == "current":
                    seed, work = self.locations[path]
                    location = work if path in self.edited else seed
                content = json.loads(output(self.leases.execute(self.generation,READ,location)))
                digest = fingerprint(content)
                result = {"content":content,"sha256":digest,"path":path,"view":args["view"],
                          "sourceHash":self.source_hash,"gitCommit":self.git_commit}
            else:
                if path not in self.locations or not args["before"] or any(len(v.encode()) > 16384 for v in args.values()):
                    raise ValueError("Edit outside admitted bounds")
                seed, work = self.locations[path]
                digest = output(self.leases.execute(self.generation,EDIT,{**args,"work":work,"seed":seed}))
                self.edited.add(path)
                result = {"path":path,"sha256":digest}
            self.events.append({"tool":name,"path":path,"view":args.get("view"),"sha256":digest,"at":time.time()})
            if self.observe:
                self.observe(self.events)
            return result

    def dispatch(self, request):
        method, params = request.get("method"), request.get("params",{})
        msg = {"jsonrpc":"2.0","id":request["id"]}
        if method == "initialize":
            result = {"protocolVersion":params["protocolVersion"],"capabilities":{"tools":{}},
                      "serverInfo":{"name":"factory-workspace","version":"1"}}
        elif method == "tools/list":
            result = {"tools":self.tools}
        elif method == "tools/call":
            try:
                value = self.call(params["name"],params["arguments"])
                result = {"content":[{"type":"text","text":json.dumps(value)}]}
            except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError):
                result = {"isError":True,"content":[{"type":"text","text":"Rejected path, edit, digest or lease generation"}]}
        elif method == "resources/list":
            result = {"resources":[]}
        elif method == "resources/templates/list":
            result = {"resourceTemplates":[]}
        elif method == "ping":
            result = {}
        else:
            return {**msg,"error":{"code":-32601,"message":"Unsupported method"}}
        return {**msg,"result":result}


@contextmanager
def open_workspace(leases, order, git_commit, on_release=None, expert_session=None, request_hash=None):
    if order["action"] in ("Consult","Discuss","AnswerWorker"):
        owner = order["action"] in ("Discuss","AnswerWorker")
        role = "owner" if owner else "expert"
        documents = [order["discussion"]["evidence"]] if owner else order["consultation"]["sources"]
        files = {s["path"]:s["content"] for s in documents}
        a = order["assignment"]
        leases.reconcile()
        generation = leases.acquire(a["taskId"],expert_session,role,order["action"],request_hash,files)
        try:
            yield Workspace(leases,generation,role,a["sourceHash"],None,files,False,next(iter(files)))
        finally:
            leases.release(generation)
        return
    layout = repository_layout(order)
    project_files = catalog(order)
    if (project_files is not FACTORY_FILES and not layout) or order["action"] not in ("Implement","Review"):
        raise ValueError("Unsupported repository role")
    writer = order["action"] == "Implement"
    role = "writer" if writer else "reviewer"
    files = lease_sources(order, "candidate.write" if writer else "candidate.review")
    paths = list(files)
    a = order["assignment"]
    candidate = order.get("candidate")
    if not writer and (not candidate or fingerprint(candidate) != a["inputHash"]):
        raise ValueError("Stale review input")
    if candidate is not None:
        if fingerprint(candidate) != a["inputHash"]:
            raise ValueError("Stale candidate checkpoint")
        with leases.transaction() as db:
            sealed = db.execute("SELECT 1 FROM leases WHERE task=? AND role='writer' AND state='released' "
                                "AND outcome_hash IS NOT NULL AND candidate_hash=?", (a["taskId"],a["inputHash"])).fetchone()
        if not sealed:
            raise ValueError("Missing sealed writer checkpoint")
        restored = leases.restore_candidate(a["taskId"],candidate)
        if layout and layout.get("additionalWrites"):
            packed = candidate_files(layout, restored)
            for index, path in enumerate([layout["source"], *layout["additionalWrites"]]):
                files[f"factory-candidate/{index}"] = packed[path]
        else:
            files["candidate.json"] = restored
    leases.reconcile()
    generation = leases.acquire(a["taskId"],a["runId"],role,order["action"],order["context"]["blueprint"]["manifestHash"],files)
    try:
        yield Workspace(leases,generation,role,a["sourceHash"],git_commit,paths,candidate is not None,project_files["Source"][2],
                        layout.get("additionalWrites",[]) if layout else [])
    finally:
        try:
            if on_release:
                on_release("release.start")
        finally:
            leases.release(generation)
        if on_release:
            on_release("release.end")

