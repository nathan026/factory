import { WriterRecovery } from "./writer-recovery";
import { GitHubIssueForm } from "./issue-picker";
import { ReviseRound } from "./revise-round";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { IssueActions } from "./issue-actions";
import { age, Api, Catalog, QueueRow, status, Task, Workspace } from "./types";
import { Brief, Conversation, EvidencePanel, ExpertAdvice, RecoverOperation } from "./panels";

import { RestorePanel } from "./restore";
import { FundingPanel } from "./funding";

function Brand() {
  return (
    <div className="brand">
      <span className="mark" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      Factory <span className="brand-divider">/</span> <span>Task desk</span>
    </div>
  );
}

function App() {
  const [token, setToken] = useState(sessionStorage.getItem("factory-token") || "");
  const [entry, setEntry] = useState("");
  const [notice, setNotice] = useState("");
  const signOut = useCallback((reason: string) => {
    sessionStorage.removeItem("factory-token");
    setToken("");
    setNotice(reason);
  }, []);
  const api: Api = useCallback(
    async (path, options = {}) => {
      const timeout = AbortSignal.timeout(options.timeoutMs ?? 10000);
      const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
      const response = await fetch("/api/" + path, {
        method: options.body ? "POST" : "GET",
        signal,
        cache: "no-store",
        headers: {
          Authorization: "Bearer " + token,
          ...(options.body ? { "Content-Type": "application/json" } : {}),
          ...(options.minimal ? { Prefer: "return=minimal" } : {}),
        },
        body: options.body ? JSON.stringify(options.body) : undefined,
      });
      if (response.status === 401) {
        if (sessionStorage.getItem("factory-token") === token)
          signOut("Sign in with the current controller token.");
        throw new Error("Sign-in required.");
      }
      if (!response.ok) {
        const message = await response.json().catch(() => null);
        throw new Error(message?.error || "Request failed (" + response.status + "). Please retry.");
      }
      if (options.download) return response.blob();
      return response.status === 204 ? undefined : response.json();
    },
    [token, signOut],
  );
  if (token) return <Desk api={api} signOut={() => signOut("")} />;
  return (
    <>
      <header className="top">
        <Brand />
      </header>
      <div className="auth">
        <form
          className="card start"
          onSubmit={(e) => {
            e.preventDefault();
            sessionStorage.setItem("factory-token", entry);
            setToken(entry);
            setEntry("");
            setNotice("");
          }}
        >
          <h1>Sign in to the task desk</h1>
          <p>Use the local token printed by the running controller.</p>
          {notice && <p role="alert">{notice}</p>}
          <label>
            Local sign-in token
            <input
              type="password"
              value={entry}
              required
              autoComplete="off"
              onChange={(e) => setEntry(e.target.value)}
            />
          </label>
          <button>Sign in</button>
        </form>
      </div>
    </>
  );
}

type DeskQueueRow = QueueRow & { task: Task & { issueTitle?: string | null } };

// A number records when a failed background lookup can be retried.
type LegacyTitle = string | null | number;

function Desk({ api, signOut }: { api: Api; signOut: () => void }) {
  const [rows, setRows] = useState<DeskQueueRow[]>([]);
  const queueList = useRef<HTMLUListElement>(null);
  const [legacyTitles, setLegacyTitles] = useState(() => new Map<string, LegacyTitle>());
  const rememberLegacyTitle = useCallback((taskId: string, title: LegacyTitle) => {
    setLegacyTitles((previous) => {
      if (previous.get(taskId) === title) return previous;
      const next = new Map(previous);
      next.delete(taskId);
      next.set(taskId, title);
      const oldest = next.keys().next().value;
      if (next.size > 128 && oldest !== undefined) next.delete(oldest);
      return next;
    });
  }, []);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [linked, setLinked] = useState(() => new URLSearchParams(location.search).get("task"));
  const [selected, setSelected] = useState(() => linked ?? sessionStorage.getItem("factory-selected") ?? "");
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState<"all" | "decisions" | "blocked">("all");
  const [openQueue, setOpenQueue] = useState(false);
  const [connection, setConnection] = useState<"connecting" | "online" | "offline">("connecting");
  const [refresh, setRefresh] = useState(0);
  const [workspace, setWorkspace] = useState<{ task: Workspace; version: number } | null>(null);
  const [detailError, setDetailError] = useState("");
  const [tab, setTab] = useState<"Conversation" | "Brief" | "Evidence" | "Funding">("Conversation");
  const [busy, setBusy] = useState(false);
  const [commandError, setCommandError] = useState("");
  const [newId, setNewId] = useState("");
  const row = rows.find((r) => r.task.id === selected) || (linked === null ? rows[0] : undefined);
  const id = row?.task.id || "";
  const revision = row?.revision;
  useEffect(() => {
    if (!id || creating || newId || id === selected) return;
    setSelected(id);
    sessionStorage.setItem("factory-selected", id);
  }, [id, selected, creating, newId]);
  const task = id === workspace?.task.id ? workspace.task : null;
  const ready = connection === "online" && !!task && row?.revision === workspace?.version && !detailError;
  const blocked = rows.filter((r) => r.task.activity === "Blocked").length;
  const attention = rows.filter((r) => r.task.activity === "Waiting").length;
  const label = (t: Task) => catalog?.scenarios.find((s) => s.id === t.scenario)?.label || t.scenario;
  const filteredRows = rows.filter(
    ({ task }) => filter === "all" || task.activity === (filter === "blocked" ? "Blocked" : "Waiting"),
  );
  const legacyIntake = !!catalog?.githubIntake;
  const selectedLegacy = legacyIntake && row?.task.issueTitle === undefined;
  const legacyQueueKey = legacyIntake
    ? filteredRows.filter(({ task }) => task.issueTitle === undefined && task.id !== id)
        .map(({ task }) => task.id).join(",")
    : "";
  useEffect(() => {
    if (selectedLegacy && task) {
      rememberLegacyTitle(task.id, task.repository ? task.summary.issue : null);
    }
  }, [selectedLegacy, task, rememberLegacyTitle]);

  useEffect(() => {
    if (!legacyQueueKey || !queueList.current) return;
    const ctrl = new AbortController();
    const visible = new Set<string>();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let loading = false;

    // Read only rows encountered in the viewport, with one background lookup at a time.
    function loadVisible() {
      clearTimeout(timer);
      if (loading) return;
      const now = Date.now();
      let retryAt = Infinity;
      for (const taskId of visible) {
        const cached = legacyTitles.get(taskId);
        if (cached === null || typeof cached === "string") continue;
        if (typeof cached === "number" && cached > now) {
          retryAt = Math.min(retryAt, cached);
          continue;
        }
        loading = true;
        void api<Workspace>("tasks/" + taskId + "/workspace", { signal: ctrl.signal })
          .then((value) => {
            if (!ctrl.signal.aborted) {
              rememberLegacyTitle(taskId, value.repository ? value.summary.issue : null);
            }
          })
          .catch(() => {
            if (!ctrl.signal.aborted) rememberLegacyTitle(taskId, Date.now() + 15000);
          });
        return;
      }
      if (Number.isFinite(retryAt)) timer = setTimeout(loadVisible, retryAt - now);
    }

    const observer = new IntersectionObserver((entries) => {
      if (ctrl.signal.aborted) return;
      for (const entry of entries) {
        const taskId = entry.target.getAttribute("data-legacy-task")!;
        if (entry.isIntersecting) visible.add(taskId);
        else visible.delete(taskId);
      }
      loadVisible();
    });
    queueList.current.querySelectorAll("[data-legacy-task]").forEach((item) => observer.observe(item));
    return () => {
      observer.disconnect();
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [api, legacyQueueKey, legacyTitles, rememberLegacyTitle]);

  function queueTitle(t: DeskQueueRow["task"]) {
    if (t.issueTitle !== undefined || !legacyIntake) return t.issueTitle ?? label(t);
    if (task?.id === t.id) return task.repository ? task.summary.issue : label(t);
    const cached = legacyTitles.get(t.id);
    return typeof cached === "string" ? cached : label(t);
  }
  const update = () => setRefresh((x) => x + 1);
  function clearLink() {
    if (linked !== null) {
      const url = new URL(location.href);
      url.searchParams.delete("task");
      history.replaceState(null, "", url);
      setLinked(null);
    }
  }
  function choose(id: string) {
    clearLink();
    setSelected(id);
    sessionStorage.setItem("factory-selected", id);
    setCreating(false);
    setTab("Conversation");
    setCommandError("");
    setOpenQueue(false);
    setNewId("");
  }
  useEffect(() => {
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const [list, options] = await Promise.all([
          api<DeskQueueRow[]>("dashboard/queue", { signal: ctrl.signal }),
          catalog ? Promise.resolve(catalog) : api<Catalog>("dashboard/scenarios", { signal: ctrl.signal }),
        ]);
        if (ctrl.signal.aborted) return;
        const pendingCreate = sessionStorage.getItem("factory-create-id");
        if (linked === null && pendingCreate && list.some(({ task }) => task.id === pendingCreate)) {
          sessionStorage.removeItem("factory-create-id");
          sessionStorage.removeItem("factory-opening");
          choose(pendingCreate);
          setFilter("all");
        }
        setRows(list);
        if (!catalog) setCatalog(options);
        setConnection("online");
      } catch {
        if (!ctrl.signal.aborted) setConnection("offline");
      } finally {
        if (!ctrl.signal.aborted) timer = setTimeout(poll, 1000);
      }
    }
    void poll();
    return () => {
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [api, catalog, refresh, linked]);
  useEffect(() => {
    if (!id || revision === undefined || creating || (newId && id !== newId)) return;
    const version = revision;
    const ctrl = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const value = await api<Workspace>("tasks/" + id + "/workspace", { signal: ctrl.signal });
        if (!ctrl.signal.aborted) {
          setWorkspace({ task: value, version });
          setDetailError("");
        }
      } catch (e) {
        if (!ctrl.signal.aborted) {
          setDetailError((e as Error).message);
          timer = setTimeout(load, 1500);
        }
      }
    }
    void load();
    return () => {
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [api, id, revision, creating, newId, refresh]);
  async function decide(value: string) {
    if (!task || !ready || busy) return;
    setBusy(true);
    setCommandError("");
    try {
      await api("tasks/" + id + "/decision", {
        body: { value, questionKey: task.questionKey },
        minimal: true,
      });
      update();
    } catch (e) {
      setCommandError((e as Error).message);
      update();
    } finally {
      setBusy(false);
    }
  }
  const empty = connection === "online" && !rows.length;
  const isNew = creating || (empty && !newId && linked === null);
  return (
    <>
      <header className="top">
        <Brand />
        <div className="tools">
          <span className={"connection " + connection}>
            <i />
            {connection === "online"
              ? "Connected"
              : connection === "offline"
                ? "Reconnecting…"
                : "Connecting…"}
          </span>
          <button className="link-btn" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>
      {connection === "offline" && (
        <p role="status" className="banner">
          Controller unavailable. Reconnecting automatically.
        </p>
      )}
      {catalog?.restorePaused && <RestorePanel api={api} />}
      <div className="desk">
        <aside className={openQueue ? "queue expanded" : "queue"}>
          <div className="queue-head">
            <h2>Task queue</h2>
            <button
              className="mobile-toggle secondary"
              aria-expanded={openQueue}
              onClick={() => setOpenQueue(!openQueue)}
            >
              {openQueue ? "Hide queue" : "Show queue"} ({rows.length})
            </button>
          </div>
          <div className="queue-body">
            <button
              className="new-task"
              onClick={() => {
                clearLink();
                setCreating(true);
                setOpenQueue(false);
                setCommandError("");
              }}
            >
              {catalog?.githubIntake ? "Start GitHub issue" : "+ New task"}
            </button>
            <div className="filters" aria-label="Queue filter">
              <button aria-pressed={filter === "all"} onClick={() => setFilter("all")}>
                All tasks <b>{rows.length}</b>
              </button>
              <button aria-pressed={filter === "decisions"} onClick={() => setFilter("decisions")}>
                Decisions <b>{attention}</b>
              </button>
              <button aria-pressed={filter === "blocked"} onClick={() => setFilter("blocked")}>
                Blocked <b>{blocked}</b>
              </button>
            </div>
            <ul className="task-list" ref={queueList}>
              {filteredRows.map(({ task: t }) => {
                const title = queueTitle(t);
                return (
                    <li
                      key={t.id}
                      data-legacy-task={legacyIntake && t.issueTitle === undefined && t.id !== id ? t.id : undefined}
                    >
                      <button
                        className="queue-task"
                        aria-pressed={!isNew && id === t.id}
                        onClick={() => choose(t.id)}
                      >
                        <span className="row-meta">
                          <code>{t.id.slice(0, 8)}</code>
                          <small>{age(t.openedAt)}</small>
                        </span>
                        <strong title={title}>{title}</strong>
                        <span className="row-meta">
                          <span className={"status " + t.activity}>{status(t)}</span> {t.phase}
                        </span>
                        {(t.waitReason || t.lastActivity) && (
                          <span className="row-note">{t.waitReason || t.lastActivity}</span>
                        )}
                      </button>
                    </li>
                );
              })}
            </ul>
            {filter === "blocked" && !blocked && <p className="muted">No blocked tasks.</p>}
            {filter === "decisions" && !attention && <p className="muted">No decisions waiting.</p>}
          </div>
        </aside>
        <div className="workspace">
          {isNew && catalog ? (
            <NewTask
              catalog={catalog}
              ready={connection === "online" && !catalog.restorePaused}
              api={api}
              cancel={rows.length ? () => setCreating(false) : undefined}
              created={(id) => {
                choose(id);
                setNewId(id);
                setFilter("all");
                update();
              }}
            />
          ) : linked !== null && connection === "online" && !row ? (
            <p role="status">This linked task is unavailable on this controller.</p>
          ) : !task || !row || (newId && id !== newId) ? (
            <p role="status">
              {detailError || (connection === "offline" ? "Waiting for controller…" : "Loading task desk…")}
            </p>
          ) : (
            <article className="open-task">
              <div className="task-heading">
                <div className="heading-meta">
                  <code>TASK / {id.slice(0, 8)}</code>
                  <span className={"status " + task.activity}>{status(task)}</span>
                </div>
                <h1>{task.repository ? task.summary.issue : label(task)}</h1>
              </div>
              <div className="task-main">
                <nav className="tabs" aria-label="Task views">
                  {(["Conversation", "Brief", "Evidence", "Funding"] as const).map((t) => (
                    <button key={t} aria-current={tab === t ? "page" : undefined} onClick={() => setTab(t)}>
                      {t}
                    </button>
                  ))}
                </nav>
                <div className="task-view">
                  {tab === "Conversation" && (
                    <Conversation key={id} task={task} api={api} ready={ready} changed={update} />
                  )}
                  {tab === "Brief" && <Brief task={task} />}
                  {tab === "Funding" && <FundingPanel key={id} id={id} api={api} />}
                  {tab === "Evidence" && (
                    <EvidencePanel key={id} id={id} revision={row.stateRevision} api={api} />
                  )}
                </div>
              </div>
              <aside className="task-details" aria-label="Task details">
                {task.summary.request && <p>{task.summary.request}</p>}
                {task.goal !== (task.repository ? task.summary.issue : label(task)) && <p>{task.goal}</p>}
                <section className="reading" aria-label="Task summary">
                  <h3>Work done</h3>
                  <ul>{task.summary.work.map((line, i) => <li key={i}>{line}</li>)}</ul>
                  <h3>Checks and review</h3>
                  <ul>{task.summary.checks.map((line, i) => <li key={i}>{line}</li>)}</ul>
                </section>
                <dl className="facts">
                  <div>
                    <dt>Owner</dt>
                    <dd>
                      Lifecycle owner <code>{task.ownerId.slice(0, 8)}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>Phase</dt>
                    <dd>{task.phase}</dd>
                  </div>
                  <div>
                    <dt>Active worker</dt>
                    <dd>
                      {task.run
                        ? {
                            Planning: "Planner",
                            Implementation: "Implementer",
                            Verification: "Verifier",
                            Review: "Reviewer",
                            Delivery: "Delivery worker",
                          }[task.run.phase]
                        : "None"}
                    </dd>
                  </div>
                </dl>
                <div className="next">
                  <strong>Next action</strong>
                  <span>{task.nextAction}</span>
                </div>
                {task.repository && (
                  <section className="reading" aria-label="Repository delivery">
                    <h3>Repository delivery</h3>
                    <p>
                      {task.repository.repository} ·{" "}
                      <a href={task.repository.issueUrl} target="_blank" rel="noopener noreferrer">Issue #{task.repository.number}</a> · Base{" "}
                      <code>{task.repository.baseSha.slice(0, 12)}</code>
                    </p>
                    {task.repository.eligibility !== "Eligible" && <p>{task.repository.eligibility}</p>}
                    {task.repository.problem && <p role="status">{task.repository.problem}</p>}
                    {task.repository.requiresReconciliation && (
                      <button disabled={!ready} onClick={async () => {
                        try {
                          await api("github/issues/" + task.repository!.number + "/sync", {body:{}});
                          update();
                        } catch (error) { setCommandError((error as Error).message); }
                      }}>Reconcile original publication</button>
                    )}
                    {task.repository.draft ? (
                      <p>
                        <a href={task.repository.draft.url} target="_blank" rel="noopener noreferrer">View PR #{task.repository.draft.number}</a>
                        {" · "}
                        <code>{task.repository.draft.headSha.slice(0, 12)}</code>
                      </p>
                    ) : (
                      task.repository.awaitingConfirmation && (
                        <p>Awaiting GitHub confirmation of the original Draft PR request.</p>
                      )
                    )}
                    {task.revisionAvailable && <ReviseRound key={task.id} task={task} ready={ready} api={api} update={update} />}
                    {!!task.previousDrafts?.length && <details><summary>Earlier deliveries</summary><ul>
                      {task.previousDrafts.map(pr=><li key={pr.number}><a href={pr.url} target="_blank" rel="noopener noreferrer">PR #{pr.number}</a> · <code>{pr.headSha.slice(0,12)}</code></li>)}
                    </ul></details>}
                    {catalog?.issueActions && task.repository.draft && task.activity === "Delivered" &&
                      <IssueActions key={task.id + task.repository.draft.headSha} task={task} api={api} ready={ready} update={update} />}
                  </section>
                )}
                {detailError && <p role="alert">{detailError} · Reconnecting…</p>}
                {task.writerRestart && <WriterRecovery key={task.recovery?.operationId} task={task} api={api} ready={ready} update={update} />}
                {task.recovery && !task.writerRestart && (
                  <RecoverOperation
                    key={task.recovery.operationId}
                    id={id}
                    recovery={task.recovery}
                    api={api}
                    ready={ready}
                    changed={update}
                  />
                )}
                {task.activity === "Waiting" && task.advice && (
                  <section className="decision" aria-label="Pending decision">
                    <div className="eyeline">Decision needed</div>
                    <h2>{task.question?.text}</h2>
                    <ExpertAdvice advice={task.advice} />
                    <div className="actions">
                      {task.choices.map((c, i) => (
                        <button
                          disabled={!ready || busy}
                          key={c.value}
                          className={i === 0 ? "" : "secondary"}
                          onClick={() => decide(c.value)}
                        >
                          {c.label}
                        </button>
                      ))}
                    </div>
                  </section>
                )}
                {task.activity === "Waiting" && commandError && <p role="alert">{commandError}</p>}
              </aside>
            </article>
          )}
        </div>
      </div>
    </>
  );
}

type StartFormProps = {
  api: Api;
  ready: boolean;
  created: (id: string) => void;
  cancel?: () => void;
};

function NewTask(props: StartFormProps & { catalog: Catalog }) {
  const { catalog } = props;
  return catalog.githubIntake
    ? <GitHubIssueForm {...props} intake={catalog.githubIntake} goal={catalog.goal} />
    : <SampleTaskForm {...props} />;
}

function SampleTaskForm({
  catalog,
  api,
  ready,
  created,
  cancel,
}: StartFormProps & { catalog: Catalog }) {
  const [scenario, setScenario] = useState(
    () => sessionStorage.getItem("factory-scenario") || catalog.scenarios[0].id,
  );
  const [text, setText] = useState(() => sessionStorage.getItem("factory-opening") || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function start(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !ready) return;
    const id = sessionStorage.getItem("factory-create-id") || crypto.randomUUID();
    sessionStorage.setItem("factory-create-id", id);
    setBusy(true);
    setError("");
    try {
      await api("tasks", { body: { id, scenario, message: text.trim() || null }, minimal: true });
      if (sessionStorage.getItem("factory-create-id") === id) {
        sessionStorage.removeItem("factory-create-id");
        sessionStorage.removeItem("factory-opening");
        created(id);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="card start" onSubmit={start}>
      <div className="eyeline">Controlled project</div>
      <h1>Start a task</h1>
      <p>{catalog.goal}</p>
      <label>
        Sample task
        <select
          value={scenario}
          disabled={busy}
          onChange={(e) => {
            setScenario(e.target.value);
            sessionStorage.setItem("factory-scenario", e.target.value);
            sessionStorage.removeItem("factory-create-id");
          }}
        >
          {catalog.scenarios.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Opening message <small>(optional)</small>
        <textarea
          value={text}
          maxLength={4000}
          disabled={busy}
          rows={4}
          placeholder="What would you like the owner to know?"
          onChange={(e) => {
            setText(e.target.value);
            sessionStorage.setItem("factory-opening", e.target.value);
            sessionStorage.removeItem("factory-create-id");
          }}
        />
      </label>
      <small>The task runs the selected brief with deterministic workers.</small>
      {error && <p role="alert">{error}</p>}
      <div className="actions">
        <button disabled={!ready || busy}>{busy ? "Starting…" : "Start task"}</button>
        {cancel && (
          <button type="button" className="secondary" disabled={busy} onClick={cancel}>
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
