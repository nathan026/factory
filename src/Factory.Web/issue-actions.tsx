import React, { useState } from "react";
import type { Api, IssueAction, Workspace } from "./types";

type Preview = { cleanupHash?: string; branches?: {target:{branch:string; pull:number; headSha:string}; keep:string|null}[]; summary: string; baseSha: string; path: string; paths?: string[]; criteria?: { id: string; text: string }[]; merged: boolean; open: boolean; targetBranch: string; state: IssueAction | null };

export function IssueActions({task, api, ready, update}: {task: Workspace; api: Api; ready: boolean; update: () => void}) {
  const repo = task.repository!;
  const draft = repo.draft!;
  const route = "github/issues/" + repo.number + "/actions";
  const [form, setForm] = useState<"follow-up" | "finish" | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const saved = task.issueAction;
  const [scope, setScope] = useState(saved?.scope ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function open(kind: "follow-up" | "finish") {
    setBusy(true); setError("");
    try {
      const p = await api<Preview>(route+"?taskId="+task.id+"&headSha="+draft.headSha+(kind==="finish" ? "&finish=true" : ""));
      setPreview(p); setForm(kind);
      if (p.state) setScope(p.state.scope);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function submit(withdraw = false) {
    if (!form || !preview || busy) return;
    setBusy(true); setError("");
    try {
      await api(route+"/"+(withdraw ? "withdraw" : form), {body:{taskId:task.id,headSha:draft.headSha,baseSha:preview.state?.baseSha ?? preview.baseSha,
        actionId:preview.state?.id, cleanupHash:form==="finish" ? preview.cleanupHash ?? undefined : undefined, scope:form==="finish" ? "" : scope.trim()}});
      setForm(null); update();
    } catch (e) { setError((e as Error).message); update(); } finally { setBusy(false); }
  }
  if (saved?.stage==="finished") return <p>Issue closed. <a href={repo.issueUrl+"#issuecomment-"+saved.commentId} target="_blank" rel="noopener noreferrer">Read the final summary</a></p>;
  return <div>
    {!form && <div className="actions">
      {(!saved || saved.kind==="follow-up") && <button disabled={!ready || busy} onClick={()=>open("follow-up")}>{saved ? "Reconcile follow-up" : "Request follow-up"}</button>}
      {(!saved || saved.kind==="finish") && <button disabled={!ready || busy} onClick={()=>open("finish")}>{saved ? "Reconcile finish" : "Finish issue"}</button>}
    </div>}
    {form && preview && <section aria-label="Confirm issue action">
      {form==="follow-up" ? <>
        {!preview.state && task.messages.length>0 && <button onClick={()=>setScope(task.messages[task.messages.length-1].text.slice(0,2000))}>Use last desk message</button>}
        <label>Approved follow-up scope<textarea maxLength={2000} readOnly={!!preview.state} value={scope} onChange={e=>setScope(e.target.value)} /></label>
        <p>The factory will post this request on issue #{repo.number} and start a new round on this task.</p>
        <p>Approved files</p>
        <ul>{(preview.paths ?? [preview.path]).map(path=><li key={path}><code>{path}</code></li>)}</ul>
        {preview.criteria && <><p>Acceptance checks</p><ul>{preview.criteria.map(c=><li key={c.id}>{c.text}</li>)}</ul></>}
        <p>Start from {preview.targetBranch} at <code>{preview.state?.baseSha ?? preview.baseSha}</code>.</p>
        {!preview.merged && (repo.baseSha===preview.baseSha
          ? <p>The previous PR stays open. The new round starts with its saved changes.</p>
          : <p>The base has changed. This round starts from the displayed base. Include any work you still need from the earlier PR in your request.</p>)}
      </> : <>
        <p>The factory will post a work summary on issue #{repo.number} and close the issue.
        </p>
        {preview.branches && (preview.branches.length ? <ul aria-label="Branch cleanup">{preview.branches.map(({target,keep})=>
          <li key={target.branch}>{keep ?? <>Delete <code>{target.branch}</code></>} (PR #{target.pull}, <code>{target.headSha.slice(0,12)}</code>)</li>)}</ul>
          : <p>No eligible branches to delete.</p>)}
        <details><summary>Preview the public comment</summary><pre style={{whiteSpace:"pre-wrap"}}>{preview.summary}</pre></details>
        {!preview.merged && <p>Merge PR #{draft.number} before finishing this issue.</p>}
      </>}
      <div className="actions">
        <button disabled={busy || !ready || (form==="finish" ? !preview.merged : !scope.trim())} onClick={()=>submit()}>
          {form==="follow-up" ? "Approve scope and start follow-up" : "Post summary and close issue"}</button>
        {form==="follow-up" && preview.state && <button disabled={busy || !ready} onClick={()=>submit(true)}>Withdraw pending request</button>}
        <button disabled={busy} onClick={()=>setForm(null)}>Cancel</button>
      </div>
    </section>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
