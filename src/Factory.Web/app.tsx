import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { age, Api, Catalog, GitHubIntake, QueueRow, status, Task, Workspace } from "./types";
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

function Desk({ api, signOut }: { api: Api; signOut: () => void }) {
  const [rows, setRows] = useState<QueueRow[]>([]);
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
          api<QueueRow[]>("dashboard/queue", { signal: ctrl.signal }),
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
            <ul className="task-list">
              {rows
                .filter(
                  (r) =>
                    filter === "all" || r.task.activity === (filter === "blocked" ? "Blocked" : "Waiting"),
                )
                .map(({ task: t }) => (
                  <li key={t.id}>
                    <button
                      className="queue-task"
                      aria-pressed={!isNew && id === t.id}
                      onClick={() => choose(t.id)}
                    >
                      <span className="row-meta">
                        <code>{t.id.slice(0, 8)}</code>
                        <small>{age(t.openedAt)}</small>
                      </span>
                      <strong>{label(t)}</strong>
                      <span className="row-meta">
                        <span className={"status " + t.activity}>{status(t)}</span> {t.phase}
                      </span>
                      {(t.waitReason || t.lastActivity) && (
                        <span className="row-note">{t.waitReason || t.lastActivity}</span>
                      )}
                    </button>
                  </li>
                ))}
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
            <article>
              <div className="task-heading">
                <div className="heading-meta">
                  <code>TASK / {id.slice(0, 8)}</code>
                  <span className={"status " + task.activity}>{status(task)}</span>
                </div>
                <h1>{label(task)}</h1>
                <p>{task.goal}</p>
              </div>
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
                    <a href={task.repository.issueUrl}>Issue #{task.repository.number}</a> · Base{" "}
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
                      <a href={task.repository.draft.url}>View PR #{task.repository.draft.number}</a>
                      {" · "}
                      <code>{task.repository.draft.headSha.slice(0, 12)}</code>
                    </p>
                  ) : (
                    task.repository.awaitingConfirmation && (
                      <p>Awaiting GitHub confirmation of the original Draft PR request.</p>
                    )
                  )}
                </section>
              )}
              {detailError && <p role="alert">{detailError} · Reconnecting…</p>}
              {task.recovery && (
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
              <nav className="tabs" aria-label="Task views">
                {(["Conversation", "Brief", "Evidence", "Funding"] as const).map((t) => (
                  <button key={t} aria-current={tab === t ? "page" : undefined} onClick={() => setTab(t)}>
                    {t}
                  </button>
                ))}
              </nav>
              {tab === "Conversation" && (
                <Conversation key={id} task={task} api={api} ready={ready} changed={update} />
              )}
              {tab === "Brief" && <Brief task={task} />}
              {tab === "Funding" && <FundingPanel key={id} id={id} api={api} />}
              {tab === "Evidence" && (
                <EvidencePanel key={id} id={id} revision={row.stateRevision} api={api} />
              )}
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

function GitHubIssueForm({ intake, goal, api, ready, created, cancel }: StartFormProps & { intake: GitHubIntake; goal: string }) {
  const [number, select] = useState(intake.issues[0].number);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const issue = intake.issues.find(item => item.number === number)!;
  async function begin(event: React.FormEvent) {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await api<{ taskId: string }>(`github/issues/${number}/start`, {
        body: { repositoryId: intake.repositoryId, issueId: issue.id, scopeHash: issue.scopeHash },
        timeoutMs: 90000,
      });
      created(result.taskId);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="card start" onSubmit={begin}>
      <h1>Start GitHub issue</h1>
      <label>Approved issue
        <select value={number} disabled={busy} onChange={e => select(Number(e.target.value))}>
          {intake.issues.map(item => <option key={item.id} value={item.number}>{intake.repository}#{item.number}</option>)}
        </select>
      </label>
      <p>{goal}</p>
      {error && <p role="alert">{error}</p>}
      <div className="actions">
        <button disabled={!ready || busy}>{busy ? "Starting…" : "Begin"}</button>
        {cancel && <button type="button" className="secondary" disabled={busy} onClick={cancel}>Cancel</button>}
      </div>
    </form>
  );
}

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
