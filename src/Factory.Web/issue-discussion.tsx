import React, { useEffect, useRef, useState } from "react";
import { Api } from "./types";

type Proposal = {goal: string; paths: string[]; checks: string[]};
type Message = {id: string; text: string; reply: string; status: string; assessment?: { investigations: string[]; decisions: string[] }};
type Conversation = {
  repositoryId: number; issueId: number; issueHash: string; baseSha: string; policyHash: string;
  assigned: boolean; ownerAvailable: boolean; stale: boolean; messages: Message[]; proposal: Proposal | null;
  canBegin: boolean; scopeApproval: string | null; savedScopeId: string | null; requestRecorded: boolean;
};

const preparationPrompt = "Prepare this issue for coding. Read the issue, prior decisions and permitted repository files. " +
  "Confirm the intended outcome and how we can verify it. Investigate what you can now. " +
  "List the investigations that remain and the specific decisions or permissions you need from me. " +
  "When the scope is clear, propose the exact files and checks. Tell me the next action.";

function readinessReason(view: Conversation, last?: Message) {
  if (view.stale) return "The issue, repository revision or project rules have changed since the last assessment. Reassess before starting.";
  if (last?.status === "Pending") return "The lifecycle owner is checking the issue and repository files.";
  if (view.canBegin && view.scopeApproval) return "The factory has approved the files and verification checks for the current issue and revision.";
  if (!view.ownerAvailable) return "The subscription-backed owner is unavailable. Manual scope review is available below.";
  if (last?.status === "RecoveredReply") return "The previous reply was recovered for reference. Ask for a current assessment before starting.";
  if (last?.assessment?.decisions.length) return "Answer the open decisions in the conversation.";
  if (last?.assessment?.investigations.length) return "The owner has identified work to investigate before coding.";
  if (view.requestRecorded) return "The scope request is saved. A project operator must configure the required files and automated checks.";
  if (view.proposal) return "The factory has not approved this proposed scope. Review its files and checks below.";
  if (last?.status === "NeedsScope") return "The owner needs exact repository paths and valid checks for a coding proposal.";
  if (last?.status === "NeedsAssessment") return "The last reply did not establish what remains. Ask the owner to reassess.";
  if (last && last.status !== "Answered") return last.reply;
  return "The factory needs a current work plan with approved files and checks.";
}

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

  const approved = Boolean(view?.canBegin && view.scopeApproval && !view.stale && !pending);
  const historical = view?.stale || latest?.status === "RecoveredReply";
  const assessment = historical ? undefined : latest?.assessment;
  const priorFinding = (historical || !assessment) && latest &&
    ["Answered","RecoveredReply","NeedsScope","NeedsAssessment"].includes(latest.status) ?
    latest.reply.split("\n\nSuggested checks\n")[0] : null;
  const composer = useRef<HTMLTextAreaElement>(null);
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
        if (next.messages.at(-1)?.status === "Pending") {
          timer = setTimeout(poll,3000);
        } else {
          const current = await api<Conversation>(route+"/discussion",{signal:abort.signal,timeoutMs:30000});
          if (!abort.signal.aborted) setView(current);
        }
        if (!abort.signal.aborted) setError("");
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
    setView(next);
  }

  async function act(action: "open"|"send"|"reload"|"begin"|"record"|"assess") {
    if (busy) return;
    setBusy(true); setError("");
    try {
      if (action === "open" || action === "reload" || action === "assess") {
        const current = await api<Conversation>(route+"/discussion",{timeoutMs:30000});
        setView(current); setRefresh(n=>n+1);
        if (current.ownerAvailable && ((action === "open" && !current.messages.length) ||
            (action === "assess" && current.messages.at(-1)?.status !== "Pending" && !current.canBegin))) {
          await send(current,preparationPrompt);
        }
      } else if (action === "send" && view && text.trim()) {
        await send(view,text.trim()); setText("");
      } else if (action === "begin" && view?.canBegin && view.scopeApproval) {
        const result = await api<{taskId:string}>(route+"/start",{body:{repositoryId:view.repositoryId,
          issueId:view.issueId,scopeHash:view.issueHash,scopeApproval:view.scopeApproval},timeoutMs:90000});
        created(result.taskId);
      } else if (action === "record" && view?.proposal) {
        await api(route+"/scope",{body:{repositoryId:view.repositoryId,issueId:view.issueId,issueHash:view.issueHash,
          baseSha:view.baseSha,policyHash:view.policyHash,previous:view.savedScopeId,ownerMessage:latest?.id,...view.proposal}});
        setView(await api<Conversation>(route+"/discussion",{timeoutMs:30000}));
      }
    } catch(e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section aria-label="Discuss issue with lifecycle owner">
    {!view ? <button disabled={!ready || busy} onClick={()=>act("open")}>Discuss issue #{number}</button> : <>
      <h2>Discuss issue #{number}</h2>
      <section className="scope-readiness" aria-label="Coding readiness">
        <h3>Coding readiness</h3>
        <p role="status"><strong>{approved ? "Ready to begin" : "Not ready to begin"}</strong></p>
        <p>{readinessReason(view,latest)}</p>
        {assessment && !approved && <>
          <h4>Owner to investigate</h4>
          {assessment.investigations.length ? <ul>{assessment.investigations.map(s=><li key={s}>{s}</li>)}</ul> : <p>No open investigation reported.</p>}
          <h4>Decisions for you</h4>
          {assessment.decisions.length ? <ul>{assessment.decisions.map(s=><li key={s}>{s}</li>)}</ul> : <p>No decision requested.</p>}
        </>}
        {!approved && !view.assigned && <p><strong>Project setup needed.</strong> This issue has no assigned files or automated checks for coding.</p>}
        {approved ? <button disabled={!ready || busy} onClick={()=>act("begin")}>Begin coding</button>
          : !pending && view.ownerAvailable && (assessment?.decisions.length ?
            <button className="secondary" disabled={!ready || busy} onClick={()=>composer.current?.focus()}>Answer the owner</button> :
            <button className="secondary" disabled={!ready || busy} onClick={()=>act("assess")}>
            {view.messages.length ? "Reassess readiness" : "Assess readiness"}
          </button>)}
        {priorFinding && !approved && <><h4>{historical ? "Previous owner finding" : "Latest owner finding"}</h4>
          <p style={{whiteSpace:"pre-wrap"}}>{priorFinding}</p></>}
      </section>
      <div className="scope-transcript" ref={transcript} role="log" aria-label="Scope conversation" tabIndex={0}>
      {view.messages.map((message,index)=><div key={message.id}>
        <p><strong>{index===0 && (message.text === preparationPrompt || message.text.startsWith("Review this issue and the approved repository files.")) ? "Opening request" : "You"}</strong><br/>{message.text}</p>
        <p style={{whiteSpace:"pre-wrap"}}><strong>Lifecycle owner</strong><br/>{message.reply}</p>
        {message.status === "NeedsScope" && <p>The reply is saved. The proposed scope needs exact repository paths and valid checks before it can be approved.</p>}
        {message.status === "RecoveredReply" && <p>The factory recovered this earlier reply from its verified receipt. The original request expired. Coding still needs an approved scope.</p>}
      </div>)}
      </div>
      {view.proposal && <section aria-label="Proposed scope"><h3>Proposed scope</h3><p>{view.proposal.goal}</p>
        <h4>Files to change</h4><ul>{view.proposal.paths.map(p=><li key={p}><code>{p}</code></li>)}</ul>
        <h4>How to verify the change</h4><ul>{view.proposal.checks.map(c=><li key={c}>{c}</li>)}</ul>
        {!approved && !pending && !view.stale && !view.requestRecorded &&
          <button className="secondary" disabled={!ready || busy} onClick={()=>act("record")}>Record scope request</button>}
      </section>}
      <label>Message the lifecycle owner<textarea ref={composer} value={text} maxLength={4000} onChange={e=>setText(e.target.value)} /></label>
      <button disabled={!ready || busy || pending || !view.ownerAvailable || !text.trim()} onClick={()=>act("send")}>Send message</button>
    </>}
    {error && <><p role="alert">{error}</p>{view && <button className="secondary" disabled={busy} onClick={()=>act("reload")}>Refresh discussion</button>}</>}
  </section>;
}
