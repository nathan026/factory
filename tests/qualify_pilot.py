"""Rehearse FCT016 with five disposable controlled tasks. Use --study to retain exports."""
import argparse
from contextlib import closing, ExitStack
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import ZipFile

import test_slice

sys.path.insert(0, str(test_slice.ROOT / "scripts"))
import pilot_evaluation as evaluation


def download(base, token, task):
    req = Request(f"{base}/api/tasks/{task}/export", headers={"Authorization":"Bearer " + token})
    with urlopen(req, timeout=8) as response:
        assert response.headers.get_content_type() == "application/zip"
        return response.read()


def run(study, browser=False):
    evaluation.initialize(study)
    if any((study / "controlled").iterdir()) or any((study / "observations").iterdir()):
        raise ValueError("Use an empty rehearsal study")
    with ExitStack() as cleanup:
        app = test_slice.FactoryTest()
        cleanup.callback(app.doCleanups)
        app.setUp()
        temp = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
        leaked = "private-note-" + uuid4().hex
        ids = []
        def create(scenario):
            tid = str(uuid4())
            app.api("tasks", {"id":tid, "scenario":scenario, "message":leaked})
            ids.append(tid)
            return tid
        def until(tid, activity):
            return app.until(tid, lambda t: t["activity"] == activity)
        def park():
            tid = create("known-decision")
            app.until(tid, lambda t: t["activity"] == "Running" and t["repairs"] == 1)
            # Expire this fixture's active repair assignment. The controller performs ordinary stall handling.
            app.stop()
            with closing(sqlite3.connect(Path(app.folder) / "slice.db")) as db:
                changed = db.execute("""UPDATE dispatch_watch SET deadline=0 WHERE id=(
                    SELECT id FROM dispatch_outbox WHERE task_id=? AND id LIKE '%.Implement' ORDER BY rowid DESC LIMIT 1)""", (tid,))
                assert changed.rowcount == 1
                db.commit()
            app.start(manual=True)
            until(tid, "Blocked")
            return tid
        reserved = create("scope-change")
        until(reserved, "Waiting")
        until(create("expert-comparison"), "Delivered")
        app.api(f"tasks/{reserved}/decision", {"value":"keep"})
        until(reserved, "Delivered")
        recovered = park()
        hold = app.api(f"tasks/{recovered}/workspace")["recovery"]
        app.api(f"tasks/{recovered}/recover", {"operationId":hold["operationId"], "gateSequence":hold["gateSequence"], "note":leaked})
        until(recovered, "Delivered")
        park()
        until(create("scope-change"), "Waiting")
        before = app.api("dashboard/queue")
        for tid in ids:
            data = download(app.url, app.token, tid)
            with ZipFile(BytesIO(data)) as archive:
                for name in archive.namelist():
                    assert leaked.encode() not in archive.read(name) and app.token.encode() not in archive.read(name)
                task = json.loads(archive.read("task.json"))
                assert task["taskId"] == tid and task["activeOperatorMinutes"] is None
                assert task["independentHumanReview"] is None and task["costs"]["taskCostUsdMicros"] is None
            path = temp / (tid + ".zip")
            path.write_bytes(data)
            assert evaluation.import_bundle(study, path) == evaluation.import_bundle(study, path)
        if browser:
            import subprocess, os
            subprocess.run(["node","browser-pilot.mjs", app.url, reserved, str(temp / "browser.zip")],
                           cwd=test_slice.ROOT / "src/Factory.Web", env=dict(os.environ, FACTORY_LOCAL_TOKEN=app.token),
                           check=True, timeout=45)
            assert evaluation.read_bundle(temp / "browser.zip")["id"] == reserved
        assert app.api("dashboard/queue") == before
    report = evaluation.evaluate(study)
    facts = report["controlled"]
    assert sorted(t["activity"] for t in facts) == ["Blocked","Delivered","Delivered","Delivered","Waiting"]
    assert all(t["costs"]["taskCostUsdMicros"] is None for t in facts)
    assert any(t["resumeGates"] and any(w["kind"] == "Blocked" and w["end"] for w in t["waits"]) for t in facts)
    assert report["comparison"]["targetMet"] is None and report["comparison"]["measuredPairs"] == 0
    assert report["proposal"]["evidence"] and report["proposal"]["scope"] == "controlled rehearsal"
    return report


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--study", required=True, type=Path)
    args = cli.parse_args()
    print(evaluation.markdown(run(args.study)))
