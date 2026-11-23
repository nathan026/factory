"""Exercise the pinned SDK and disposable leases on a local Linux Docker engine."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from uuid import uuid4

RUNTIME = Path(__file__).resolve().parents[1] / "src" / "Factory.Runtime"
LABEL = "factory.m0.qualification"


def command(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]} operation failed: {result.stderr[-1200:].strip()}")
    return result.stdout.strip()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)

def reap_expired(cli):
    removed = []
    def if_present(container, *args):
        try:
            return cli(*args)
        except RuntimeError:
            if cli("ps", "-aq", "--no-trunc", "--filter", f"id={container}"):
                raise
            return None
    for container in cli("ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}.expires").splitlines():
        # Another supervisor may already have reconciled this immutable ID.
        raw = if_present(container, "inspect", "--type", "container", container)
        if raw is None:
            continue
        info = json.loads(raw)[0]
        labels = info["Config"]["Labels"]
        owner = labels.get(LABEL, "")
        deadline = labels.get(LABEL+".expires", "")
        if (info["Id"] == container and re.fullmatch(r"[0-9a-f]{32}", owner)
                and re.fullmatch(fr"/factory-m0-{owner}-[0-9]+", info["Name"])
                and labels.get(LABEL+".role") in {"ro","rw","runtime"}
                and deadline.isdigit() and int(deadline) <= time.time()):
            if if_present(container, "rm", "-f", container) is not None:
                removed.append(container)
    return removed


def qualify(report, laptop=False, live=False):
    context = command(["docker", "context", "show"])
    docker = ["docker", "--context", context]
    endpoint = command(docker + ["context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"])
    require(not os.getenv("DOCKER_HOST") and endpoint.startswith(("npipe:////./pipe/", "unix:///")),
            "Select a local Docker context with a local socket")
    report["engine"] = command(docker + ["version", "--format", "{{.Server.Os}}/{{.Server.Arch}} {{.Server.Version}}"])
    require(report["engine"].startswith("linux/"), "Linux containers required")
    run = uuid4().hex
    names = []
    checks = report["checks"]

    def cli(*args, timeout=30):
        return command(docker + list(args), timeout)

    def acquire(image, folder, mode, program, ttl=300):
        name = f"factory-m0-{run}-{len(names)}"
        names.append(name)
        mount = [] if folder is None else ["--mount", f"type=bind,source={folder},target=/work"
                                          + (",readonly" if mode == "ro" else "")]
        before = time.monotonic()
        cli("run", "-d", "--init", "--name", name, "--label", f"{LABEL}={run}",
            "--label", f"{LABEL}.role={mode}", "--label", f"{LABEL}.expires={int(time.time())+ttl}",
            "--user", "1000:1000", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--network", "none", "--pids-limit", "128", "--cpus", "1", "--memory", "512m",
            "--memory-swap", "512m", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            *mount, image, *program)
        entry = json.loads(cli("inspect", name))[0]
        mounts = [m for m in entry["Mounts"] if m["Type"] != "tmpfs"]
        require((not mounts) if folder is None else
                (len(mounts) == 1 and mounts[0]["Destination"] == "/work" and mounts[0]["RW"] == (mode == "rw")),
                "Unexpected worker mounts")
        report["containerStartMs"].setdefault(mode, round(1000 * (time.monotonic() - before)))
        return name

    with tempfile.TemporaryDirectory(prefix="factory-m0-") as tmp:
        root = Path(tmp)
        try:
            reap_expired(cli)
            iid = root / "image-id"
            cli("build", "--builder", context, "--iidfile", str(iid), str(RUNTIME), timeout=420)
            image = iid.read_text().strip()
            require(re.fullmatch(r"sha256:[a-f0-9]{64}", image), "Invalid built image ID")
            report["image"] = image
            report["imageBytes"] = int(cli("image", "inspect", image, "--format", "{{.Size}}"))
            report["sdkLockSha256"] = hashlib.sha256((RUNTIME / "package-lock.json").read_bytes()).hexdigest()
            a, b = root / "writer", root / "reviewer"
            for folder in [a, b]:
                folder.mkdir()
                folder.chmod(0o777)  # Only synthetic files in a private temporary parent.
            first, second = uuid4().hex, uuid4().hex
            for folder, marker in [(a, first), (b, second)]:
                path = folder / marker
                path.write_text(marker)
                path.chmod(0o644)
            sample = RUNTIME.parents[1] / "fixtures/shipping/qualification-test.cjs"
            (b / "qualification-test.cjs").write_bytes(sample.read_bytes())
            (b / "lease-fixture.json").write_text(json.dumps({"marker":second, "sibling":first}))
            runtime = acquire(image, None, "runtime", ["sleep", "300"])
            report["cli"] = cli("exec", runtime, "./node_modules/.bin/codex", "--version")
            checks["sdkOffline"] = json.loads(cli("exec", "-e", "OPENAI_API_KEY=synthetic-unused",
                                                  "-e", "CODEX_API_KEY=synthetic-unused", runtime,
                                                  "node", "sdk-offline.mjs", timeout=205))
            peak = cli("exec", runtime, "node", "-e",
                       "const fs=require('fs'); const p=['/sys/fs/cgroup/memory.peak',"
                       "'/sys/fs/cgroup/memory/memory.max_usage_in_bytes'].find(p=>fs.existsSync(p));"
                       "console.log(p?fs.readFileSync(p,'utf8').trim():'unknown')")
            report["sdkLeasePeakBytes"] = int(peak) if peak.isdigit() else None
            cli("exec", runtime, "node", "-e",
                "const fs=require('fs'),assert=require('assert'); for(const p of ['/tmp/runtime-home/auth-canary',"
                "'/tmp/control-canary']){fs.writeFileSync(p,'synthetic');assert.equal(fs.readFileSync(p,'utf8'),'synthetic')}")
            reviewer = acquire(image, b, "ro", ["sleep", "300"])
            writer = acquire(image, a, "rw", ["node", "hold-turn.mjs"])
            def inspect_role(name, own, other, mode):
                return json.loads(cli("exec", name, "node", "inspect-role.mjs", own, other, mode))
            reader = inspect_role(reviewer, second, first, "ro")
            worker = inspect_role(writer, first, second, "rw")
            require(reader["pidNamespace"] != worker["pidNamespace"], "Shared PID namespace")
            require((a / "change").read_text() == "permitted" and not (b / "change").exists(), "Role access mismatch")
            checks["workspaceAndEgress"] = "separate canaries and PID namespaces, enforced ro/rw, external route denied"
            checks["runtimeState"] = "control and auth canaries present in runtime, inaccessible in both worker leases"
            heartbeat = a / "heartbeat"
            deadline = time.monotonic() + 15
            while not heartbeat.exists() and time.monotonic() < deadline:
                time.sleep(.1)
            require(heartbeat.exists() and (a / "inflight").exists(), "SDK did not reach in-flight checkpoint")
            before = heartbeat.read_bytes()
            time.sleep(.35)
            require(before != heartbeat.read_bytes(), "No active fixture heartbeat")
            processes = cli("top", writer, "-eo", "pid,comm").splitlines()
            require(any(p.split()[-1:] == ["codex"] for p in processes), "No live Codex child observed")
            cli("kill", writer)
            state = json.loads(cli("inspect", writer, "--format", "{{json .State}}"))
            require(not state["Running"] and state["Pid"] == 0, "Worker still running")
            last = heartbeat.read_bytes()
            time.sleep(.4)
            require(last == heartbeat.read_bytes(), "Cancelled workspace still changing")
            inspect_role(reviewer, second, first, "ro")
            checks["cancelLease"] = "in-flight Codex process observed, namespace stopped, heartbeat stopped, other lease responsive"
            if laptop:
                from m0_lease_tool import check, lease_tool
                from m0_laptop import laptop_probe
                rid = cli("inspect", reviewer, "--format", "{{.Id}}")
                generation = cli("inspect", reviewer, "--format", "{{.State.StartedAt}}")
                require(check(context, rid, run, generation)["marker"] == second, "Repository check returned a foreign marker")
                def must_reject(owner):
                    try:
                        check(context, rid, owner, generation)
                    except RuntimeError as exc:
                        require(str(exc) == "Lease unavailable", "Unexpected lease rejection")
                    else:
                        raise RuntimeError("Unadmitted lease accepted a tool call")
                must_reject(uuid4().hex)
                checks["laptop"] = {}
                with lease_tool(context, rid, run, generation) as (url, token):
                    laptop_probe(root, url, token, second, live, checks["laptop"])
                cli("stop", "--time", "1", reviewer)
                must_reject(run)
                cli("start", reviewer)
                must_reject(run)
                checks["leaseFencing"] = "foreign owner, stopped container and restarted container generation rejected"
                expired = acquire(image, b, "ro", ["sleep", "30"], ttl=0)
                expired_id, started = cli("inspect", expired, "--format", "{{.Id}} {{.State.StartedAt}}").split()
                try:
                    check(context, expired_id, run, started)
                except RuntimeError as exc:
                    require(str(exc) == "Lease unavailable", "Unexpected expiry error")
                else:
                    raise RuntimeError("Expired lease accepted a tool call")
                require(expired_id in reap_expired(cli), "Expired qualification container remained allocated")
                require(cli("inspect", reviewer, "--format", "{{.State.Running}}") == "true", "Active lease lost during reconciliation")
                checks["expiryRecovery"] = "running expired lease denied and reclaimed, current lease preserved"
        finally:
            # Reconcile by the per-run label even if creating a container lost its response.
            owned = cli("ps", "-a", "--filter", f"label={LABEL}={run}", "--format", "{{.Names}}").splitlines()
            for name in owned:
                require(name in names, "Unexpected container with this run label")
                cli("rm", "-f", name)
            require(not cli("ps", "-aq", "--filter", f"label={LABEL}={run}"), "Owned container remains")
            checks["cleanup"] = f"{len(owned)} owned containers removed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="write a new JSON evidence file")
    parser.add_argument("--laptop", action="store_true", help="also qualify the native trusted CLI and lease tool")
    parser.add_argument("--live", action="store_true", help="include three synthetic subscription turns on the laptop")
    args = parser.parse_args()
    report = {"startedUtc": datetime.now(timezone.utc).isoformat(), "scope":"local-offline",
              "m0Exit":"offline evidence only", "checks":{}, "containerStartMs":{},
              "qualifierSha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    receipt = args.report.open("x+", encoding="utf-8") if args.report else None
    if receipt:
        receipt.write(json.dumps({"startedUtc":report["startedUtc"], "outcome":"in_progress"})+"\n")
        receipt.flush()
    try:
        if args.laptop or args.live:
            report["scope"] = "laptop-subscription" if args.live else "laptop-offline"
            report["m0Exit"] = "see package acceptance record"
            sources = [Path(__file__).with_name(n) for n in ["m0_laptop.py", "m0_lease_tool.py"]]
            sources.append(RUNTIME.parents[1] / "fixtures/shipping/qualification-test.cjs")
            report["probeSources"] = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
        qualify(report, args.laptop or args.live, args.live)
        report["outcome"] = "passed"
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        report.update(outcome="failed", reason=str(exc))
    output = json.dumps(report, indent=2)
    print(output)
    if receipt:
        receipt.seek(0)
        receipt.write(output + "\n")
        receipt.truncate()
        receipt.close()
    sys.exit(0 if report["outcome"] == "passed" else 1)
