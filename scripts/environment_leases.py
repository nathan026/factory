"""Local Docker execution leases. The supervisor owns this journal and the Docker connection."""
from contextlib import contextmanager
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from uuid import UUID, uuid4

IMAGE = "node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee"
# Project manifests and historical receipts retain IMAGE. Use its fully qualified Docker Hub name on the engine.
ENGINE_IMAGE = "docker.io/library/" + IMAGE
LABEL = "factory.environment.lease"
ROOT = Path(__file__).resolve().parents[1]
SEED = """
const fs = require('node:fs');
const path = require('node:path');
for (const [name, value] of Object.entries(JSON.parse(fs.readFileSync(0, 'utf8')))) {
    const dest = path.join('/source', name);
    fs.mkdirSync(path.dirname(dest), {recursive:true, mode:0o755});
    fs.writeFileSync(dest, value, {flag:'wx', mode:0o444});
}
"""


def fingerprint(data):
    return sha256(data.encode()).hexdigest()


def cli(args, **kwargs):
    return subprocess.run(["docker", *args], capture_output=True, timeout=kwargs.pop("timeout", 20), **kwargs)


def output(result):
    if result.returncode:
        raise RuntimeError("Local Docker lease operation failed: " + result.stderr.decode(errors="replace")[:240])
    return result.stdout.decode().strip()


class Leases:
    def __init__(self, state=None):
        self.db = Path(state or os.getenv("FACTORY_LEASE_DB") or ROOT / ".factory/leases.sqlite")
        self.capacity = int(os.getenv("FACTORY_MAX_LEASES", "5"))
        if self.capacity < 1:
            raise ValueError("Environment capacity must be positive")
        context = output(cli(["context", "show"]))
        endpoint = output(cli(["context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"]))
        if os.getenv("DOCKER_HOST") or not endpoint.startswith(("npipe:////./pipe/", "unix:///")):
            raise RuntimeError("Leases require a local Docker engine")
        self.endpoint = endpoint
        engine = output(self.docker("info", "--format", "{{.OSType}}/{{.ID}}"))
        if not re.fullmatch(r"linux/[a-zA-Z0-9:-]+", engine):
            raise RuntimeError("Expected a local Linux engine with an identity")
        self.engine = engine
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS provider(engine TEXT NOT NULL)")
            pinned = db.execute("SELECT engine FROM provider").fetchone()
            if pinned is None:
                db.execute("INSERT INTO provider VALUES (?)", (engine,))
            elif pinned[0] != engine:
                raise RuntimeError("Lease journal belongs to another Docker engine")
            db.execute("""CREATE TABLE IF NOT EXISTS leases(
                generation TEXT PRIMARY KEY, task TEXT NOT NULL, run TEXT NOT NULL, role TEXT NOT NULL,
                operation TEXT NOT NULL, policy TEXT NOT NULL, image TEXT NOT NULL, files TEXT NOT NULL,
                expires INTEGER NOT NULL, state TEXT NOT NULL, cid TEXT, started TEXT,
                candidate TEXT, candidate_hash TEXT, UNIQUE(task,run,role,operation))""")
            if "outcome_hash" not in {r["name"] for r in db.execute("PRAGMA table_info(leases)")}:
                db.execute("ALTER TABLE leases ADD COLUMN outcome_hash TEXT")
            if "container_name" not in {r["name"] for r in db.execute("PRAGMA table_info(leases)")}:
                db.execute("ALTER TABLE leases ADD COLUMN container_name TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS lease_resolutions(
                generation TEXT PRIMARY KEY REFERENCES leases(generation), engine TEXT NOT NULL,
                principal TEXT NOT NULL, note TEXT NOT NULL, checked_at REAL NOT NULL,
                checked_name TEXT NOT NULL, checked_label TEXT NOT NULL)""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_writer ON leases(task)
                WHERE role='writer' AND state <> 'released'""")

    def docker(self, *args, **kwargs):
        return cli(["--host", self.endpoint, *args], **kwargs)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.db, timeout=45)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        finally:
            db.close()

    def record(self, generation):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE generation=?", (generation,)).fetchone()
            if not row:
                raise RuntimeError("Unknown environment lease")
            return dict(row)

    @staticmethod
    def name(row):
        return row["container_name"] or "factory-lease-" + row["generation"]

    def owned(self, row):
        # Discover a lost create acknowledgement by the generated name AND ownership label.
        # A recorded ID remains authoritative if an operator later renames the container.
        lookup = row["cid"] or self.name(row)
        result = self.docker("container", "inspect", lookup)
        if output(self.docker("info", "--format", "{{.OSType}}/{{.ID}}")) != self.engine:
            raise RuntimeError("Docker engine changed. Lease remains quarantined.")
        if result.returncode:
            if b"No such container" not in result.stderr and b"No such object" not in result.stderr:
                output(result)
            matches = output(self.docker("ps", "-aq", "--no-trunc", "--filter", "label=" + LABEL + "=" + row["generation"]))
            if matches:
                raise RuntimeError("Lease resource identity changed. Cleanup is quarantined.")
            return None
        info = json.loads(result.stdout)[0]
        labels = info["Config"]["Labels"]
        if (info["Name"] != "/" + self.name(row) or labels.get(LABEL) != row["generation"]
                or labels.get(LABEL + ".task") != row["task"] or labels.get(LABEL + ".run") != row["run"]
                or labels.get(LABEL + ".role") != row["role"] or row["image"] != IMAGE
                or info["Config"]["Image"] not in (IMAGE, ENGINE_IMAGE)
                or row["cid"] is not None and info["Id"] != row["cid"]):
            raise RuntimeError("Lease resource ownership mismatch. Cleanup is quarantined.")
        return info

    def acquire(self, task, run, role, operation, policy, files, image=IMAGE, ttl=90, expires_by=None):
        if image != IMAGE or role not in {"writer", "reviewer", "verifier", "expert", "owner"} or not 2 <= ttl <= 300:
            raise ValueError("Unsupported environment template, role or lifetime")
        if not operation or not policy or not files or len(files) > 16:
            raise ValueError("Invalid admitted source packet")
        for name, data in files.items():
            parts = name.split("/")
            if (len(name) > 240 or not isinstance(data, str)
                    or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", part) for part in parts)
                    or any("/".join(parts[:i]) in files for i in range(1, len(parts)))):
                raise ValueError("Invalid admitted source path or file")
        if sum(len(v.encode()) for v in files.values()) > 1024*1024:
            raise ValueError("Admitted source packet exceeds its byte limit")
        task, run = str(UUID(task)), str(UUID(run))
        if output(self.docker("info", "--format", "{{.OSType}}/{{.ID}}")) != self.engine:
            raise RuntimeError("Lease journal belongs to another Docker engine")
        output(self.docker("image", "inspect", ENGINE_IMAGE, "--format", "{{.Id}}"))
        generation = uuid4().hex
        hashes = json.dumps({k:fingerprint(v) for k,v in sorted(files.items())})
        with self.transaction() as db:
            if db.execute("SELECT COUNT(*) FROM leases WHERE state<>'released'").fetchone()[0] >= self.capacity:
                raise RuntimeError("Active environment limit reached")
            name = None
            if role == "writer":
                # The engine retains this exclusion if a replacement supervisor has an empty journal.
                # Also detect writers created under the earlier generation-based naming scheme.
                if output(self.docker("ps","-aq","--no-trunc","--filter","label="+LABEL+".task="+task,
                                      "--filter","label="+LABEL+".role=writer")):
                    raise RuntimeError("An earlier writer still exists on this engine.")
                name = "factory-writer-" + task
            now = int(time.time())
            if expires_by is not None:
                ttl = min(ttl, int(expires_by) - now)
                if ttl < 2:
                    raise RuntimeError("Coding lease expired before reservation")
            try:
                db.execute("""INSERT INTO leases(generation,task,run,role,operation,policy,image,files,expires,state,container_name)
                    VALUES(?,?,?,?,?,?,?,?,?,'reserved',?)""",
                    (generation, task, run, role, operation, policy, image, hashes, now + ttl, name))
            except sqlite3.IntegrityError as err:
                raise RuntimeError("Operation already leased or earlier writer requires retirement") from err
        try:
            with self.transaction() as db:
                if db.execute("UPDATE leases SET state='creating' WHERE generation=? AND state='reserved' AND expires>?",
                              (generation, time.time())).rowcount != 1:
                    raise RuntimeError("Lease retired before creation")
            with self.transaction() as db:
                row = db.execute("SELECT * FROM leases WHERE generation=?", (generation,)).fetchone()
                if row["state"] != "creating" or row["expires"] <= time.time():
                    raise RuntimeError("Lease retired before creation")
                labels = [arg for k in ("generation","task","run","role") for arg in
                          ("--label", (LABEL if k == "generation" else LABEL + "." + k) + "=" + row[k])]
                workspace = ["--tmpfs", "/work:rw,nosuid,nodev,noexec,size=8m,uid=1000,mode=0700"] if role == "writer" else []
                cid = output(self.docker("create", "--pull=never", "--name", self.name(row), *labels,
                    "--user", "1000:1000", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--network", "none", "--pids-limit", "64", "--memory", "128m", "--memory-swap", "128m",
                    "--cpus", "1", "--log-driver", "none", "--tmpfs", "/source:rw,nosuid,nodev,noexec,size=4m,mode=0755",
                    *workspace, "--entrypoint", "sleep", ENGINE_IMAGE, str(ttl)))
                if not re.fullmatch("[0-9a-f]{64}", cid):
                    raise RuntimeError("Docker creation is unconfirmed")
                db.execute("UPDATE leases SET cid=? WHERE generation=?", (cid, generation))
            with self.transaction() as db:
                row = db.execute("SELECT * FROM leases WHERE generation=?", (generation,)).fetchone()
                info = self.owned(row)
                if row["state"] != "creating" or info is None:
                    raise RuntimeError("Lease retired before startup")
                host = info["HostConfig"]
                expected = {"/source", "/work"} if role == "writer" else {"/source"}
                if (host["NetworkMode"] != "none" or host["Privileged"] or not host["ReadonlyRootfs"]
                        or host["PidMode"] == "host" or host["IpcMode"] == "host" or host["CapAdd"]
                        or "ALL" not in host["CapDrop"] or "no-new-privileges" not in host["SecurityOpt"]
                        or info["Config"]["User"] != "1000:1000" or set(host["Tmpfs"]) != expected
                        or any(m["Type"] != "tmpfs" or m["Destination"] not in expected for m in info["Mounts"])):
                    raise RuntimeError("Created container differs from the admitted isolation profile")
                output(self.docker("start", cid))
                output(self.docker("exec", "-i", "--user", "0:0", cid, "node", "-e", SEED, input=json.dumps(files).encode()))
                info = self.owned(row)
                if not info or not info["State"]["Running"] or time.time() >= row["expires"]:
                    raise RuntimeError("Lease startup incomplete")
                db.execute("UPDATE leases SET state='active',started=? WHERE generation=?",
                           (info["State"]["StartedAt"], generation))
            return generation
        except BaseException:
            self.release(generation)
            raise

    def current(self, row):
        if row["state"] != "active" or time.time() >= row["expires"]:
            raise RuntimeError("Retired or expired environment lease")
        info = self.owned(row)
        if not info or not info["State"]["Running"] or info["State"]["StartedAt"] != row["started"] or time.time() >= row["expires"]:
            raise RuntimeError("Environment generation has changed")

    def execute(self, generation, program, packet=None, timeout=20):
        row = self.record(generation)
        self.current(row)
        result = self.docker("exec", "-i", row["cid"], "node", "-e", program,
                             input=json.dumps(packet).encode(), timeout=timeout)
        self.current(self.record(generation))
        return result

    def snapshot(self, generation, filename):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", filename):
            raise ValueError("Invalid candidate filename")
        if self.record(generation)["role"] != "writer":
            raise RuntimeError("Candidate checkpoint requires the admitted writer")
        capture = """const fs=require('node:fs');
const file='/work/'+JSON.parse(fs.readFileSync(0, 'utf8'));
const fd=fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
const stat=fs.fstatSync(fd);
if (!stat.isFile() || stat.size > 1048576) process.exit(2);
const data=Buffer.alloc(stat.size);
if (fs.readSync(fd,data,0,data.length,0)!==data.length || fs.fstatSync(fd).size!==stat.size) process.exit(2);
process.stdout.write(data);"""
        captured = self.execute(generation, capture, filename)
        captured.check_returncode()
        candidate = captured.stdout.decode()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE generation=?", (generation,)).fetchone()
            if row is None or row["role"] != "writer":
                raise RuntimeError("Candidate checkpoint requires the admitted writer")
            self.current(row)
            if row["candidate"] is not None and row["candidate"] != candidate:
                raise RuntimeError("Candidate checkpoint already sealed")
            db.execute("UPDATE leases SET candidate=?,candidate_hash=? WHERE generation=?",
                       (candidate, fingerprint(candidate), generation))
        return candidate

    def finish(self, generation, result, check_dispatch=None):
        # Serialize result sealing with cancellation. Cleanup alone cannot confer success.
        digest = fingerprint(json.dumps(result, sort_keys=True, separators=(",", ":")))
        with self.transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE generation=?", (generation,)).fetchone()
            if row is None:
                raise RuntimeError("Unknown environment lease")
            self.current(row)
            if row["role"] == "writer" and row["candidate_hash"] is None:
                raise RuntimeError("A writer must checkpoint its candidate before finishing")
            if check_dispatch:
                check_dispatch()
            db.execute("UPDATE leases SET state='finished',outcome_hash=? WHERE generation=?", (digest, generation))

    def restore_candidate(self, task, admitted):
        digest = fingerprint(admitted)
        with self.transaction() as db:
            task = str(UUID(task))
            writer = db.execute("SELECT 1 FROM leases WHERE task=? AND role='writer' LIMIT 1", (task,)).fetchone()
            if not writer:
                return admitted  # Older fixture checkpoints are supplied by the controller.
            row = db.execute("""SELECT candidate FROM leases WHERE task=? AND role='writer' AND state='released'
                                AND outcome_hash IS NOT NULL AND candidate_hash=? LIMIT 1""", (task, digest)).fetchone()
            if row is None or row["candidate"] != admitted:
                raise RuntimeError("Captured writer checkpoint differs from the admitted candidate")
            return row["candidate"]

    def release(self, generation):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE generation=?", (generation,)).fetchone()
            if row is None:
                raise RuntimeError("Unknown environment lease")
            if row["state"] == "released":
                return
            if row["state"] == "reserved":
                db.execute("UPDATE leases SET state='released' WHERE generation=?", (generation,))
                return
            db.execute("UPDATE leases SET state='quarantined' WHERE generation=?", (generation,))
        # If creation lost its reply, durably bind the discovered immutable ID before deleting.
        if row["cid"] is None:
            with self.transaction() as db:
                row = db.execute("SELECT * FROM leases WHERE generation=?", (generation,)).fetchone()
                if row["cid"] is None:
                    info = self.owned(row)
                    if info is None:
                        raise RuntimeError("Missing create acknowledgement. Lease remains quarantined.")
                    db.execute("UPDATE leases SET cid=? WHERE generation=?", (info["Id"], generation))
        with self.transaction() as db:
            row = db.execute("SELECT * FROM leases WHERE generation=?", (generation,)).fetchone()
            if row["state"] == "released":
                return
            info = self.owned(row)
            if info:
                output(self.docker("rm", "-f", info["Id"]))
                if self.owned(row):
                    raise RuntimeError("Lease container removal unconfirmed")
            db.execute("UPDATE leases SET state='released' WHERE generation=?", (generation,))

    def resolve_absent(self, generation, note):
        if not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000:
            raise ValueError("A reconciliation note of 1 to 1000 characters is required")
        note = note.strip()
        if os.name == "nt":
            principal = subprocess.check_output(["whoami"], text=True, timeout=5).strip()
        else:
            import pwd
            principal = pwd.getpwuid(os.getuid()).pw_name
        if not principal:
            raise RuntimeError("Missing local operator identity")
        # The creator holds this same lock during create and must reacquire it before start.
        # An old create request finishing late can only leave an unstarted container.
        with self.transaction() as db:
            prior = db.execute("SELECT * FROM lease_resolutions WHERE generation=?", (generation,)).fetchone()
            if prior:
                if prior["principal"] != principal or prior["note"] != note:
                    raise RuntimeError("Conflicting operator resolution")
                return dict(prior)
            row = db.execute("SELECT * FROM leases WHERE generation=?", (generation,)).fetchone()
            if (row is None or row["state"] != "quarantined" or row["expires"] > time.time()
                    or any(row[k] is not None for k in ("cid", "started", "candidate_hash", "outcome_hash"))):
                raise RuntimeError("Resolution requires an expired quarantined create with no recorded ID or execution")
            if self.owned(row) is not None:
                raise RuntimeError("Owned lease container exists. Reconcile its immutable ID.")
            if output(self.docker("info", "--format", "{{.OSType}}/{{.ID}}")) != self.engine:
                raise RuntimeError("Docker engine changed during reconciliation")
            db.execute("INSERT INTO lease_resolutions VALUES (?,?,?,?,?,?,?)",
                       (generation, self.engine, principal, note, time.time(), self.name(row), LABEL+"="+generation))
            db.execute("UPDATE leases SET state='released' WHERE generation=?", (generation,))
            return dict(db.execute("SELECT * FROM lease_resolutions WHERE generation=?", (generation,)).fetchone())

    def reconcile(self):
        with self.transaction() as db:
            rows = db.execute("""SELECT generation FROM leases WHERE state IN ('quarantined','finished')
                OR (state<>'released' AND expires<=?)""", (time.time(),)).fetchall()
        blocked = {}
        for row in rows:
            generation = row["generation"]
            try:
                self.release(generation)
            except (OSError, RuntimeError, subprocess.SubprocessError) as err:
                blocked[generation] = str(err)
        return blocked



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "reconcile", "release", "resolve-absent"))
    parser.add_argument("generation", nargs="?")
    parser.add_argument("--note", help="Operator evidence for an expired, missing create")
    args = parser.parse_args()
    leases = Leases()
    if args.action == "resolve-absent":
        if not args.generation or not args.note:
            parser.error("resolve-absent requires a generation and --note")
        print(json.dumps(leases.resolve_absent(args.generation, args.note), indent=2))
    elif args.action == "release":
        if not args.generation:
            parser.error("release requires the recorded generation")
        leases.release(args.generation)
    elif args.action == "reconcile":
        blocked = leases.reconcile()
        print(json.dumps(blocked, indent=2))
        raise SystemExit(1 if blocked else 0)
    else:
        with leases.transaction() as db:
            rows = db.execute("SELECT generation,task,run,role,operation,state,expires,candidate_hash FROM leases ORDER BY rowid")
            print(json.dumps([dict(row) for row in rows], indent=2))
