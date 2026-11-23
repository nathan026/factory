"""Time the existing cold-review regression once alone and twice concurrently in one lease journal."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch
from uuid import uuid4

import test_environment_leases as regression
import environment_leases as provider


def summary(samples):
    return {"count":len(samples), "maxMs":round(max(samples), 2) if samples else None,
            "totalMs":round(sum(samples), 2)}


def measure(journal, width):
    commands, probes, locks = [], [], []
    local = threading.local()
    barrier = threading.Barrier(width)
    original_cli = provider.cli
    original_transaction = provider.Leases.transaction

    @contextmanager
    def transaction(leases):
        start = time.perf_counter()
        with original_transaction(leases) as db:
            locks.append((time.perf_counter()-start)*1000)
            yield db

    @contextmanager
    def parent_phase(name, **fields):
        probe = getattr(local,"probe",None)
        if probe is None:
            yield
            return
        offset = lambda: round((time.perf_counter()-probe["clockStart"])*1000,3)
        span = {"phase":name,"startMs":offset(),**fields}
        probe["parentPhases"].append(span)
        try:
            yield
        except BaseException as err:
            span["errorClass"] = type(err).__name__
            raise
        finally:
            span["endMs"] = offset()

    class ObservedProcess(subprocess.Popen):
        def __init__(self,*args,**kwargs):
            with parent_phase("create"):
                super().__init__(*args,**kwargs)

        def communicate(self,*args,**kwargs):
            with parent_phase("communicate"):
                return super().communicate(*args,**kwargs)

        def wait(self,timeout=None):
            budget = None if timeout is None else round(timeout*1000,3)
            with parent_phase("wait",timeoutMs=budget):
                return super().wait(timeout)

    def cli(args, **kwargs):
        parts = args[2:] if args[:1] == ["--host"] else args
        start = time.perf_counter()
        kind = None
        if parts[0]=="exec" and "/proc/self/ns/pid" in parts[-1]:
            kind, subject = "namespace", parts[parts.index("node")-1]
        elif parts[:2] in (["container","inspect"],["image","inspect"]):
            kind, subject = parts[0], parts[2]
        if kind:
            local.probe = {"case":local.case,"kind":kind,"subject":subject,
                           "startUnix":time.time(),"clockStart":start,"parentPhases":[]}
        try:
            result = original_cli(args, **kwargs)
            if kind:
                local.probe["returnCode"] = result.returncode
            return result
        except BaseException as err:
            if kind:
                local.probe["errorClass"] = type(err).__name__
            raise
        finally:
            end = time.perf_counter()
            commands.append((parts[0], local.case, start, end))
            if getattr(local,"probe",None) is not None:
                local.probe["hostMs"] = round((end-start)*1000,2)
                del local.probe["clockStart"]
                probes.append(local.probe)
                local.probe = None

    class SharedJournalCase(regression.EnvironmentTest):
        def setUp(self):
            self.task = str(uuid4())
            self.db = journal
            self.leases = provider.Leases(self.db)

    def run_case(index):
        local.case = index
        barrier.wait(timeout=10)
        case = SharedJournalCase("test_self_source_snapshot_and_isolated_cold_review")
        result = case.run()
        return {"case":index, "passed":result.wasSuccessful(),
                "errors":[trace.splitlines()[-1][:180] for _,trace in result.errors+result.failures]}

    start = time.perf_counter()
    with (patch.object(subprocess,"Popen",ObservedProcess), patch.object(provider,"cli",cli),
          patch.object(provider.Leases,"transaction",transaction)):
        with ThreadPoolExecutor(max_workers=width) as pool:
            cases = list(pool.map(run_case,range(width)))
    elapsed = time.perf_counter()-start
    with closing(sqlite3.connect(journal)) as db:
        leases = db.execute("SELECT cid,role,state FROM leases").fetchall()
    roles = {cid:role for cid,role,_ in leases}
    for probe in probes:
        if probe["kind"] == "namespace":
            probe["container"] = probe["subject"]
            probe["role"] = roles.get(probe["container"])
    # Count elapsed time with two CLI subprocess calls in flight on this host.
    edges = sorted([(start,1) for _,_,start,_ in commands]+[(end,-1) for _,_,_,end in commands])
    active, overlap, last = 0, 0, 0
    for at, delta in edges:
        if active>1:
            overlap += at-last
        active += delta
        last = at
    return {"cases":cases,"wallSeconds":round(elapsed,3),"overlappingDockerCommandSeconds":round(overlap,3),
            "dockerCommands":{name:summary([(b-a)*1000 for cmd,_,a,b in commands if cmd==name]) for name in sorted({cmd for cmd,*_ in commands})},
            "journalBeginImmediate":summary(locks),"namespaceProbes":[p for p in probes if p["kind"]=="namespace"],
            "lookupProbes":[p for p in probes if p["kind"]!="namespace"],
            "leaseCount":len(leases),"released":sum(state=="released" for _,_,state in leases)}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("output",type=Path)
    args = cli.parse_args()
    if args.output.exists():
        cli.error("Choose a new evidence file")
    # Retain the local lease journals if an operation fails or ownership needs reconciliation.
    scratch = provider.ROOT / ".factory"
    scratch.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=scratch, prefix="td25-"))
    provider_info = provider.Leases(root/"serial.sqlite")
    running = provider.output(provider_info.docker("ps","-q","--filter","label="+provider.LABEL))
    if running or provider_info.capacity<4:
        cli.error("Measurement requires idle factory leases and four already-admitted slots")
    report = {"observedUtc":datetime.now(timezone.utc).isoformat(),
              "sourceRevision":subprocess.check_output(["git","rev-parse","HEAD"],cwd=provider.ROOT,text=True).strip(),
              "evidenceClass":"existing Docker lease regression; no scheduler or Codex sessions",
              "image":provider.IMAGE,"capacity":provider_info.capacity,"dockerCommandTimeoutSeconds":20,
              "leaseLifetimeSeconds":90,"serial":measure(root/"serial.sqlite",1)}
    if all(c["passed"] for c in report["serial"]["cases"]) and report["serial"]["released"]==report["serial"]["leaseCount"]:
        provider.Leases(root/"pair.sqlite")
        report["paired"] = measure(root/"pair.sqlite",2)
    with args.output.open("x",encoding="utf-8") as f:
        json.dump(report,f,indent=2)
        f.write("\n")
    phases = [report[k] for k in ("serial","paired") if k in report]
    if len(phases)!=2 or not all(all(c["passed"] for c in p["cases"]) and p["leaseCount"]==p["released"] for p in phases):
        raise SystemExit("Cold-review diagnostic incomplete; inspect retained report")
