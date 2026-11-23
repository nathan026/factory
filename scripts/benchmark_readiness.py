"""FCT018 controlled laptop experiment. All environments are disposable FCT017 leases."""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import tempfile
import time
from uuid import uuid4

from environment_leases import ENGINE_IMAGE, IMAGE, ROOT, SEED, Leases, fingerprint, output

PROFILES = {"shipping": "fixtures/shipping/repository", "factory-web": "src/Factory.Web"}
POLICY = "fct018/local-source-readiness-v1"

# Runs as the worker, before root seeds any source in this single-use container.
EMPTY = """
const fs = require('fs'), assert = require('assert/strict');
for (const dir of ['/source', '/work', '/dev/shm']) assert.deepEqual(fs.readdirSync(dir), []);
assert.equal(process.getuid(), 1000);
assert.throws(() => fs.writeFileSync('/source/denied', 'test'), {code:'EACCES'});
assert.equal(fs.existsSync('/var/run/docker.sock'), false);
assert.equal(fs.readFileSync('/proc/net/route','utf8').trim().split('\\n').length, 1);
const allowed = ['PATH','HOSTNAME','HOME','NODE_VERSION','YARN_VERSION'];
assert.deepEqual(Object.keys(process.env).filter(k => !allowed.includes(k)), []);
const pids = fs.readdirSync('/proc').filter(s => /^\\d+$/.test(s)).map(Number).sort((a,b) => a-b);
assert.deepEqual(pids,[1,process.pid]);
process.stdout.write('clean');
"""

# Identical readiness boundary for both modes. Only hashes and counters leave the worker.
READY = """
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const assert = require('assert/strict');
const expected = JSON.parse(fs.readFileSync(0,'utf8'));
const names = fs.readdirSync('/source',{recursive:true,withFileTypes:true}).filter(e=>e.isFile())
    .map(e=>path.relative('/source',path.join(e.parentPath,e.name))).sort();
assert.deepEqual(names,Object.keys(expected).sort());
for (const name of names) assert.equal(crypto.createHash('sha256')
    .update(fs.readFileSync(path.join('/source',name))).digest('hex'),expected[name]);
function counter(paths) {
    const p = paths.find(p=>fs.existsSync(p)); return p ? Number(fs.readFileSync(p,'utf8').trim()) : null;
}
const cpuV1=counter(['/sys/fs/cgroup/cpuacct/cpuacct.usage','/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage']);
const cpuV2='/sys/fs/cgroup/cpu.stat';
const usec = cpuV1 !== null ? cpuV1/1000 : fs.existsSync(cpuV2)
    ? Number(fs.readFileSync(cpuV2,'utf8').match(/^usage_usec (\\d+)$/m)[1]) : null;
const peak=counter(['/sys/fs/cgroup/memory.peak','/sys/fs/cgroup/memory/memory.max_usage_in_bytes']);
const disk=fs.statfsSync('/source');
process.stdout.write(JSON.stringify({node:process.versions.node,arch:process.arch,os:process.platform,
    cgroupCpuUsecAtReady:usec,cgroupPeakBytesAtReady:peak,sourceTmpfsBytes:(disk.blocks-disk.bfree)*disk.bsize}));
"""
DIRTY = """
const fs=require('fs'), marker=JSON.parse(fs.readFileSync(0,'utf8'));
for (const dir of ['/work','/dev/shm']) fs.writeFileSync(dir+'/prior-task-token', marker, {flag:'wx'});
fs.writeFileSync('/work/checkpoint',marker);
"""


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def source_packets(revision):
    oid = git("rev-parse", "--verify", revision + "^{commit}").decode().strip()
    packets = {}
    for name, directory in PROFILES.items():
        files = {}
        for entry in git("ls-tree", "-rz", oid, "--", directory).split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            mode, kind, blob = meta.split()
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise ValueError("Benchmark sources must be tracked regular files")
            files[path.decode()[len(directory)+1:]] = git("cat-file", "blob", blob.decode()).decode()
        if not files:
            raise ValueError("Missing benchmark package: " + directory)
        packets[name] = files
    return oid, packets


def elapsed(start):
    return round((time.perf_counter() - start) * 1000, 2)


def memory_bytes(usage):
    number, unit = re.fullmatch(r"([\d.]+)\s*(B|KiB|MiB|GiB)", usage.split("/")[0].strip()).groups()
    return round(float(number) * {"B":1,"KiB":1024,"MiB":1024**2,"GiB":1024**3}[unit])


class TrialLeases(Leases):
    """Observe the normal acquisition at its last empty-container boundary, in a private journal."""
    trial = None

    def docker(self, *args, **kwargs):
        trial = self.trial
        if trial is not None and args[0:2] == ("image", "inspect"):
            result = super().docker(*args, **kwargs)
            trial["imageCacheHit"] = result.returncode == 0
            return result
        if trial is None or args[-3:] != ("node", "-e", SEED):
            return super().docker(*args, **kwargs)
        cid = args[-4]
        if trial["mode"] == "warm":
            trial["prepareMs"] = elapsed(self.start)
            idle_start = time.perf_counter()
            stats = json.loads(output(super().docker("stats","--no-stream","--format","{{json .}}",cid)))
            trial["idleMemoryBytes"] = memory_bytes(stats["MemUsage"])
            trial["idleCpuPercent"] = float(stats["CPUPerc"].rstrip("%"))
            trial["idleMs"] = elapsed(idle_start)
            self.arrival = time.perf_counter()
        if output(super().docker("exec", cid, "node", "-e", EMPTY)) != "clean":
            raise RuntimeError("Empty template failed its isolation check")
        trial["cleanBeforeSeed"] = True
        before_seed = time.perf_counter()
        seeded = super().docker(*args, **kwargs)
        trial["seedMs"] = elapsed(before_seed)
        return seeded


def distribution(values):
    if not values:
        return None
    ordered = sorted(values)
    return {"n":len(values), "p50":round(statistics.median(values),2),
            "p95":ordered[math.ceil(.95*len(ordered))-1]}


def summarize(rows):
    groups = {}
    for profile in PROFILES:
        for mode in ("cold", "warm"):
            attempts = [r for r in rows if r["profile"] == profile and r["mode"] == mode]
            passed = [r for r in attempts if r["status"] == "passed"]
            groups[f"{profile}/{mode}"] = {
                "attempted":len(attempts), "passed":len(passed),
                "readinessFailures":sum("readyMs" not in r for r in attempts),
                "cleaned":sum(r.get("cleaned") is True for r in attempts),
                "imageCacheHits":sum(r.get("imageCacheHit") is True for r in attempts),
                **{k:distribution([r[k] for r in passed if r.get(k) is not None]) for k in
                   ("readyMs","prepareMs","idleMs","seedMs","preparePlusReadyMs","teardownMs","idleMemoryBytes",
                    "idleCpuPercent","cgroupCpuUsecAtReady","cgroupPeakBytesAtReady","sourceTmpfsBytes","writableLayerBytes")}}
    return groups


def sample(leases, files, trial):
    task, run = str(uuid4()), str(uuid4())
    generation = None
    trial["task"] = task
    hashes = {k:fingerprint(v) for k,v in files.items()}
    leases.trial = trial
    try:
        leases.start = leases.arrival = time.perf_counter()
        generation = leases.acquire(task,run,"writer","readiness",POLICY,files)
        trial["generation"] = generation
        verified = json.loads(output(leases.execute(generation,READY,hashes)))
        trial["readyMs"] = elapsed(leases.arrival)
        trial["preparePlusReadyMs"] = round(trial["readyMs"] + trial.get("prepareMs",0),2)
        trial.update(verified)
        cid = leases.record(generation)["cid"]
        trial["writableLayerBytes"] = int(output(leases.docker("inspect","--size","--format","{{.SizeRw}}",cid)))
        marker = str(uuid4())
        output(leases.execute(generation,DIRTY,marker))
        candidate = leases.snapshot(generation,"checkpoint")
        if candidate != marker:
            raise RuntimeError("Checkpoint content changed")
        leases.finish(generation, {"checkpoint":fingerprint(candidate)})
        trial["status"] = "passed"
    finally:
        leases.trial = None
        if generation:
            before_release = time.perf_counter()
            leases.release(generation)
            trial["teardownMs"] = elapsed(before_release)
            trial["cleaned"] = leases.record(generation)["state"] == "released"
    return task, candidate


def recover(db, task, candidate):
    start = time.perf_counter()
    leases = Leases(db)
    restored = leases.restore_candidate(task, candidate)
    generation = leases.acquire(task,str(uuid4()),"reviewer","restore",POLICY,{"checkpoint":restored})
    try:
        output(leases.execute(generation,READY,{"checkpoint":fingerprint(candidate)}))
        return {"readyMs":elapsed(start), "checkpointHash":fingerprint(restored), "generation":generation}
    finally:
        leases.release(generation)


def reject_dirty(db):
    class DirtyStandby(TrialLeases):
        def docker(self, *args, **kwargs):
            if args[-3:] == ("node", "-e", SEED):
                output(super().docker("exec", args[-4], "node", "-e",
                                      "require('fs').writeFileSync('/dev/shm/prior-task-token','synthetic')"))
            return super().docker(*args, **kwargs)

    leases = DirtyStandby(db)
    trial = {"mode":"fault"}
    leases.trial = trial
    leases.start = time.perf_counter()
    task = str(uuid4())
    try:
        generation = leases.acquire(task,str(uuid4()),"writer","contamination-control",POLICY,{"fixture":"unreleased"})
    except RuntimeError as err:
        if "prior-task-token" not in str(err):
            raise
    else:
        leases.release(generation)
        raise RuntimeError("Contaminated standby passed admission")
    with leases.transaction() as connection:
        row = connection.execute("SELECT * FROM leases WHERE task=?", (task,)).fetchone()
    if row is None or row["state"] != "released" or leases.owned(row) is not None or "seedMs" in trial:
        raise RuntimeError("Contaminated standby was seeded or cleanup is unconfirmed")
    return {"generation":row["generation"], "dirtyMemoryRejected":True, "sourceSeeded":False, "cleaned":True}


def save(path, data):
    # Publish complete JSON after each trial; an interrupted run retains its evidence.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write("\n")
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def benchmark(path, revision, pairs):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"status":"starting"}, stream)
    study = ROOT / ".factory" / "readiness" / uuid4().hex
    db = study / "leases.sqlite"
    report = {"format":"factory-readiness/v1","status":"running","startedUtc":datetime.now(timezone.utc).isoformat(),
              "journalDirectory":study.name, "trials":[], "recovery":{}, "pairsPerProfile":pairs}
    try:
        leases = TrialLeases(db)
        oid, packets = source_packets(revision)
        image = json.loads(output(leases.docker("image","inspect",ENGINE_IMAGE)))[0]
        report.update(sourceCommit=oid, benchmarkHash=fingerprint(Path(__file__).read_text(encoding="utf-8-sig")),
                      providerHash=fingerprint((ROOT/"scripts/environment_leases.py").read_text(encoding="utf-8-sig")),
                      engineVersion=output(leases.docker("version","--format","{{.Server.Version}}")),
                      template={"image":IMAGE,"imageId":image["Id"],"imageBytes":image["Size"],"os":image["Os"],
                                "architecture":image["Architecture"],"role":"writer","network":"none","services":[],
                                "dependencySetup":"Node built-ins in the pinned image; project dependencies not installed",
                                "mutableSharedCaches":[],"imagePull":"never","healthProbeSha256":fingerprint(READY)},
                      sources={k:{"directory":PROFILES[k],"files":len(v),"bytes":sum(len(s.encode()) for s in v.values()),
                                  "fileHashes":{n:fingerprint(s) for n,s in v.items()}} for k,v in packets.items()})
        report["sanitizationControl"] = reject_dirty(db)
        last = {}
        for i in range(pairs):
            for profile in list(PROFILES)[::1 if i%2==0 else -1]:
                modes = ("cold","warm") if (i+list(PROFILES).index(profile))%2==0 else ("warm","cold")
                for mode in modes:
                    row = {"pair":i+1,"profile":profile,"mode":mode,"status":"attempting"}
                    report["trials"].append(row)
                    save(path, report)
                    try:
                        last[profile] = sample(leases, packets[profile], row)
                    except Exception as err:
                        row.update(status="failed", error=str(err))
                        raise
                    save(path, report)
            print(f"Completed matched round {i+1}/{pairs}", flush=True)
        for profile, checkpoint in last.items():
            report["recovery"][profile] = recover(db,*checkpoint)
        versions = {r["node"] for r in report["trials"]}
        if len(versions) != 1:
            raise RuntimeError("Toolchain changed between trials")
        report["template"]["nodeVersion"] = versions.pop()
        report["status"] = "passed"
    except BaseException as err:
        report.update(status="failed", error=str(err))
        raise
    finally:
        if db.exists() and "leases" in locals():
            with leases.transaction() as connection:
                report["outstanding"] = [dict(r) for r in connection.execute(
                    "SELECT generation,state FROM leases WHERE state<>'released'")]
            if report["outstanding"]:
                report["status"] = "failed"
        report["summary"] = summarize(report["trials"])
        report["endedUtc"] = datetime.now(timezone.utc).isoformat()
        save(path,report)
    if report["status"] != "passed":
        raise RuntimeError("Study has unresolved environment leases")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New JSON evidence file")
    parser.add_argument("--revision", default="HEAD", help="Local Git commit containing both source packages")
    parser.add_argument("--pairs", type=int, default=20, help="Matched cold/warm rounds per package")
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    benchmark(args.output, args.revision, args.pairs)
