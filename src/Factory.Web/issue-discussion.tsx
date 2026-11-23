import React, { useEffect, useRef, useState } from "react";
import { Api } from "./types";

type Proposal = {goal: string; paths: string[]; checks: string[]};
type Message = {id: string; text: string; reply: string; status: string};
type Conversation = {
  repositoryId: number; issueId: number; issueHash: string; baseSha: string; policyHash: string;
  ownerAvailable: boolean; stale: boolean; messages: Message[]; proposal: Proposal | null;
  canBegin: boolean; scopeApproval: string | null; savedScopeId: string | null; requestRecorded: boolean;
};

export function IssueDiscussion({api, number, ready, created}: {
  api: Api; number: number; ready: boolean; created: (id: string) => void;
}) {
  const route = "github/issues/" + number;
  const [view,setView] = useState<Conversation | null>(null);
  const [text,setText] = useState("");
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState("");
  const [refresh,setRefresh] = useState(0);
  const retry = useRef<{id:string; text:string; previous:string|null; issueHash:string; baseSha:string} | null>(null);
  const latest = view?.messages.at(-1);
  const pending = latest?.status === "Pending";

  const transcript = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = transcript.current;
    if (element) element.scrollTop = element.scrollHeight;
  },[latest?.id, latest?.status, latest?.reply]);

  useEffect(() => {
    if (!pending) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const next = await api<Conversation>(route+"/discussion?poll=true",{signal:abort.signal});
        if (abort.signal.aborted) return;
        setView(next);
        setError("");
        if (next.messages.at(-1)?.status === "Pending") timer = setTimeout(poll,3000);
      } catch (e) {
        if (!abort.signal.aborted) setError((e as Error).message);
      }
    }
    timer = setTimeout(poll,1500);
    return () => { clearTimeout(timer); abort.abort(); };
  },[api,route,pending,refresh]);

  async function send(current: Conversation, utterance: string) {
    const previous = current.messages.at(-1)?.id ?? null;
    const prior = retry.current;
    const cmd = prior && prior.text === utterance && prior.previous === previous &&
      prior.issueHash === current.issueHash && prior.baseSha === current.baseSha
      ? prior : {id:crypto.randomUUID(),text:utterance,previous,issueHash:current.issueHash,baseSha:current.baseSha};
    retry.current = cmd;
    const next = await api<Conversation>(route+"/discussion",{body:{...cmd,repositoryId:current.repositoryId,
      issueId:current.issueId,policyHash:current.policyHash},timeoutMs:90000});
    retry.current = null;
    setView(next); setText("");
  }

  async function act(action: "open"|"send"|"reload"|"begin"|"record") {
    if (busy) return;
    setBusy(true); setError("");
    try {
      if (action === "open" || action === "reload") {
        const current = await api<Conversation>(route+"/discussion",{timeoutMs:30000});
        setView(current); setRefresh(n=>n+1);
        if (action === "open" && current.ownerAvailable && !current.messages.length) {
          await send(current,"Review this issue and the approved repository files. Propose a work plan and checks. Ask me about any requirement you still need to clarify.");
        }
      } else if (action === "send" && view && text.trim()) {
        await send(view,text.trim());
      } else if (action === "begin" && view?.canBegin && view.scopeApproval) {
        const result = await api<{taskId:string}>(route+"/start",{body:{repositoryId:view.repositoryId,
          issueId:view.issueId,scopeHash:view.issueHash,scopeApproval:view.scopeApproval},timeoutMs:90000});
        created(result.taskId);
      } else if (action === "record" && view?.proposal) {
        await api(route+"/scope",{body:{repositoryId:view.repositoryId,issueId:view.issueId,issueHash:view.issueHash,
          baseSha:view.baseSha,policyHash:view.policyHash,previous:view.savedScopeId,ownerMessage:latest?.id,...view.proposal}});
        setView(await api<Conversation>(route+"/discussion?poll=true"));
      }
    } catch(e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section aria-label="Discuss issue with lifecycle owner">
    {!view ? <button disabled={!ready || busy} onClick={()=>act("open")}>Discuss issue #{number}</button> : <>
      <h2>Discuss issue #{number}</h2>
      {!view.ownerAvailable && <p role="status">The subscription-backed owner is unavailable. You can still review the issue scope manually.</p>}
      <div className="scope-transcript" ref={transcript} role="log" aria-label="Scope conversation" tabIndex={0}>
      {view.messages.map((message,index)=><div key={message.id}>
        <p><strong>{index===0 && message.text.startsWith("Review this issue and the approved repository files.") ? "Opening request" : "You"}</strong><br/>{message.text}</p>
        <p style={{whiteSpace:"pre-wrap"}}><strong>Lifecycle owner</strong><br/>{message.reply}</p>
      </div>)}
      </div>
      {pending && <p role="status">Waiting for the lifecycle owner…</p>}
      {view.stale && <p role="status">The issue or repository has changed. Ask the owner to reassess the scope against the current revision.</p>}
      {view.proposal && <section aria-label="Proposed scope"><h3>Proposed scope</h3><p>{view.proposal.goal}</p>
        <h4>Files to change</h4><ul>{view.proposal.paths.map(p=><li key={p}><code>{p}</code></li>)}</ul>
        <h4>How to verify the change</h4><ul>{view.proposal.checks.map(c=><li key={c}>{c}</li>)}</ul>
        {view.canBegin ? <button disabled={!ready || busy || pending || view.stale} onClick={()=>act("begin")}>Begin coding</button>
          : !pending && !view.stale && <><p>{view.requestRecorded ? "Scope request saved. A project operator must configure the missing file permissions or checks before coding can begin." :
            "This plan needs file permissions or automated checks beyond those assigned to this issue. Ask the owner to revise the plan, or record the request for project setup."}</p>
            {!view.requestRecorded && <button className="secondary" disabled={!ready || busy} onClick={()=>act("record")}>Record scope request</button>}</>}
      </section>}
      <label>Message the lifecycle owner<textarea value={text} maxLength={4000} onChange={e=>setText(e.target.value)} /></label>
      <button disabled={!ready || busy || pending || !view.ownerAvailable || !text.trim()} onClick={()=>act("send")}>Send message</button>
    </>}
    {error && <><p role="alert">{error}</p>{view && <button className="secondary" disabled={busy} onClick={()=>act("reload")}>Refresh discussion</button>}</>}
  </section>;
}
