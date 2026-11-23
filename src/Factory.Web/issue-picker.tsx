import React, { useEffect, useState } from "react";
import { Api, GitHubIntake } from "./types";

type Item = { number: number; id: number; title: string; taskId: string | null; configured: boolean; canReview: boolean; reason: string | null };
type IssuePage = { items: Item[]; nextPage: number | null };
type Scope = { goal: string; paths: string[]; checks: string[] };
type Preview = {
  repositoryId: number; issueId: number; issueHash: string; baseSha: string; policyHash: string;
  assigned: boolean; title: string; body: string | null; paths: string[]; checks: string[]; canBegin: boolean;
  saved: {id: string; request: Scope} | null;
};
type Props = { api: Api; ready: boolean; created: (id:string)=>void; cancel?: ()=>void; intake: GitHubIntake; goal: string };

export function GitHubIssueForm({api,ready,created,cancel,intake,goal}: Props) {
  const [page,setPage]=useState(1), [reload,refresh]=useState(0);
  const [list,setList]=useState<IssuePage|null>(null), [number,select]=useState(0);
  const [busy,setBusy]=useState(false), [error,setError]=useState("");
  useEffect(()=>{
    const ctrl=new AbortController();
    setList(null); setError("");
    api<IssuePage>("github/issues?page="+page,{signal:ctrl.signal}).then(result=>{
      if (ctrl.signal.aborted) return;
      setList(result); select(result.items[0]?.number ?? 0);
    }).catch(e=>{if(!ctrl.signal.aborted)setError(e.message);});
    return ()=>ctrl.abort();
  },[api,page,reload]);
  const item=list?.items.find(i=>i.number===number);
  const configured=intake.issues.find(i=>i.id===item?.id && i.number===number);
  async function begin() {
    if (!configured || !ready || busy) return;
    setBusy(true); setError("");
    try {created((await api<{taskId:string}>(`github/issues/${number}/start`, {body:{
      repositoryId:intake.repositoryId,issueId:configured.id,scopeHash:configured.scopeHash},timeoutMs:90000})).taskId);}
    catch(e){setError((e as Error).message);} finally {setBusy(false);}
  }
  return <section className="card start"><h1>Start GitHub issue</h1>
    {list && list.items.length>0 && <label>Open issue
      <select value={number} disabled={busy} onChange={e=>{select(Number(e.target.value));setError("");}}>
        {list.items.map(i=><option key={i.id} value={i.number}>#{i.number} · {i.title}</option>)}
      </select></label>}
    {!list && !error && <p role="status">Loading open issues…</p>}
    {list && !list.items.length && <p>No open issues on this page.</p>}
    {item && (item.taskId ? <button disabled={!ready} onClick={()=>created(item.taskId!)}>Open existing task</button> :
      item.configured ? <><p>{item.reason==="Eligible" ? goal : item.reason}</p>
        <button disabled={!ready || busy || item.reason!=="Eligible"} onClick={begin}>Begin</button></> :
      item.canReview ? <IssueScope key={item.id} api={api} ready={ready} item={item} created={created}/> :
      <p>This issue has no approved scope. Desk scope approval is disabled for this repository.</p>)}
    {error && <p role="alert">{error}</p>}
    <div className="actions">
      {page>1 && <button className="secondary" disabled={busy} onClick={()=>setPage(page-1)}>Previous page</button>}
      {list?.nextPage && <button className="secondary" disabled={busy} onClick={()=>setPage(list.nextPage!)}>Next page</button>}
      <button className="secondary" disabled={busy} onClick={()=>refresh(n=>n+1)}>Refresh issues</button>
      {cancel && <button className="secondary" disabled={busy} onClick={cancel}>Cancel</button>}
    </div>
  </section>;
}

function issueChecklist(body: string | null): string[] {
  const checks: string[] = [];
  let inFence = "";
  for (const line of (body ?? "").split(/\r?\n/)) {
    const fence = line.trimStart().match(/^(`{3,}|~{3,})/);
    if (fence) {
      if (!inFence) inFence = fence[1];
      else if (fence[1].startsWith(inFence)) inFence = "";
      continue;
    }
    if (inFence) continue;
    const item = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.+?)\s*$/);
    if (item && item[1].length <= 2000 && !checks.includes(item[1])) checks.push(item[1]);
    if (checks.length === 16) break;
  }
  return checks;
}

function IssueScope({api,ready,item,created}: {api:Api;ready:boolean;item:Item;created:(id:string)=>void}) {
  const [preview,setPreview]=useState<Preview|null>(null), [editing,edit]=useState(false), [busy,setBusy]=useState(false), [error,setError]=useState("");
  const [goal,setGoal]=useState(""), [files,setFiles]=useState(""), [tests,setTests]=useState("");
  const route="github/issues/"+item.number;
  async function load() {
    const p=await api<Preview>(route+"/scope");
    setPreview(p); setGoal(p.saved?.request.goal ?? p.title); setFiles((p.saved?.request.paths ?? []).join("\n"));
    setTests((p.saved?.request.checks ?? issueChecklist(p.body)).join("\n"));
    return p;
  }
  async function act(action:"review"|"save"|"start") {
    setError(""); setBusy(true);
    try {
      if(action==="review"){const p=await load(); edit(!p.saved);}
      else if(action==="save" && preview){
        const lines=(text:string)=>text.split("\n").map(s=>s.trim()).filter(Boolean);
        await api(route+"/scope",{body:{repositoryId:preview.repositoryId,issueId:preview.issueId,issueHash:preview.issueHash,
          baseSha:preview.baseSha,policyHash:preview.policyHash, previous:preview.saved?.id ?? null,
          goal:goal.trim(),paths:lines(files),checks:lines(tests)}});
        await load(); edit(false);
      } else if(action==="start" && preview?.saved) {
        created((await api<{taskId:string}>(route+"/start",{body:{repositoryId:preview.repositoryId,
          issueId:preview.issueId,scopeHash:preview.issueHash,scopeApproval:preview.saved.id},timeoutMs:90000})).taskId);
      }
    } catch(e){setError((e as Error).message);setPreview(null);edit(false);} finally{setBusy(false);}
  }
  return <section aria-label="Issue work scope">
    {!preview ? <button disabled={!ready||busy} onClick={()=>act("review")}>Review scope</button> :
      <><details><summary>Original GitHub issue and base revision</summary>
        <div style={{whiteSpace:"pre-wrap"}}>{preview.body}</div><p>Base revision <code>{preview.baseSha}</code></p>
      </details>
      {editing ? <>
        <label>Work to complete<textarea value={goal} onChange={e=>setGoal(e.target.value)} maxLength={2000}/></label>
        <label>What should work when issue #{item.number} is done? Enter one check per line.
          <textarea rows={6} value={tests} onChange={e=>setTests(e.target.value)}/></label>
        <details><summary>Files the writer may change</summary>
          <label>Repository paths, one per line (optional)<textarea value={files} onChange={e=>setFiles(e.target.value)}/></label>
        </details>
        {preview.assigned ? <details><summary>Files and automated checks assigned to issue #{item.number}</summary>
          <ul>{[...preview.paths,...preview.checks].map(s=><li key={s}>{s}</li>)}</ul>
          <button className="secondary" disabled={busy} onClick={()=>{setFiles(preview.paths.join("\n"));setTests(preview.checks.join("\n"));}}>Use the assigned files and checks</button>
        </details> : <p>You can save this checklist. Before work can start, the factory needs approved files and automated checks for issue #{item.number}.</p>}
        <button disabled={!ready||busy||!goal.trim()||!tests.trim()} onClick={()=>act("save")}>{preview.assigned ? "Approve scope" : "Save checklist"}</button>
      </> : <>
        <h2>Saved checklist for issue #{item.number}</h2><p>{goal}</p><ul>{files.split("\n").filter(Boolean).map(p=><li key={p}><code>{p}</code></li>)}</ul>
        <ul>{tests.split("\n").map(c=><li key={c}>{c}</li>)}</ul>
        <p role="status">{preview.canBegin ? "Scope approved. Ready to begin." : preview.assigned
          ? "Review the current issue, base revision and assigned checks before starting."
          : `Checklist saved. Work can start after files and automated checks are assigned to issue #${item.number} and you approve the scope.`}</p>
        {preview.canBegin && <button disabled={!ready||busy} onClick={()=>act("start")}>Begin</button>}
        <button className="secondary" disabled={busy} onClick={()=>edit(true)}>Edit scope</button>
      </>}</>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
