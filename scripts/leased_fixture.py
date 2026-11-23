"""Apply a controlled writer proposal or inspect the admitted candidate in a fresh role lease."""
import json
import sys

from blueprint_tools import catalog, lease_sources, repository_rules
from environment_leases import Leases, fingerprint, output

WRITE = """
const fs = require('node:fs');
const proposal = JSON.parse(fs.readFileSync(0, 'utf8'));
const original = fs.readFileSync('/source/' + proposal.path, 'utf8');
if (original !== proposal.base) throw Error('Source mismatch');
fs.writeFileSync('/work/' + proposal.filename, proposal.value, {flag:'wx',mode:0o600});
"""
REVIEW = """
const fs = require('node:fs');
const crypto = require('node:crypto');
const bytes = fs.readFileSync('/source/candidate.json');
const rule = JSON.parse(bytes);
const threshold = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify({hash:crypto.createHash('sha256').update(bytes).digest('hex'),
    matches:rule.thresholdCents === Number(threshold) && rule.comparison === 'greaterOrEqual'}));
"""


def apply(leases, order, result):
    if order["assignment"]["policyVersion"] == "factory-summary/v2" or repository_rules(order) is not None:
        raise ValueError("Scripted execution is forbidden for live workers")
    role = {"Implement":"writer", "Review":"reviewer"}[order["action"]]
    assignment, source = order["assignment"], order["source"]
    path = catalog(order)["Source"][2]
    filename = path.rsplit("/", 1)[-1]
    tool = "candidate.write" if role == "writer" else "candidate.review"
    files = lease_sources(order, tool)
    if role == "reviewer":
        if fingerprint(order["candidate"]) != assignment["inputHash"]:
            raise RuntimeError("Review candidate differs from the admitted assignment")
        files["candidate.json"] = leases.restore_candidate(assignment["taskId"], order["candidate"])
    policy = (order.get("context") or {}).get("policy") or {"version":assignment["policyVersion"], "requiredChecks":assignment["requiredChecks"]}
    blocked = leases.reconcile()
    if blocked:
        print("Unresolved environment quarantines: " + ", ".join(blocked), file=sys.stderr)
    generation = leases.acquire(assignment["taskId"], assignment["runId"], role, order["action"],
                       fingerprint(json.dumps(policy, sort_keys=True)), files)
    try:
        if role == "writer":
            written = leases.execute(generation, WRITE, {"base":source["source"], "value":result["output"], "path":path, "filename":filename})
            written.check_returncode()
            candidate = leases.snapshot(generation, filename)
            if candidate != result["output"]:
                raise RuntimeError("Writer snapshot differs from the controlled proposal")
            result["output"] = candidate
        else:
            if assignment["policyVersion"] == "factory-summary/v1":
                digest = output(leases.execute(generation, "process.stdout.write(require('crypto').createHash('sha256').update(require('fs').readFileSync('/source/candidate.json')).digest('hex'))"))
                finding = order["context"]["findings"][-1]
                observed = {"hash":digest, "matches":finding["candidateHash"] == digest and all(c["passed"] for c in finding["checks"])}
            else:
                observed = json.loads(output(leases.execute(generation, REVIEW, order["answer"]["value"])))
            review = result["output"]
            if observed["hash"] != review["candidateHash"] or (review["approved"] and not observed["matches"]):
                raise RuntimeError("Read-only inspection differs from the controlled review")
        leases.finish(generation, result)
        return result
    finally:
        leases.release(generation)


if __name__ == "__main__":
    payload = json.load(sys.stdin.buffer)
    print(json.dumps(apply(Leases(), payload["order"], payload["result"])))
