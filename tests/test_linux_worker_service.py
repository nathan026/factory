"""Linux unit ownership and process-loss checks, without an account, Docker or task intake."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def probe():
    from linux_worker_service import supervise
    supervise(sys.argv)
    marker = Path(sys.argv[1])
    if os.environ.get("FACTORY_DISPATCH_TOKEN"):
        raise RuntimeError("Dispatcher credential leaked into adapter")
    if sys.stdin.readline().strip() != "probe":
        raise RuntimeError("Lost service input")
    child = subprocess.Popen([sys.executable, "-c",
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"],
        start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    marker.write_text(f"{os.getpid()} {child.pid}")
    print("ready", flush=True)
    time.sleep(50)


@unittest.skipUnless(sys.platform == "linux", "requires the Linux user service manager")
class ServiceTest(unittest.TestCase):
    def test_process_loss(self):
        for loss in ("adapter", "launcher"):
            with self.subTest(loss=loss), tempfile.TemporaryDirectory() as tmp:
                marker = Path(tmp) / "ready"
                session = str(uuid4())
                unit = "factory-codex-" + session + ".service"
                env = {**os.environ, "CODEX_HOME":tmp, "FACTORY_DISPATCH_TOKEN":"synthetic-canary"}
                proc = subprocess.Popen([sys.executable, __file__, str(marker), "0", session],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
                try:
                    proc.stdin.write("probe\n")
                    proc.stdin.close()
                    until = time.monotonic()+15
                    while not marker.exists() and proc.poll() is None and time.monotonic()<until:
                        time.sleep(.05)
                    if proc.poll() is not None:
                        self.fail(proc.stderr.read())
                    self.assertTrue(marker.exists())
                    main, child = map(int,marker.read_text().split())
                    self.assertEqual(proc.stdout.readline(),"ready\n")
                    self.assertNotEqual(os.getpgid(child),os.getpgid(main))
                    self.assertIn(unit, Path(f"/proc/{child}/cgroup").read_text())
                    os.kill(main if loss=="adapter" else proc.pid, signal.SIGKILL)
                    until = time.monotonic()+10
                    while Path(f"/proc/{child}").exists() and time.monotonic()<until:
                        time.sleep(.05)
                    self.assertFalse(Path(f"/proc/{child}").exists(), "Detached descendant survived supervisor loss")
                    proc.wait(timeout=10)
                    self.assertNotEqual(proc.returncode,0)
                finally:
                    subprocess.run(["systemctl","--user","stop",unit],capture_output=True,timeout=10)
                    if proc.poll() is None: proc.kill()
                    proc.wait(timeout=5)
                    proc.stdout.close()
                    proc.stderr.close()

    def test_marker_without_assigned_service_is_rejected(self):
        env = {**os.environ, "CODEX_HOME":"/tmp"}
        run = subprocess.run([sys.executable, __file__, "/unused", "0", str(uuid4()),
            "--systemd-worker", "1:0", "1:0"],input="probe",capture_output=True,env=env,text=True)
        self.assertNotEqual(run.returncode,0)
        self.assertNotIn("ready",run.stdout)


if __name__=="__main__":
    if len(sys.argv)>2: probe()
    else: unittest.main()
