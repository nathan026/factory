"""Create and restore trusted, local controlled-factory checkpoints. See docs/operations.md."""
import argparse
from itertools import chain
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "src/Factory.Api/bin/Debug/net10.0"
MAX_BYTES = 512 * 1024**2
HEADROOM = 64 * 1024**2
CONFIG = ("profiles/shipping-v1.json", "profiles/shipping-expert-v1.json",
          "sample/acceptance.json", "sample/decisions.json", "sample/shipping-rule.json")
PROJECT_FILES = ("acceptance.json", "decisions.json", "shipping-rule.json", "factory-policy.json", "verify.cjs")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def scoped_manifests(build):
    projects = (build / "projects").resolve(strict=True)
    for path in sorted(projects.glob("*/*-policy.json")):
        if not re.fullmatch(r"(factory|v[1-9][0-9]*)-policy\.json", path.name):
            continue
        if path.resolve(strict=True) != path or not re.fullmatch("[a-z][a-z0-9-]*",path.parent.name):
            raise ValueError("Unsupported installed project location")
        rules = json.loads(path.read_text(encoding="utf-8"))
        layout = rules.get("repositoryLayout")
        if layout is None:
            continue
        names = [layout["source"],layout["decisions"],layout["cases"],rules["script"],
                 *layout["additionalReads"],*layout.get("additionalWrites",[])]
        for name in names:
            if (not isinstance(name,str) or len(name)>240 or
                    any(not re.fullmatch("[A-Za-z0-9][A-Za-z0-9_.-]{0,79}",part) for part in name.split("/"))):
                raise ValueError("Unapproved repository path in checkpoint")
            if (path.parent/name).resolve(strict=True) != path.parent/name:
                raise ValueError("Checkpoint source traverses a filesystem link")
        yield path, [path.parent/name for name in names]


def identity(build, version=5):
    files = ["Factory.Api.dll", *CONFIG, *(f"projects/{project}/{name}"
             for project in ("retail", "wholesale") for name in PROJECT_FILES)]
    if version >= 2:
        files += ["projects/retail/v2-policy.json", *("projects/factory-summary/"+name for name in
            ("factory-policy.json","v2-policy.json","policy.json","verify.cjs","renderer-base.cjs",
             "task-summary.cjs","fct020-operator-evidence.json"))]
    if version in (3, 4):
        files += ["projects/checkout-scope/" + name for name in (
            "factory-policy.json", "fixtures/shipping/repository/shipping-rule.json",
            "fixtures/shipping/repository/decisions.json", "fixtures/shipping/repository/acceptance.json",
            "fixtures/retail/repository/verify.cjs")]
    if version == 4:
        files += ["projects/checkout-scope/v2-policy.json", "projects/checkout-scope/fixtures/retail/repository/shipping-rule.json"]
    if version == 5:
        for manifest, paths in scoped_manifests(build):
            files.extend(p.relative_to(build.resolve()).as_posix() for p in [manifest,*paths])
    return {name: digest(build / name) for name in sorted(set(files))}


def space(path, size):
    if size > MAX_BYTES or shutil.disk_usage(path).free < size + HEADROOM:
        raise ValueError("Checkpoint exceeds the 512 MiB laptop limit or available disk reserve.")


def local_docker(container):
    context = subprocess.check_output(["docker", "context", "show"], text=True).strip()
    endpoint = subprocess.check_output(["docker", "context", "inspect", context, "--format",
                                        '{{(index .Endpoints "docker").Host}}'], text=True).strip()
    if os.getenv("DOCKER_HOST") or not endpoint.startswith(("unix://", "npipe://")):
        raise ValueError("Select a local Docker context for this laptop operation.")
    cmd = ["docker", "--context", context]
    service = subprocess.check_output([*cmd, "inspect", "--format",
                                       '{{index .Config.Labels "com.docker.compose.service"}}', container], text=True)
    if service.strip() != "db":
        raise ValueError("Expected the database service from the laptop Compose deployment.")
    return [*cmd, "exec", "-i", container]


def psql(cmd, database, statement, role="postgres"):
    result = subprocess.run([*cmd, "psql", "-XAtq", "-v", "ON_ERROR_STOP=1", "-U", role, "-d", database],
                            input=statement.encode(), capture_output=True, check=True)
    return result.stdout.decode().strip()


def snapshot_sqlite(state, dest):
    source = (state / "slice.db").resolve(strict=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        page_bytes = db.execute("PRAGMA page_size").fetchone()[0]
        space(dest.parent, page_bytes * db.execute("PRAGMA page_count").fetchone()[0])
        def capacity(status, remaining, total):
            if total * page_bytes > MAX_BYTES or shutil.disk_usage(dest.parent).free < HEADROOM:
                raise ValueError("SQLite snapshot reached the laptop storage limit.")
        with closing(sqlite3.connect(dest)) as copy:
            db.backup(copy, pages=256, progress=capacity)
            check_sqlite(copy)


def check_sqlite(db):
    db.execute("SELECT restore_id FROM restore_history LIMIT 0")
    if (db.execute("SELECT count(*) FROM controller_project WHERE id=1").fetchone()[0] != 1
            or db.execute("PRAGMA quick_check").fetchall() != [("ok",)]
            or db.execute("PRAGMA foreign_key_check").fetchone() is not None):
        raise ValueError("Checkpoint failed factory identity, integrity or foreign-key validation.")


def snapshot_pg(args, dest):
    cmd = local_docker(args.pg_container)
    # Restrict this interface to the controller role and its databases.
    if not re.fullmatch(r"factory|restore_[0-9a-f]{32}", args.database):
        raise ValueError("Expected a factory controller database.")
    if psql(cmd, args.database, "SELECT count(*) FROM public.controller_project WHERE id=1;", "factory") != "1":
        raise ValueError("Missing factory project binding.")
    psql(cmd, args.database, "SELECT restore_id FROM public.restore_history LIMIT 0;", "factory")
    with dest.open("xb") as out, subprocess.Popen([*cmd, "pg_dump", "--no-acl", "--no-owner", "-U", "factory",
                                                   "-d", args.database], stdout=subprocess.PIPE) as proc:
        try:
            while chunk := proc.stdout.read(1024**2):
                space(dest.parent, len(chunk))
                if out.tell() + len(chunk) > MAX_BYTES:
                    raise ValueError("PostgreSQL dump exceeds the laptop limit.")
                out.write(chunk)
        finally:
            proc.stdout.close()
        if proc.wait():
            raise ValueError("PostgreSQL dump failed.")


@contextmanager
def snapshot_leases(path, dest):
    # Hold a writer reservation through the controller snapshot. A second, read-only connection copies the committed pages.
    uri = path.resolve(strict=True).as_uri()
    with closing(sqlite3.connect(uri+"?mode=rw",uri=True,timeout=5)) as fence:
        fence.execute("BEGIN IMMEDIATE")
        if fence.execute("SELECT COUNT(*) FROM leases WHERE state<>'released'").fetchone()[0]:
            raise ValueError("Checkpoint requires released environment leases.")
        engines = fence.execute("SELECT engine FROM provider").fetchall()
        if len(engines)!=1:
            raise ValueError("Missing lease provider identity.")
        if fence.execute("SELECT 1 FROM sqlite_master WHERE name='codex_attempts'").fetchone() and fence.execute(
                "SELECT 1 FROM codex_attempts WHERE status IN ('preflight','running') LIMIT 1").fetchone():
            raise ValueError("Checkpoint requires settled native attempts.")
        size = fence.execute("PRAGMA page_count").fetchone()[0]*fence.execute("PRAGMA page_size").fetchone()[0]
        space(dest.parent,size)
        with closing(sqlite3.connect(uri+"?mode=ro",uri=True)) as reader, closing(sqlite3.connect(dest)) as copy:
            reader.backup(copy)
            if copy.execute("PRAGMA quick_check").fetchall()!=[("ok",)]:
                raise ValueError("Lease journal integrity failure.")
        yield {"engine":engines[0][0],"sha256":digest(dest),"bytes":dest.stat().st_size}


def check_live_receipts(profile, build, lease_copy, repository_rows, expert_rows):
    manifests = [build / "projects/factory-summary/v2-policy.json", *(path for path,_ in scoped_manifests(build))]
    live_repository = any(profile == hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
                          for path in manifests if path.is_file())
    experts = list(expert_rows())
    if not live_repository and not experts:
        return
    if lease_copy is None:
        raise ValueError("A live controller checkpoint requires its deployment's lease journal.")
    with closing(sqlite3.connect(lease_copy.resolve().as_uri() + "?mode=ro", uri=True)) as leases:
        for task, run, action, raw in chain(experts, repository_rows() if live_repository else ()):
            if raw is None:
                continue
            receipt = leases.execute("""
                SELECT a.result, l.outcome_hash FROM codex_attempts a JOIN leases l ON a.generation=l.generation
                WHERE a.task=? AND a.run=? AND a.action=? AND a.status='sealed' AND l.task=a.task AND l.run=a.run
                  AND l.operation=a.action AND l.policy=a.policy AND l.state='released'
                """, (task, run, action)).fetchone()
            if (not receipt or not receipt[0] or hashlib.sha256(receipt[0].encode()).hexdigest() != receipt[1]
                    or json.loads(receipt[0]) != json.loads(raw)):
                raise ValueError("Controller receipt differs from its settled native lease journal.")


def validate_sqlite_receipts(db, build, lease_copy):
    profile, = db.execute("SELECT profile FROM controller_project WHERE id=1").fetchone()
    check_live_receipts(profile, build, lease_copy, lambda: db.execute("""
        SELECT o.task_id,o.run_id,json_extract(o.request,'$.action'),i.output FROM dispatch_outbox o JOIN dispatch_inbox i ON i.id=o.id
        WHERE i.output IS NOT NULL AND json_extract(o.request,'$.action') IN ('Implement','Review')
        """), lambda: db.execute("""
        SELECT o.task_id,s.session_id,json_extract(o.request,'$.action'),i.output FROM dispatch_outbox o LEFT JOIN dispatch_inbox i ON i.id=o.id
        LEFT JOIN dispatch_sessions s ON s.operation_id=o.id
        WHERE json_extract(o.request,'$.expertRuntime')='codex-subscription' OR json_extract(o.request,'$.action') IN ('Discuss','AnswerWorker')
        """))


def validate_pg_receipts(cmd, database, build, lease_copy, role):
    profile = psql(cmd, database, "SELECT profile FROM controller_project WHERE id=1", role)
    def rows():
        raw = psql(cmd, database, """
            SELECT json_build_array(o.task_id,o.run_id,o.request::json->>'action',i.output) FROM dispatch_outbox o JOIN dispatch_inbox i ON i.id=o.id
            WHERE i.output IS NOT NULL AND o.request::json->>'action' IN ('Implement','Review')
            """, role)
        return (json.loads(line) for line in raw.splitlines())
    def experts():
        raw = psql(cmd, database, """
            SELECT json_build_array(o.task_id,s.session_id,o.request::json->>'action',i.output) FROM dispatch_outbox o LEFT JOIN dispatch_inbox i ON i.id=o.id
            LEFT JOIN dispatch_sessions s ON s.operation_id=o.id
            WHERE o.request::json->>'expertRuntime'='codex-subscription' OR o.request::json->>'action' IN ('Discuss','AnswerWorker')
            """, role)
        return (json.loads(line) for line in raw.splitlines())
    check_live_receipts(profile, build, lease_copy, rows, experts)


def create(args):
    output = args.bundle.resolve()
    if output.exists():
        raise ValueError("Choose a new backup directory.")
    files = identity(args.build)
    version = json.loads(subprocess.check_output(["dotnet", str(args.build / "Factory.Api.dll"), "--release-info"], timeout=10))
    if version["sha256"] != files["Factory.Api.dll"]:
        raise ValueError("Controller changed during release identification.")
    space(output.parent, 0)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        stage = Path(temp) / "bundle"
        stage.mkdir()
        engine = "sqlite" if args.state else "postgres"
        payload = stage / ("slice.db" if args.state else "factory.sql")
        lease_db = args.lease_db
        lease_copy = stage/"leases.sqlite"
        with (snapshot_leases(lease_db,lease_copy) if lease_db else nullcontext(None)) as journal:
            if args.state:
                snapshot_sqlite(args.state, payload)
                with closing(sqlite3.connect(payload)) as db:
                    validate_sqlite_receipts(db, args.build, lease_copy if journal else None)
            else:
                snapshot_pg(args, payload)
                validate_pg_receipts(local_docker(args.pg_container), args.database, args.build,
                                     lease_copy if journal else None, "factory")
            space(stage.parent,payload.stat().st_size+(journal["bytes"] if journal else 0))
        for name in files.keys() - {"Factory.Api.dll"}:
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(args.build / name, dest)
            if digest(dest) != files[name]:
                raise ValueError("Deployed project configuration changed during backup.")
        manifest = {"format": 5, "leaseJournal":journal, "backupId": str(uuid4()), "createdUtc": datetime.now(timezone.utc).isoformat(),
                    "engine": engine, "release": files, "buildVersion": version["version"], "sha256": digest(payload), "bytes": payload.stat().st_size}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        stage.rename(output)
    return {"backupId": manifest["backupId"], "bytes": manifest["bytes"], "bundle": str(output)}


def guard_sql(manifest, restore_id):
    backup_id = str(UUID(manifest["backupId"]))
    release = manifest["release"]["Factory.Api.dll"]
    when = datetime.now(timezone.utc).isoformat()
    return ("INSERT INTO restore_history (seq,restore_id,backup_id,revision,restored_utc) "
            f"SELECT COALESCE(MAX(seq),0)+1,'{restore_id}','{backup_id}','{release}','{when}' FROM restore_history;")


def restore(args):
    bundle = args.bundle.resolve(strict=True)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    engine = "sqlite" if args.state else "postgres"
    if manifest["format"] not in (1,2,3,4,5) or manifest["engine"] != engine or manifest["release"] != identity(args.build,manifest["format"]):
        raise ValueError("Backup format, engine or deployed controller/configuration differs.")
    for name, expected in manifest["release"].items():
        if name != "Factory.Api.dll" and digest(bundle / name) != expected:
            raise ValueError("Archived project configuration failed checksum validation.")
    payload = bundle / ("slice.db" if args.state else "factory.sql")
    size = payload.stat().st_size
    if size != manifest["bytes"] or size > MAX_BYTES or digest(payload) != manifest["sha256"]:
        raise ValueError("Backup payload failed size or checksum validation.")
    journal = manifest.get("leaseJournal")
    lease_file = bundle/"leases.sqlite"
    if journal is not None and (manifest["format"] not in (2,3,4,5) or lease_file.stat().st_size!=journal["bytes"] or
                                size+journal["bytes"]>MAX_BYTES or digest(lease_file)!=journal["sha256"]):
        raise ValueError("Lease journal failed size or checksum validation.")
    lease_state = args.state or args.lease_state
    if journal and (lease_state is None or lease_state.exists()):
        raise ValueError("Restore the lease journal into a new state directory.")
    if args.lease_state and (args.state or not journal):
        raise ValueError("--lease-state is for PostgreSQL bundles with a lease journal.")
    def copy_leases(directory):
        dest = directory/"leases.sqlite"
        shutil.copyfile(lease_file,dest)
        if digest(dest)!=journal["sha256"]:
            raise ValueError("Lease journal changed during restoration.")
    restore_id = uuid4()
    guard = guard_sql(manifest, restore_id)
    if args.state:
        target = args.state.resolve()
        if target.exists():
            raise ValueError("Restore needs a new state directory.")
        space(target.parent, size + (journal["bytes"] if journal else 0))
        with tempfile.TemporaryDirectory(dir=target.parent) as temp:
            stage = Path(temp) / "state"
            stage.mkdir()
            dest = stage / "slice.db"
            shutil.copyfile(payload, dest)
            if digest(dest) != manifest["sha256"]:
                raise ValueError("Snapshot changed during restore.")
            with closing(sqlite3.connect(dest)) as db:
                check_sqlite(db)
                validate_sqlite_receipts(db, args.build, lease_file if journal else None)
                with db:
                    db.execute(guard)
            if journal:
                copy_leases(stage)
            stage.rename(target)
        location = str(target)
    else:
        cmd = local_docker(args.pg_container)
        location = "restore_" + restore_id.hex
        # The new database rejects application connections until data and guard commit together.
        psql(cmd, "postgres", f"CREATE DATABASE {location} OWNER factory TEMPLATE template0 CONNECTION LIMIT 0;")
        psql(cmd, "postgres", f"REVOKE CONNECT ON DATABASE {location} FROM PUBLIC;")
        with payload.open("rb") as dump, subprocess.Popen([*cmd, "psql", "-Xq", "-v", "ON_ERROR_STOP=1",
                "--single-transaction", "-U", "postgres", "-d", location, "-f", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL) as proc:
            proc.stdin.write(b"SET LOCAL ROLE factory;\n")
            hashed = hashlib.sha256()
            while chunk := dump.read(1024**2):
                hashed.update(chunk)
                proc.stdin.write(chunk)
            if hashed.hexdigest() != manifest["sha256"]:
                proc.kill()
                raise ValueError("Dump changed during restore. The new database remains isolated.")
            proc.stdin.write(("\nSET LOCAL search_path TO public;\n" + guard + "\n").encode())
            proc.stdin.close()
            if proc.wait():
                raise ValueError("Restore transaction failed. The new database remains isolated.")
        validate_pg_receipts(cmd, location, args.build, lease_file if journal else None, "postgres")
        if journal:
            space(lease_state.parent,journal["bytes"])
            with tempfile.TemporaryDirectory(dir=lease_state.parent) as tmp:
                stage = Path(tmp)/"state"
                stage.mkdir()
                copy_leases(stage)
                stage.rename(lease_state)
        psql(cmd, "postgres", f"ALTER DATABASE {location} CONNECTION LIMIT -1;")
    return {"restoreId": str(restore_id), "location": location, "paused": True,
            "leaseDb":str(lease_state/"leases.sqlite") if journal else None}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("create", "restore"):
        p = commands.add_parser(name)
        p.add_argument("bundle", type=Path)
        p.add_argument("--build", type=Path, default=BUILD, help="exact deployed controller output directory")
        target = p.add_mutually_exclusive_group(required=True)
        target.add_argument("--state", type=Path, help="SQLite state directory")
        target.add_argument("--pg-container", help="local laptop Compose database container ID")
        if name == "create":
            p.add_argument("--database", default="factory")
            p.add_argument("--lease-db",type=Path,help="Deployed provider journal at a settled lease boundary")
        else:
            p.add_argument("--lease-state",type=Path,help="New PostgreSQL restore state directory for the lease journal")
    args = cli.parse_args()
    print(json.dumps(create(args) if args.command == "create" else restore(args)))


if __name__ == "__main__":
    main()
