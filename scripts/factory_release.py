"""Operator-owned release selector for the disposable factory-source rehearsal."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import getpass
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from environment_leases import IMAGE

PROFILE = Path(__file__).resolve().parents[1] / "fixtures/self-development/policy.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def policy():
    data = PROFILE.read_text(encoding="utf-8").encode()
    return json.loads(data), digest(data)


def git(directory, *args):
    return subprocess.check_output(["git", "--git-dir", str(directory / "remote.git"), *args],
                                   stderr=subprocess.PIPE, timeout=10)


def validate(directory, certificate):
    profile, policy_hash = policy()
    base, head, path = profile["base"], certificate["head"], profile["path"]
    if (certificate["profile"] != profile["version"] or certificate["policyHash"] != policy_hash
            or certificate["base"] != base or certificate["path"] != path
            or certificate["issue"] != profile["issue"] or certificate["draftNumber"] <= 0):
        raise ValueError("Release certificate differs from the installed factory-source policy")
    def oid(ref):
        return git(directory, "rev-parse", "--verify", ref).decode().strip()
    if (oid("refs/heads/main") != base or oid("refs/heads/factory/" + certificate["task"]) != head
            or git(directory, "rev-list", "--parents", "-n", "1", head).decode().split() != [head, base]
            or git(directory, "diff", "--name-status", base, head).decode().strip() != "A\t" + path
            or digest(git(directory, "cat-file", "blob", head + ":" + path)) != certificate["candidateHash"]):
        raise ValueError("Release head, parent, scope or candidate differs from the reviewed Draft")
    validate_roles(directory, certificate)


def validate_roles(directory, certificate):
    profile, policy_hash = policy()
    base = profile["base"]
    if (certificate["base"] != base or certificate["policyHash"] != policy_hash or
            certificate.get("controlPlane", {}).get("checkpointAndRuntimePreserved") is not True):
        raise ValueError("Control-plane isolation evidence is missing")
    roles = certificate["roles"]
    if set(roles) != {"writer","verifier","reviewer"} or len({r["run"] for r in roles.values()}) != 3:
        raise ValueError("Independent writer, verifier and reviewer required")
    with closing(sqlite3.connect((directory / "leases.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for role, receipt in roles.items():
            row = db.execute("SELECT * FROM leases WHERE generation=?", (receipt["generation"],)).fetchone()
            outcome = receipt["outcome"]
            seal = digest(json.dumps(outcome, sort_keys=True, separators=(",", ":")).encode())
            if (not row or row["task"] != certificate["task"] or row["run"] != receipt["run"] or row["role"] != role
                    or row["state"] != "released" or row["image"] != IMAGE or row["policy"] != policy_hash or row["outcome_hash"] != seal
                    or outcome.get("isolated") is not True or outcome["candidateHash"] != certificate["candidateHash"] or outcome["base"] != base
                    or (role == "writer" and row["candidate_hash"] != outcome["candidateHash"])):
                raise ValueError("Missing, stale or inconsistent " + role + " lease evidence")
    verified = roles["verifier"]["outcome"]
    review = roles["reviewer"]["outcome"]
    if (verified["checks"] != {key:True for key in profile["requiredChecks"]} or review["approved"] is not True
            or review["verifierRun"] != roles["verifier"]["run"]):
        raise ValueError("Required independent verification or review is missing")


def stage(directory, certificate):
    validate(directory, certificate)
    with closing(sqlite3.connect(directory / "release.sqlite")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS certified(id INTEGER PRIMARY KEY CHECK(id=1), record TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY, action TEXT NOT NULL,
                   previous TEXT NOT NULL, active TEXT NOT NULL, actor TEXT NOT NULL, recorded TEXT NOT NULL)""")
        db.execute("INSERT INTO certified VALUES(1,?)", (json.dumps(certificate, sort_keys=True),))
        db.execute("INSERT INTO events VALUES(0,'stage',?,?,?,?)",
                   (certificate["base"], certificate["base"], "fixture supervisor", datetime.now(timezone.utc).isoformat()))


def inspect(directory):
    with closing(sqlite3.connect((directory / "release.sqlite").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        certificate = json.loads(db.execute("SELECT record FROM certified WHERE id=1").fetchone()[0])
        events = [dict(row) for row in db.execute("SELECT * FROM events ORDER BY seq")]
        return {"certificate":certificate, "events":events}


def transition(directory, action, sequence, previous, target, actor):
    # The release DB and Git objects are local operator resources, outside every worker lease.
    with closing(sqlite3.connect((directory / "release.sqlite").as_uri() + "?mode=rw", uri=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        certificate = json.loads(db.execute("SELECT record FROM certified WHERE id=1").fetchone()[0])
        expected = {"promote":(certificate["base"], certificate["head"]),
                    "rollback":(certificate["head"], certificate["base"])}
        if expected.get(action) != (previous, target) or not actor.strip():
            raise ValueError("Decision differs from the certified release pair")
        last, active = db.execute("SELECT seq,active FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        if sequence != last or previous != active:
            raise ValueError("Decision is stale for the active release")
        if action == "promote":
            validate(directory, certificate)
        elif git(directory, "cat-file", "-t", target).strip() != b"commit":
            raise ValueError("Known-good rollback commit is unavailable")
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (last+1, action, previous, target,
                   actor, datetime.now(timezone.utc).isoformat()))
    return target


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("directory", type=Path)
    actions = cli.add_subparsers(dest="action", required=True)
    actions.add_parser("status")
    for name in ("promote", "rollback"):
        command = actions.add_parser(name)
        command.add_argument("sequence", type=int)
        command.add_argument("previous")
        command.add_argument("target")
    args = cli.parse_args()
    directory = args.directory.resolve()
    if args.action == "status":
        print(json.dumps(inspect(directory), indent=2))
    else:
        phrase = args.action + " " + args.target
        if not sys.stdin.isatty() or input("Confirm by typing " + phrase + "\n") != phrase:
            raise SystemExit("Explicit local operator confirmation required")
        print(transition(directory, args.action, args.sequence, args.previous, args.target, getpass.getuser()))
