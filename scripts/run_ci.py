"""Run the controller's pinned verification jobs on the local Docker engine."""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

from blueprint_tools import candidate_files, catalog, lease_sources, repository_rules
from environment_leases import Leases


RUN_VERIFIER = """const fs = require('node:fs');
const read = name => fs.readFileSync('/source/' + name, 'utf8');
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = Object.fromEntries(Object.entries(request.paths).map(([key,path])=>[key,read(path)]));
const input = JSON.stringify({...request, source, candidate:read('candidate.json'), cases:source.cases});
const result = require('node:child_process').spawnSync(process.execPath, ['/source/verify.cjs'],
    {input, timeout:15000, maxBuffer:4096});
if (result.error || result.signal || result.status === null) process.exit(2);
process.stdout.write(result.stdout); process.stderr.write(result.stderr); process.exit(result.status);"""


# The deployment supplies argv. Commands run as the lease UID against an immutable copy of the candidate tree.
RUN_SUITE = """const fs = require('node:fs'), child = require('node:child_process');
const request = JSON.parse(fs.readFileSync(0,'utf8'));
const read = path => fs.readFileSync('/source/'+path,'utf8');
const source = Object.fromEntries(Object.entries(request.paths).map(([key,path])=>[key,read(path)]));
const deadline = Date.now()+15000;
let last;
for (const path of request.targets) {
    const input = JSON.stringify({selector:request.selector,decision:request.decision,candidate:read(path),cases:source.cases,source});
    for (const command of request.commands) {
        const ms = deadline-Date.now();
        if (ms<=0 || command[0]!=='node') process.exit(2);
        last = child.spawnSync(process.execPath,command.slice(1),{cwd:'/source',input,timeout:ms,maxBuffer:4096});
        if (last.error || last.signal || ![0,1].includes(last.status)) process.exit(2);
        if (last.status) {
            process.stdout.write(last.stdout); process.stderr.write(last.stderr); process.exit(last.status);
        }
    }
}
process.stdout.write(last.stdout); process.stderr.write(last.stderr);"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post(controller, token, route, body):
    target = urlsplit(controller)
    if (target.scheme != "http" or not ipaddress.ip_address(target.hostname).is_loopback
            or target.username or target.password or target.path not in ("", "/") or target.query or target.fragment):
        raise ValueError("CI requires a loopback controller URL")
    request = Request(controller.rstrip("/") + "/internal/ci/" + route, data=json.dumps(body).encode(),
                      headers={"Authorization":"Bearer " + token, "Content-Type":"application/json"})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
        return json.load(response)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def execute(job, leases=None):
    leases = leases or Leases()
    binding, order, script = job["binding"], job["order"], job["script"]
    if (digest(script.encode()) != binding["scriptHash"] or digest(order["candidate"].encode()) != binding["candidateHash"]
            or not re.fullmatch(r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}", binding["environment"])):
        raise ValueError("CI input differs from the sealed execution")
    deadline = job.get("deadlineUnix")
    grant = (order.get("context") or {}).get("blueprint")
    if grant is not None and ("checks.run" not in grant["tools"] or grant["manifestHash"] != binding["policyHash"] or deadline is None):
        raise ValueError("CI tool is outside the admitted project grant")
    def remaining():
        if deadline is None:
            return 20
        seconds = deadline - time.time()
        if seconds <= 0:
            raise RuntimeError("Project CI round deadline expired.")
        return min(20, seconds)

    checks = []
    files = lease_sources(order, "checks.run")
    rules = repository_rules(order)
    layout = rules["repositoryLayout"] if rules else None
    if layout:
        # Only map the verified candidate into the approved write path. Other files retain their admitted bytes.
        if (binding["candidateHash"] != order["assignment"]["inputHash"] or binding["revisionKind"] != "git"
                or binding["revision"] != rules["settings"]["base"]
                or binding["environment"] != rules["environment"] or binding["scriptHash"] != rules["scriptHash"]
                or order["assignment"]["requiredChecks"] != rules["policy"]["requiredChecks"]):
            raise ValueError("Repository commands require an admitted Git revision")
        files.update(candidate_files(layout, order["candidate"]))
    else:
        files.update({"candidate.json": order["candidate"], "verify.cjs": script})
    for selector in order["assignment"]["requiredChecks"]:
        remaining()
        generation = leases.acquire(binding["taskId"], binding["executionId"], "verifier", selector,
                                    binding["policyHash"], files, binding["environment"])
        try:
            cid = leases.record(generation)["cid"]
            # Read the immutable source and verifier from the current lease. Only the
            # selector and admitted decision cross stdin when the repository script runs.
            packet = {"selector":selector, "decision":order["answer"], "paths":{key:path for _,key,path in catalog(order).values()}}
            program = RUN_VERIFIER
            if layout:
                program = RUN_SUITE
                packet["commands"] = layout["commands"]
                packet["targets"] = [layout["source"], *layout.get("additionalWrites", [])]
            run = (leases.execute(generation, program, packet, timeout=remaining()) if deadline is not None
                   else leases.execute(generation, program, packet))
            code = run.returncode
            if code not in (0, 1) or run.stderr.strip() or not run.stdout.strip() or len(run.stdout) > 4096:
                raise RuntimeError("CI check did not return a completed test outcome: " + selector)
            check = {"id":selector, "assertion":run.stdout.decode().strip(), "exitCode":code,
                     "containerId":cid, "outputHash":digest(run.stdout + b"\x00" + run.stderr)}
            leases.finish(generation, check)
            checks.append(check)
        finally:
            leases.release(generation)
    remaining()
    return {"token":job["token"], "binding":binding, "checks":checks}


def serve_once(controller, token, leases=None):
    leases = leases or Leases()
    for generation, reason in leases.reconcile().items():
        print(f"Lease {generation} remains quarantined: {reason}", file=sys.stderr)
    job = post(controller, token, "claim", {})["job"]
    if job is None:
        return False
    started = time.time_ns() // 1_000_000
    try:
        result = execute(job, leases)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if isinstance(error, RuntimeError):
            reason = str(error)
        elif isinstance(error, subprocess.TimeoutExpired):
            reason = "Docker verification exceeded its time limit."
        else:
            reason = "CI could not execute or inspect the admitted verification environment."
        post(controller, token, job["id"] + "/unavailable",
             {"token":job["token"], "executionId":job["binding"]["executionId"], "reason":reason})
        raise RuntimeError(reason) from error
    finally:
        if os.getenv('FACTORY_OPERATION_TIMING') == '1':
            print(json.dumps({'operation':job['id'], 'started':started, 'ended':time.time_ns() // 1_000_000}), flush=True)
    for attempt in range(3):
        try:
            post(controller, token, job["id"] + "/result", result)
            return True
        except HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
        time.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    controller = os.environ.get("FACTORY_CONTROLLER_URL", "http://127.0.0.1:5187")
    token = os.environ["FACTORY_CI_TOKEN"]
    leases = Leases()
    while True:
        try:
            worked = serve_once(controller, token, leases)
        except (RuntimeError, URLError, TimeoutError, ConnectionError) as error:
            if args.once or isinstance(error, HTTPError) and error.code in (401, 403):
                raise
            print("CI provider deferred: " + str(error), file=sys.stderr)
            time.sleep(2)
            continue
        if args.once:
            break
        if not worked:
            time.sleep(1)
