import React, { useEffect, useState } from "react";
import { Api } from "./types";

type Record = { id: string; backupId: string; releasedUtc: string | null; reconciliation: string | null };
type Status = { restore: Record; blockers: { kind: string; id: string }[] };

export function RestorePanel({ api }: { api: Api }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [note, setNote] = useState("");
  const [fenced, setFenced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const ctrl = new AbortController();
    api<Status>("restore", { signal: ctrl.signal }).then(setStatus).catch(e => {
      if (!ctrl.signal.aborted) setError(e.message);
    });
    return () => ctrl.abort();
  }, [api]);
  async function reconcile(operationId?: string) {
    if (!status || busy) return;
    setBusy(true);
    try {
      const body = { restoreId: status.restore.id, note, sourceFenced: fenced };
      if (operationId) {
        await api("restore/retire", { body: { ...body, operationId } });
        setStatus(await api<Status>("restore"));
      } else {
        const record = await api<Record>("restore/release", { body });
        setStatus({ ...status, restore: record });
      }
      setError("");
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="card reading" aria-label="Restore reconciliation">
    <h2>Restored controller paused</h2>
    {error && <p role="alert">{error}</p>}
    {status && <>
      <p>Backup {status.restore.backupId}</p>
      {status.restore.releasedUtc ? <>
        <p>Release recorded. Restart this controller to enable dispatch.</p><p>{status.restore.reconciliation}</p>
      </> : <form onSubmit={e => { e.preventDefault(); void reconcile(); }}>
        <label>Reconciliation evidence<textarea value={note} maxLength={2000} required onChange={e => setNote(e.target.value)} /></label>
        <label><input type="checkbox" checked={fenced} onChange={e => setFenced(e.target.checked)} /> I fenced the source and its leases and checked external activity since backup.</label>
        {status.blockers.length > 0 && <ul>{status.blockers.map(b => <li key={b.kind + b.id}>
          {b.kind} - {b.id} {["Dispatch", "CI"].includes(b.kind)
            ? <button type="button" disabled={busy || !note.trim() || !fenced} onClick={() => void reconcile(b.id)}>Retire fenced operation</button>
            : <span>Reconcile on the source and take a fresh checkpoint.</span>}
        </li>)}</ul>}
        <button disabled={busy || !note.trim() || !fenced || status.blockers.length > 0}>Release restored controller</button>
      </form>}
    </>}
  </section>;
}
