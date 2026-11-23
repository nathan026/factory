"""Keep each Linux native worker and all its descendants in one bounded systemd unit."""
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from threading import Thread
from uuid import UUID

# Only the trusted adapter needs Docker and controller bindings. codex_worker.environment()
# separately narrows the environment passed to the CLI and its metadata probe.
SERVICE_ENV = ("PATH", "HOME", "CODEX_HOME", "XDG_RUNTIME_DIR", "DOCKER_HOST",
               "DOCKER_CONTEXT", "DOCKER_CONFIG", "FACTORY_CONTROLLER_RELEASE", "FACTORY_FOLLOWUP_APPROVALS",
               "FACTORY_LEASE_DB", "FACTORY_MAX_LEASES", "FACTORY_OPERATION_TIMING", "FACTORY_CODING_END_MS")


def process_identity(pid):
    # /proc/PID/stat field 22 follows the parenthesized process name (which may contain spaces).
    start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    return f"{pid}:{start}"


def fence_on_loss(identities):
    poller = select.poll()
    for identity in identities:
        pid = int(identity.split(":", 1)[0])
        fd = os.pidfd_open(pid)
        if process_identity(pid) != identity:
            raise RuntimeError("Worker launcher was replaced before admission")
        poller.register(fd, select.POLLIN)

    def watch():
        poller.poll()
        # The manager tears down this unit's entire cgroup on loss of its main PID.
        os.kill(os.getpid(), signal.SIGKILL)

    Thread(target=watch, daemon=True).start()


def supervise(argv):
    inside = len(argv) == 7 and argv[4] == "--systemd-worker"
    if inside:
        identities = argv[5:]
        del argv[4:]
    if len(argv) != 4 or not Path(os.environ.get("CODEX_HOME", "")).is_absolute():
        raise RuntimeError("Linux worker needs an execution session and a deployment CODEX_HOME")
    unit = "factory-codex-" + str(UUID(argv[3])) + ".service"
    if inside:
        # The marker alone cannot enable unsupervised execution.
        props = subprocess.check_output(["/usr/bin/systemctl", "--user", "show", unit,
            "-p", "MainPID", "-p", "KillMode", "-p", "SendSIGKILL", "-p", "ControlGroup"], text=True, timeout=5)
        fields = dict(line.split("=", 1) for line in props.splitlines())
        groups = Path("/proc/self/cgroup").read_text().splitlines()
        if (fields["MainPID"] != str(os.getpid()) or fields["KillMode"] != "control-group"
                or fields["SendSIGKILL"] != "yes" or not fields["ControlGroup"].endswith("/" + unit)
                or "0::" + fields["ControlGroup"] not in groups):
            raise RuntimeError("Native adapter is outside its assigned systemd unit")
        fence_on_loss(identities)
        return
    parent = os.getppid()
    if parent <= 1:
        raise RuntimeError("Native adapter has no live launcher")
    identities = [process_identity(parent), process_identity(os.getpid())]
    env = [key + "=" + os.environ[key] for key in SERVICE_ENV if key in os.environ]
    # exec preserves the bridge's stdin/stdout. The unit's main process owns the
    # attempt; systemd retires even detached descendants if that process dies.
    runtime_seconds = 75
    if "FACTORY_CODING_END_MS" in os.environ:
        remaining = int(os.environ["FACTORY_CODING_END_MS"]) // 1000 - int(time.time())
        if not 3 <= remaining <= 300:
            raise RuntimeError("Coding service deadline is expired or outside the deployment limit")
        runtime_seconds = remaining - 2  # Reserve the configured cgroup stop grace.
    os.execv("/usr/bin/systemd-run", ["systemd-run", "--user", "--quiet", "--wait", "--pipe",
        "--collect", "--service-type=exec", "--unit=" + unit, "--expand-environment=no",
        "--property=KillMode=control-group", "--property=SendSIGKILL=yes",
        "--property=TimeoutStopSec=2s", f"--property=RuntimeMaxSec={runtime_seconds}s",
        "--property=NoNewPrivileges=yes", "--property=UMask=0077",
        "/usr/bin/env", "-i", *env, sys.executable, str(Path(argv[0]).resolve()), *argv[1:], "--systemd-worker", *identities])
