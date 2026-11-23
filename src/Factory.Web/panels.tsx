import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Api, Evidence, Exchange, Workspace } from "./types";

export function Conversation({
  task,
  api,
  ready,
  changed,
}: {
  task: Workspace;
  api: Api;
  ready: boolean;
  changed: () => void;
}) {
  const key = "factory-message-" + task.id;
  const [draft, setDraft] = useState(() => sessionStorage.getItem(key) || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState<Exchange | null>(null);
  const historyPane = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  // Use the acknowledgement immediately. Polling supplies subsequent owner updates.
  const latest = sent && !task.messages.some(m => m.id === sent.id) ? sent : task.messages.at(-1);
  useLayoutEffect(() => {
    const pane = historyPane.current;
    if (pane && followLatest.current) pane.scrollTop = pane.scrollHeight;
  });
  const messages = new Map<number, typeof task.messages>();
  for (const message of task.messages) {
    if (message.id === latest?.id) continue;
    const group = messages.get(message.afterEntry) ?? [];
    group.push(message);
    messages.set(message.afterEntry, group);
  }
  function edit(text: string) {
    setDraft(text);
    sessionStorage.setItem(key, text);
    sessionStorage.removeItem(key + "-id");
  }
  async function send(e: React.FormEvent) {
    e.preventDefault();
    if (!draft.trim() || busy || !ready) return;
    const id = sessionStorage.getItem(key + "-id") || crypto.randomUUID();
    sessionStorage.setItem(key + "-id", id);
    setBusy(true);
    setError("");
    try {
      const exchange = await api<Exchange>("tasks/" + task.id + "/messages", { body: { id, text: draft.trim() } });
      setSent(exchange);
      edit("");
      changed();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const exchanges = (n: number) =>
    messages.get(n)?.map((m) => (
      <React.Fragment key={m.id}>
        <Line actor="Operator" text={m.text} />
        <Line actor="Lifecycle owner" text={m.reply} />
      </React.Fragment>
    ));
  return (
    <section className="conversation" aria-label="Task conversation">
      <div
        className="conversation-history"
        ref={historyPane}
        tabIndex={0}
        onScroll={({ currentTarget: pane }) => {
          followLatest.current = pane.scrollHeight - pane.clientHeight - pane.scrollTop <= 1;
        }}
      >
        <h3>Conversation &amp; activity</h3>
        <ol className="thread">
          {exchanges(0)}
          {task.history.map((e) => (
            <React.Fragment key={e.number}>
              <Line actor={e.actor} text={e.text} />
              {exchanges(e.number)}
            </React.Fragment>
          ))}
        </ol>
        {latest && (
          <section className="reading latest-reply" aria-label="Latest owner reply">
            <h3>Latest owner reply</h3>
            <p className="reply-text" aria-live="polite">{latest.reply}</p>
            <details><summary>Your message</summary><p>{latest.text}</p></details>
          </section>
        )}
      </div>
      <form className="composer" onSubmit={send}>
        <label htmlFor="discussion">Message the lifecycle owner</label>
        <textarea
          id="discussion"
          placeholder="Ask about progress, evidence or a decision…"
          value={draft}
          maxLength={4000}
          disabled={busy}
          onChange={(e) => edit(e.target.value)}
          rows={2}
        />
        <div className="composer-foot">
          <small>Discussion is saved to this task.</small>
          <button disabled={!ready || busy || !draft.trim()}>{busy ? "Sending…" : "Send message"}</button>
        </div>
        {error && <p role="alert">{error}</p>}
      </form>
    </section>
  );
}

function Line({ actor, text }: { actor: string; text: string }) {
  return (
    <li className="line">
      <span aria-hidden="true" className={"avatar " + (actor === "Operator" ? "operator" : "")}>
        {actor
          .split(" ")
          .map((s) => s[0])
          .slice(0, 2)
          .join("")
          .toUpperCase()}
      </span>
      <div>
        <strong>{actor}</strong>
        <p>{text}</p>
      </div>
    </li>
  );
}

export function RecoverOperation({
  id,
  recovery,
  api,
  ready,
  changed,
}: {
  id: string;
  recovery: NonNullable<Workspace["recovery"]>;
  api: Api;
  ready: boolean;
  changed: () => void;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function resume(e: React.FormEvent) {
    e.preventDefault();
    if (!ready || busy || !note.trim()) return;
    setBusy(true);
    setError("");
    try {
      await api("tasks/" + id + "/recover", {
        body: {
          operationId: recovery.operationId,
          gateSequence: recovery.gateSequence,
          note: note.trim(),
        },
        minimal: true,
      });
      changed();
    } catch (e) {
      setError((e as Error).message);
      changed();
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="decision" onSubmit={resume}>
      <h2>{recovery.kind}</h2>
      <p>{recovery.reason}</p>
      <details>
        <summary>Interrupted operation</summary>
        <p>
          Operation <code>{recovery.operationId}</code>
        </p>
        <p>Parked {new Date(recovery.since).toLocaleString()}</p>
        {recovery.schedulerId && (
          <p>
            Scheduler run <code>{recovery.schedulerId}</code>
          </p>
        )}
        {recovery.sessionId && (
          <p>
            Worker session <code>{recovery.sessionId}</code>
          </p>
        )}
        {recovery.retainedOutputHash && (
          <p>
            Unapplied result <code>{recovery.retainedOutputHash}</code>
          </p>
        )}
      </details>
      <label>
        Recovery note
        <textarea
          required
          maxLength={2000}
          value={note}
          disabled={busy}
          placeholder="What did you check before resuming?"
          onChange={(e) => setNote(e.target.value)}
        />
      </label>
      <p>Resuming starts a fresh fixture attempt from this checkpoint. The old operation stays retired.</p>
      <button disabled={!ready || busy || !note.trim()}>Resume from checkpoint</button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}

export function ExpertAdvice({ advice }: { advice: NonNullable<Workspace["advice"]> }) {
  const consultation = advice.consultation;
  return (
    <>
      <p>
        <strong>Recommendation</strong> {advice.recommendation}
      </p>
      <p>
        <strong>Alternative</strong> {advice.alternative}
      </p>
      <p>
        <strong>Consequence</strong> {advice.consequence}
      </p>
      {consultation ? (
        <details>
          <summary>Expert reasoning and sources</summary>
          <p>
            {consultation.domain} · {consultation.profileVersion} · Brief v{consultation.briefVersion}
          </p>
          <p>
            <strong>Question</strong> {consultation.question.text}
          </p>
          <p>
            <strong>Conclusion</strong> {consultation.conclusion}
          </p>
          <p>
            <strong>Assumption</strong> {consultation.assumption}
          </p>
          <p>
            <strong>Uncertainty</strong> {consultation.uncertainty}
          </p>
          <p>
            <strong>Decision authority</strong> {consultation.requiredAuthority}
          </p>
          <ul>
            {consultation.sources.map((s) => (
              <li key={s.path}>
                {s.path} <code>{s.digest.slice(0, 12)}</code>
              </li>
            ))}
          </ul>
        </details>
      ) : (
        <small>{advice.source}</small>
      )}
    </>
  );
}

export function Brief({ task }: { task: Workspace }) {
  const { brief } = task;
  return (
    <section className="reading">
      <h3>
        Accepted brief <small>v{brief.version}</small>
      </h3>
      <p>{task.goal}</p>
      <h4>Acceptance criteria</h4>
      <ul>
        {brief.requirements.criteria.map((c) => (
          <li key={c.id}>{c.text}</li>
        ))}
      </ul>
      <h4>Constraints</h4>
      <ul>
        {brief.requirements.constraints.map((x) => (
          <li key={x}>{x}</li>
        ))}
      </ul>
      <h4>Outside scope</h4>
      <ul>
        {brief.requirements.exclusions.map((x) => (
          <li key={x}>{x}</li>
        ))}
      </ul>
      <h4>Current plan</h4>
      <p>{brief.plan || "Planning is pending."}</p>
      {task.advice && task.activity !== "Waiting" && (
        <>
          <h4>Expert advice</h4>
          <ExpertAdvice advice={task.advice} />
        </>
      )}
      <h4>Recorded decisions</h4>
      {task.decisions.length ? (
        task.decisions.map((d, i) => (
          <div className="record" key={i}>
            <strong>{d.decision.text}</strong>
            <p>
              {d.actor} · {d.authority}
            </p>
            <p>{d.rationale}</p>
            <small>{d.sources.join(", ")}</small>
          </div>
        ))
      ) : (
        <p>No decision recorded yet.</p>
      )}
    </section>
  );
}

export function EvidencePanel({ id, revision, api }: { id: string; revision: number; api: Api }) {
  const [data, setData] = useState<Evidence | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  async function download() {
    setDownloading(true);
    setDownloadError("");
    try {
      const blob = await api<Blob>("tasks/" + id + "/export", { download: true });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "factory-" + id + ".zip";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setDownloadError((e as Error).message); }
    finally { setDownloading(false); }
  }
  useEffect(() => {
    const ctrl = new AbortController();
    api<Evidence>("tasks/" + id + "/evidence", { signal: ctrl.signal })
      .then((value) => {
        if (!ctrl.signal.aborted) {
          setData(value);
          setError("");
        }
      })
      .catch((e) => {
        if (!ctrl.signal.aborted) setError(e.message);
      });
    return () => ctrl.abort();
  }, [id, revision, api, retry]);
  return (
    <section className="reading">
      {error && (
        <p role="alert">
          {error} <button onClick={() => setRetry((x) => x + 1)}>Retry evidence</button>
        </p>
      )}
      {!data ? (
        <p>Loading evidence…</p>
      ) : (
        <>
          <h3>Verification evidence</h3>
          <button disabled={downloading} onClick={download}>Download task evidence</button>
          {downloadError && <p role="alert">{downloadError}</p>}
          {data.earlierVerification?.map(round => <details key={round.policy}>
            <summary>Earlier verification · {round.policy}</summary><p>{round.goal}</p>
            <ul>{round.attempts.map((a,i) => <li key={i}><code>{a.candidateHash.slice(0,12)}</code> · {a.failed.join(", ") || "Passed"}</li>)}</ul>
          </details>)}
          {!data.receipts.length && <p>No verification receipt recorded yet.</p>}
          {data.receipts.map((r, i) => (
            <details key={i}>
              <summary>
                {data.rejectedReceipt?.index === i ? "Rejected" : r.passed ? "Passed" : "Failed"} ·{" "}
                {r.candidateHash?.slice(0, 12)}
              </summary>
              {data.rejectedReceipt?.index === i && <p>{data.rejectedReceipt.reason}</p>}
              <ul>
                {r.checks?.map(
                  (c, j) =>
                    c && (
                      <li key={j}>
                        {c.passed ? "Pass" : "Fail"} · {c.assertion}
                      </li>
                    ),
                )}
              </ul>
            </details>
          ))}
          {data.ciPolicy && (
            <section aria-label="Local CI">
              <h3>Local CI</h3>
              <p>{data.ciPolicy.policy.version} · Repair limit {data.ciPolicy.policy.maxRepairs}</p>
              {data.ciBudget && <p>CI rounds used {data.ciBudget.admitted} of {data.ciBudget.limit} · {data.ciBudget.secondsPerRound}s per round</p>}
              <p>Environment <code>{data.ciPolicy.environment}</code></p>
              {data.ciExecutions?.map(({ binding, checks }) => (
                <details key={binding.executionId}>
                  <summary>{checks.every(c => c.exitCode === 0) ? "Passed" : "Failed"} · {binding.candidateHash.slice(0, 12)}</summary>
                  <p>Execution <code>{binding.executionId}</code> · {binding.revisionKind} <code>{binding.revision}</code></p>
                  <ul>{checks.map(c => <li key={c.id}>{c.id} · exit {c.exitCode}</li>)}</ul>
                </details>
              ))}
            </section>
          )}
          <h3>Gate history</h3>
          {!data.gates.length && <p>No gate evaluated yet.</p>}
          {data.gates.map((g) => (
            <details key={g.sequence}>
              <summary>
                {g.input.phase} · {g.outcome}
              </summary>
              <p>{g.reason}</p>
              <p>
                Next: {g.nextPhase} · {g.nextActivity}
              </p>
              <ul>
                {g.criteria.map((c, i) => (
                  <li key={i}>
                    {c.id} · {c.finding}
                  </li>
                ))}
              </ul>
            </details>
          ))}
          {data.review && (
            <>
              <h3>Independent review</h3>
              <p>{data.review.finding}</p>
            </>
          )}
          {data.candidate && (
            <details>
              <summary>Candidate contents</summary>
              <pre>{data.candidate}</pre>
            </details>
          )}
          <h3>Pinned sources</h3>
          {data.documents.map((d) => (
            <Document key={d.path} id={id} doc={d} api={api} />
          ))}
        </>
      )}
    </section>
  );
}

function Document({ id, doc, api }: { id: string; doc: Evidence["documents"][number]; api: Api }) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function load() {
    setBusy(true);
    setError("");
    try {
      setContent((await api<{ content: string }>("tasks/" + id + "/owner/documents/" + doc.part + "?path=" + encodeURIComponent(doc.path))).content);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <details
      onToggle={(e) => {
        if (e.currentTarget.open && content === null && !busy && !error) void load();
      }}
    >
      <summary>{doc.path}</summary>
      {content !== null && <pre>{content}</pre>}
      {busy && <p>Loading source…</p>}
      {error && (
        <p role="alert">
          {error} <button onClick={load}>Retry source</button>
        </p>
      )}
    </details>
  );
}
