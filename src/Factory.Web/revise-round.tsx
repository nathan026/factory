import React, { useState } from "react";
import { Api, Workspace } from "./types";

type Preview = {
  approval: { taskId: string; checkpoint: string; baseSha: string; policyHash: string };
  goal: string;
  paths: string[];
  criteria: { id: string; text: string }[];
};

export function ReviseRound({task, ready, api, update}: { task: Workspace; ready: boolean; api: Api; update: () => void }) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const endpoint = "github/issues/" + task.repository!.number + "/revision";
  async function submit() {
    setError(""); setBusy(true);
    try {
      if (preview) {
        await api(endpoint, {body: preview.approval});
        setPreview(null); update();
      } else {
        setPreview(await api<Preview>(endpoint + "?taskId=" + encodeURIComponent(task.id)));
      }
    } catch (e) {
      setPreview(null); setError((e as Error).message);
    } finally { setBusy(false); }
  }
  return <section aria-label="Revise verification">
    {preview && <>
      <h3>Review the next round</h3>
      <p>{preview.goal}</p>
      <p>Start from <code>{preview.approval.baseSha}</code>.</p>
      <h4>Files the writer can change</h4>
      <ul>{preview.paths.map(path => <li key={path}><code>{path}</code></li>)}</ul>
      <h4>Acceptance checks</h4>
      <ul>{preview.criteria.map(c => <li key={c.id}>{c.text}</li>)}</ul>
      <p>Approval starts a new planning round on this task. Earlier attempts stay in its history.</p>
    </>}
    {error && <p role="alert">{error}</p>}
    <div className="actions">
      <button disabled={!ready || busy} onClick={submit}>{preview ? "Approve and resume" : "Revise and resume"}</button>
      {preview && <button className="secondary" disabled={busy} onClick={() => setPreview(null)}>Cancel</button>}
    </div>
  </section>;
}
