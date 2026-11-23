"""Collect D002 observations and render a read-only FCT016 retrospective."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from io import BytesIO
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from uuid import UUID
from zipfile import ZipFile, BadZipFile

KINDS = ("requirements", "reserved-decision", "outcome-review", "avoidable", "other-necessary")
ACTIVITIES = ("requirements", "steering", "question-resolution", "repair", "review", "other")
OUTCOMES = ("delivered", "failed", "blocked", "in-progress", "unknown")
TOKEN_MEASURES = ("subscriptionInputTokens", "subscriptionCachedInputTokens", "subscriptionOutputTokens")
MEASURES = ("elapsedSeconds", "coldStartSeconds", "warmStartSeconds", "teardownSeconds", "waitSeconds",
            "workerCpuSeconds", "peakWorkerMemoryBytes", "runtimeCostUsd", "attributableInfrastructureCostUsd",
            "apiBilledUsd", "repairAttempts", *TOKEN_MEASURES)
LIMIT = 16 * 1024 * 1024


def require(ok, message):
    if not ok:
        raise ValueError(message)


def present(value):
    return isinstance(value, str) and bool(value.strip())


def instant(value):
    dt = datetime.fromisoformat(value)
    require(dt.utcoffset() is not None, "Timestamps must include a UTC offset")
    return dt


def initialize(study):
    for group in ("observations", "controlled"):
        (study / group).mkdir(parents=True, exist_ok=True)


def publish(path, data):
    # Link a finished file into place without overwriting another operator's observation.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stage:
        tmp = Path(stage.name)
        try:
            stage.write(data)
            stage.flush()
            os.fsync(stage.fileno())
        except BaseException:
            stage.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink()


def new_record(study, workflow, task, scope, match, boundary):
    require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", task), "Use a short task ID containing letters, digits, '-' or '_'")
    require(present(scope), "Supply the task scope")
    record = dict(workflow=workflow, id=task, scope=scope, match=match, boundary=boundary,
                  reference=None, authorization=None, outcome="unknown", acceptanceEvidence=None,
                  quality="unknown", security="unknown", independentReview=None, acceptedDraftPr=None,
                  substantiveRepairs=None, failedVerificationEvidence=None, attention=None, unattended=[], tags=[],
                  measurements={k:None for k in MEASURES}, measurementEvidence=None)
    path = study / "observations" / f"{workflow}-{task}.json"
    publish(path, (json.dumps(record, indent=2) + "\n").encode())
    return path


def validate(record):
    workflow, task = record["workflow"], record["id"]
    require(workflow in ("baseline", "pilot") and present(task) and present(record["scope"]), "Invalid workflow, task ID or scope")
    require(record["outcome"] in OUTCOMES, "Unknown outcome")
    for key in ("quality", "security"):
        require(record[key] in ("pass", "fail", "unknown"), f"Invalid {key} assessment")
    for key in ("reference", "authorization", "acceptanceEvidence", "independentReview", "acceptedDraftPr", "match", "boundary"):
        require(record[key] is None or present(record[key]), f"{key} needs a reference or null")
    require(record.get("failedVerificationEvidence") is None or present(record["failedVerificationEvidence"]), "Invalid failed verification reference")
    measures = record.get("measurements", {})
    require(isinstance(measures, dict) and set(measures) <= set(MEASURES), "Unknown cost or timing measure")
    for value in measures.values():
        require(value is None or (type(value) in (int,float) and math.isfinite(value) and value >= 0), "Invalid cost or timing")
    for key in (*TOKEN_MEASURES, "peakWorkerMemoryBytes", "repairAttempts"):
        require(measures.get(key) is None or type(measures[key]) is int, f"{key} requires a whole count")
    if measures.get("subscriptionCachedInputTokens") is not None and measures.get("subscriptionInputTokens") is not None:
        require(measures["subscriptionCachedInputTokens"] <= measures["subscriptionInputTokens"], "Cached input exceeds total input")
    if any(v is not None for v in measures.values()):
        require(present(record.get("measurementEvidence")), "Supply evidence for observed costs or timings")
    repairs = record["substantiveRepairs"]
    require(repairs is None or type(repairs) is int and repairs >= 0, "Invalid repair count")
    require(isinstance(record["tags"], list) and set(record["tags"]) <= {"deliberate-blocker", "self-development"}, "Invalid pilot tags")
    logs = record["attention"]
    require(logs is None or isinstance(logs, list), "attention must be null or a complete list")
    for log in logs or []:
        minutes = log["minutes"]
        require(type(minutes) in (float, int) and math.isfinite(minutes) and minutes >= 0 and
                log["kind"] in KINDS and present(log["reason"]), "Invalid measured attention entry")
        require(type(log["requiredAuthority"]) is bool, "Record whether the interruption required operator authority")
        require("activity" not in log or log["activity"] in ACTIVITIES, "Unknown operator activity")
    require(isinstance(record["unattended"], list), "unattended must be a list")
    for interval in record["unattended"]:
        require(instant(interval["end"]) > instant(interval["start"]), "Unattended interval ends before it starts")
        for key in ("completed", "progress", "unresolved"):
            require(present(interval[key]), f"Record the return observation for {key} (use 'none' when observed)")
        require(isinstance(interval["interruptions"], list), "Record interruptions during the interval")
        for event in interval["interruptions"]:
            require(present(event["reason"]) and type(event["requiredAuthority"]) is bool, "Invalid interruption")
    totals = None if logs is None else {kind: sum(l["minutes"] for l in logs if l["kind"] == kind) for kind in KINDS}
    activities = None if logs is None else dict.fromkeys((*ACTIVITIES,"unclassified"),0)
    for entry in logs or []:
        activities[entry.get("activity","unclassified")] += entry["minutes"]
    return {**record, "minutesByReason": totals, "minutesByActivity": activities,
            "activeMinutes": None if totals is None else sum(totals.values())}


def read_bundle(path):
    with ZipFile(path) as archive:
        infos = archive.infolist()
        require(len(infos) == len({i.filename for i in infos}) and sum(i.file_size for i in infos) <= LIMIT,
                "Invalid or oversized evidence package")
        task = json.loads(archive.read("task.json"))
        require(task["format"] == "factory-task-evidence/v1" and task["evidenceClass"] == "controlled", "Unsupported evidence class or version")
        tid = str(UUID(task["taskId"]))
        require(tid == task["taskId"], "Invalid task ID")
        for source in task["sources"]:
            if source["path"] is not None:
                require(hashlib.sha256(archive.read(source["path"])).hexdigest() == source["digest"], "Source digest mismatch")
        failures = []
        for i, item in enumerate(task["receipts"], 1):
            name = f"receipts/{i}.json"
            require(item["path"] == name and item["index"] == i, "Invalid receipt reference")
            data = archive.read(name)
            require(hashlib.sha256(data).hexdigest() == item["digest"], "Receipt digest mismatch")
            receipt = json.loads(data)
            require(receipt["status"] == item["status"], "Receipt status mismatch")
            if receipt["status"] != "Rejected":
                require(receipt["taskId"] == tid, "Receipt belongs to another task")
            if receipt["status"] == "Failed":
                failures.extend({"check": c["id"], "ref": f"controlled/{tid}.zip!/{name}"} for c in receipt["checks"] if not c["passed"] and c["id"])
        return {"id": tid, "activity": task["activity"], "scope": task["scope"], "sourceHash": task["sourceHash"],
                "repairs": task["repairs"], "observedElapsedSeconds":task.get("observedElapsedSeconds"), "waits": task["waits"], "resumeGates": [g["sequence"] for g in task["gates"] if g["outcome"] == "Resume"],
                "decisions": None if task["decisions"] is None else len(task["decisions"]), "receipts": [r["status"] for r in task["receipts"]],
                "publication": task["publication"], "costs": task["costs"], "interventions":task["interventions"], "failedChecks": failures}


def import_bundle(study, path):
    require(path.stat().st_size <= LIMIT, "Oversized evidence package")
    # Validate the exact bytes that will be retained.
    data = path.read_bytes()
    facts = read_bundle(BytesIO(data))
    dest = study / "controlled" / (facts["id"] + ".zip")
    try:
        publish(dest, data)
    except FileExistsError:
        require(dest.read_bytes() == data, "Another snapshot of this task is present. Choose a new study for a later capture.")
    return dest


def comparison(records):
    buckets = defaultdict(lambda: defaultdict(list))
    unmatched = []
    for r in records:
        if r["match"] and r["boundary"]:
            buckets[r["match"], r["boundary"]][r["workflow"]].append(r)
        else:
            unmatched.append(f'{r["workflow"]}/{r["id"]}')
    counts = Counter((r["workflow"], field, r[field]) for r in records
                     for field in ("reference", "acceptedDraftPr") if present(r[field]))
    duplicates = {key for key, count in counts.items() if count > 1}
    pairs = []
    for (match, boundary), sides in sorted(buckets.items()):
        if len(sides["baseline"]) != 1 or len(sides["pilot"]) != 1:
            unmatched.extend(f'{r["workflow"]}/{r["id"]}' for side in sides.values() for r in side)
            continue
        a, b = sides["baseline"][0], sides["pilot"][0]
        def avoidable(r):
            return None if r["minutesByReason"] is None else r["minutesByReason"]["avoidable"]
        completed = all(r["outcome"] == "delivered" and present(r["reference"]) and present(r["acceptanceEvidence"]) for r in (a,b))
        unique = all((r["workflow"], field, r[field]) not in duplicates for r in (a, b)
                     for field in ("reference", "acceptedDraftPr"))
        pairs.append({"completedAtBoundary": completed, "uniqueReferences": unique, "match": match, "boundary": boundary, "baseline": a["id"], "pilot": b["id"],
                      "baselineMinutes": avoidable(a), "pilotMinutes": avoidable(b),
                      "qualityAndSecuritySupported": all(r["quality"] == r["security"] == "pass" and r["acceptanceEvidence"] and
                          r["reference"] and r["acceptedDraftPr"] and r["substantiveRepairs"] is not None and r["outcome"] == "delivered" for r in (a,b)) and bool(b["independentReview"])})
    measured = [p for p in pairs if p["uniqueReferences"] and p["completedAtBoundary"] and p["baselineMinutes"] is not None and p["pilotMinutes"] is not None]
    base = sum(p["baselineMinutes"] for p in measured)
    pilot = sum(p["pilotMinutes"] for p in measured)
    percent = None if base == 0 else round(100 * (base-pilot)/base, 2)
    live = [r for r in records if r["workflow"] == "pilot"]
    gaps = []
    if len(live) != 5:
        gaps.append(f"Five authorized pilot tasks required; {len(live)} recorded.")
    if not pairs or unmatched or len(measured) != len(pairs):
        gaps.append("Missing, ambiguous or unmeasured matched observations.")
    if base == 0:
        gaps.append("No positive measured baseline denominator.")
    if not live or any(not r["authorization"] or not r["acceptedDraftPr"] or r["substantiveRepairs"] is None for r in live):
        gaps.append("Pilot authorization, accepted Draft PR evidence or repair observations missing.")
    if not pairs or any(not p["qualityAndSecuritySupported"] for p in pairs):
        gaps.append("Equivalent accepted quality, security and independent review are unconfirmed.")
    if not {"deliberate-blocker","self-development"} <= {tag for r in live for tag in r["tags"]}:
        gaps.append("Deliberate blocker and self-development coverage incomplete.")
    for side, field in sorted({(side, field) for side, field, _ in duplicates}):
        gaps.append(f"Repeated {side} observation field '{field}' requires review.")
    if not any(r.get("failedVerificationEvidence") for r in live):
        gaps.append("No failed verification receipt recorded for the live cohort.")
    if not any(r["unattended"] for r in live):
        gaps.append("No observed unattended pilot interval.")
    assessments = [r[key] for r in records for key in ("quality", "security")]
    release = ("failed" if "fail" in assessments else "supported by operator observations" if
               pairs and not unmatched and not duplicates and all(p["qualityAndSecuritySupported"] for p in pairs) else "unconfirmed")
    return {"qualityAndSecurity": release, "pairs": pairs, "unmatched": sorted(unmatched), "measuredPairs": len(measured),
            "matchedBaselineAvoidableMinutes": base if measured else None, "matchedPilotAvoidableMinutes": pilot if measured else None,
            "matchedReductionPercent": percent, "gaps": gaps,
            "targetMet": None if gaps else (base-pilot)*2 >= base}


def proposal(records, controlled):
    reasons = defaultdict(list)
    for r in records:
        if r["workflow"] == "pilot":
            for i, log in enumerate(r["attention"] or []):
                if log["kind"] == "avoidable" and log["minutes"] > 0:
                    reasons[log["reason"], log["requiredAuthority"]].append((log["minutes"], f'{r["observationFile"]}#/attention/{i}'))
    if reasons:
        (reason, authority), events = max(reasons.items(), key=lambda group: sum(minutes for minutes, _ in group[1]))
        change = ("Prepare the supporting evidence before requesting the reserved decision" if authority else
                  "Trial resolving this interruption inside the owner's existing delegated scope")
        return {"scope":"operator-observed pilot", "status":"proposed for independent review",
                "change":f"{change}. Recorded reason: {reason}", "observedMinutes":sum(minutes for minutes, _ in events),
                "evidence":[ref for _, ref in events],
                "test":"Repeat the matched task and compare avoidable minutes while retaining the same approval, quality and security gates."}
    frequency = Counter(c["check"] for task in controlled for c in task["failedChecks"])
    if not frequency:
        return None
    check = frequency.most_common(1)[0][0]
    return {"scope":"controlled rehearsal", "status":"proposed for independent review",
            "change":f"Try running the {check} check before handing the candidate to verification.",
            "evidence":[c["ref"] for t in controlled for c in t["failedChecks"] if c["check"] == check],
            "test":"Compare repair counts and retained verification results on matched tasks. Keep the independent verification gate."}


def evaluate(study):
    require(any((study / name).is_dir() for name in ("observations", "controlled")), "Initialize the study first")
    records = []
    for path in sorted((study / "observations").glob("*.json")):
        try:
            records.append({**validate(json.loads(path.read_text(encoding="utf-8"))), "observationFile":path.relative_to(study).as_posix()})
        except (ValueError, TypeError, KeyError) as e:
            raise ValueError(f"{path.name}: {e}") from e
    require(len(records) == len({(r["workflow"], r["id"]) for r in records}), "Duplicate task observation")
    fixtures = [read_bundle(p) for p in sorted((study / "controlled").glob("*.zip"))]
    require(len(fixtures) == len({f["id"] for f in fixtures}), "Duplicate controlled task ID")
    require(not ({f["id"] for f in fixtures} & {r["id"] for r in records if r["workflow"] == "pilot"}),
            "A controlled task cannot also count toward the live pilot")
    return {"format":"factory-pilot-evaluation/v1", "observations":records, "controlled":fixtures,
            "comparison":comparison(records), "proposal":proposal(records,fixtures)}


def markdown(report):
    def shown(v):
        return "unmeasured" if v is None else str(v)
    out = ["# Factory pilot retrospective", "", "## Operator observations", ""]
    for r in report["observations"]:
        out.append(f'- {r["workflow"]}/{r["id"]} · {r["scope"]} · {r["outcome"]} · active minutes {shown(r["activeMinutes"])} · repairs {shown(r["substantiveRepairs"])}')
        out.append(f'  - Quality {r["quality"]} · security {r["security"]} · acceptance {r["acceptanceEvidence"] or "unreported"} · independent review {r["independentReview"] or "unreported"} · accepted Draft PR {r["acceptedDraftPr"] or "unreported"}')
        if r.get("measurements"):
            summary = ", ".join(f"{k} {v}" for k,v in r["measurements"].items() if v is not None)
            out.append("  - Timing and cost observations · " + (summary or "unmeasured"))
        if r["minutesByActivity"] is not None:
            out.append("  - Active minutes by activity · " + ", ".join(f"{k} {v}" for k,v in r["minutesByActivity"].items()))
        for entry in r["attention"] or []:
            out.append(f'  - {entry["kind"]} · {entry["minutes"]} min · {entry["reason"]} · authority required {entry["requiredAuthority"]}')
        for interval in r["unattended"]:
            out.append(f'  - Unattended {interval["start"]} to {interval["end"]}. Completed: {interval["completed"]}. Progress: {interval["progress"]}. Unresolved: {interval["unresolved"]}.')
            for interruption in interval["interruptions"]:
                out.append(f'    - {interruption["reason"]} · authority required {interruption["requiredAuthority"]}')
    if not report["observations"]:
        out.append("No baseline or live pilot observations supplied.")
    c = report["comparison"]
    out.extend(["", "## Matched comparison", "", f'Measured pairs: {c["measuredPairs"]}. Matched avoidable attention reduction: ' +
                ("unmeasured." if c["matchedReductionPercent"] is None else f'{c["matchedReductionPercent"]}%.'),
                "", "50% target: " + ("undetermined" if c["targetMet"] is None else "met" if c["targetMet"] else "missed") + ".", ""])
    out.append(f'Matched avoidable minutes · baseline {shown(c["matchedBaselineAvoidableMinutes"])} · pilot {shown(c["matchedPilotAvoidableMinutes"])}. Quality and security: {c["qualityAndSecurity"]}.')
    out.append("")
    for pair in c["pairs"]:
        out.append(f'- {pair["match"]} · {pair["boundary"]} · {pair["baseline"]} {shown(pair["baselineMinutes"])} min / {pair["pilot"]} {shown(pair["pilotMinutes"])} min')
    out.extend("- " + gap for gap in c["gaps"])
    if c["unmatched"]:
        out.append("- Unmatched observations: " + ", ".join(c["unmatched"]))
    out.extend(["", "## Controlled rehearsal", ""])
    for t in report["controlled"]:
        out.append(f'- {t["id"]} · {t["activity"]} · repairs {t["repairs"]} · receipts {", ".join(t["receipts"]) or "none"} · wait intervals {len(t["waits"])} · resumes {len(t["resumeGates"])}')
        costs = t["costs"]
        out.append(f'  - Simulated usage {costs["simulatedEstimatedUsdMicros"]} USD micros · held {costs["simulatedHeldUsdMicros"]} · task cost {shown(costs["taskCostUsdMicros"])} · accepted human review unreported')
    if not report["controlled"]:
        out.append("No controlled evidence imported.")
    p = report["proposal"]
    out.extend(["", "## Improvement proposal", ""])
    if p:
        out.extend([p["scope"] + " · " + p["status"] + ".", "", p["change"], "", p["test"], ""])
        out.extend("- " + ref for ref in p["evidence"])
    else:
        out.append("Awaiting an observed event that supports a proposal.")
    return "\n".join(out).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "add", "import-fixture", "report"):
        p = cmds.add_parser(name)
        p.add_argument("study", type=Path)
        if name == "add":
            p.add_argument("workflow", choices=("baseline", "pilot"))
            p.add_argument("task")
            p.add_argument("--scope", required=True)
            p.add_argument("--match")
            p.add_argument("--boundary")
        elif name == "import-fixture":
            p.add_argument("zip", type=Path)
        elif name == "report":
            p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    a = parser.parse_args()
    if a.command == "init":
        initialize(a.study)
    elif a.command == "add":
        print(new_record(a.study, a.workflow, a.task, a.scope, a.match, a.boundary))
    elif a.command == "import-fixture":
        print(import_bundle(a.study, a.zip))
    else:
        report = evaluate(a.study)
        print(json.dumps(report, indent=2) if a.format == "json" else markdown(report), end="\n" if a.format == "json" else "")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, BadZipFile) as exc:
        sys.exit(f"Evaluation could not be completed: {exc}")
