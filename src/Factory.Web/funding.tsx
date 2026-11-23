import React, { useCallback, useEffect, useState } from "react";
import { Api } from "./types";

type Money = { usdMicros: number; asOf: string; source: string };
type Usage = { heldMicros: number; estimatedMicros: number };
type Attempt = {
  attempt: number; state: string; reason: string; reservedMicros: number; estimatedMicros: number; updatedUtc: string;
  receipt: { answer: string; inputTokens: number; outputTokens: number } | null;
};
type Report = {
  paidApiEnabled: boolean; controlledWorkerClaims: number; subscriptionWorkerClaims?: number; ciClaims: number;
  subscription: { auth: string; observedUtc: string; windows?: { minutes: number; usedPercent: number; resetsAt?: number }[] } | null;
  manualApiBalance: Money | null; infrastructureCost: Money | null;
  fixture: { account: string; model: string; authorized: boolean; accountLimitMicros: number; runLimitMicros: number;
    taskLimitMicros: number; taskUse: Usage; accountUse: Usage; wait: { kind: string; retryAt: number | null } | null } | null;
  history: { id: string; question: string; account: string; latest: Attempt }[];
};
const usd = (micros: number) => new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumFractionDigits: 6,
}).format(micros / 1000000);
const date = (value: string) => new Date(value).toLocaleString();
function observation(money: Money | null) {
  return money ? `${usd(money.usdMicros)} · ${date(money.asOf)} · ${money.source}` : "Unreported";
}

export function FundingPanel({ id, api }: { id: string; api: Api }) {
  const path = "tasks/" + id + "/funding";
  const key = "factory-advice-" + id;
  const [question, setQuestion] = useState(sessionStorage.getItem(key + "-question") || "");
  const [data, setData] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    setData(await api<Report>(path, { signal }));
    setError("");
  }, [api, path]);
  useEffect(() => {
    const ctrl = new AbortController();
    refresh(ctrl.signal).catch(e => { if (!ctrl.signal.aborted) setError(e.message); });
    return () => ctrl.abort();
  }, [refresh]);

  async function act(route: string, body: object, clear = false) {
    setBusy(true);
    try {
      await api(path + route, { body });
      await refresh();
      if (clear) {
        sessionStorage.removeItem(key);
        sessionStorage.removeItem(key + "-question");
        setQuestion("");
      }
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  function ask(e: React.FormEvent) {
    e.preventDefault();
    if (!question.trim() || busy) return;
    const saved = sessionStorage.getItem(key);
    const requestId = saved && sessionStorage.getItem(key + "-question") === question ? saved : crypto.randomUUID();
    sessionStorage.setItem(key, requestId);
    sessionStorage.setItem(key + "-question", question);
    void act("/advice", { id: requestId, question: question.trim() }, true);
  }
  const fixture = data?.fixture;
  const latest = new Map(data?.history.map(h => [h.id, h.latest.attempt]));
  return <section className="reading" aria-label="Task funding">
    <h3>Usage and funding</h3>
    <button disabled={busy} onClick={() => refresh().catch(e => setError(e.message))}>Refresh usage</button>
    {error && <p role="alert">{error}</p>}
    {data && <>
      <p>Controlled worker claims {data.controlledWorkerClaims} · Subscription worker claims {data.subscriptionWorkerClaims ?? "unreported"} · CI claims {data.ciClaims}</p>
      <h4>Subscription</h4>
      <p>{data.subscription ? `${data.subscription.auth} observed ${date(data.subscription.observedUtc)}` : "Allowance unreported"}</p>
      {data.subscription?.windows?.map((w, i) => <p key={i}>{w.minutes}-minute window · {w.usedPercent}% used
        {w.resetsAt ? " · Reset " + new Date(w.resetsAt * 1000).toLocaleString() : ""}</p>)}
      <h4>Direct API</h4>
      <p>Paid calls disabled · Last confirmed manual balance {observation(data.manualApiBalance)}</p>
      <h4>Infrastructure</h4><p>Observed cost {observation(data.infrastructureCost)}</p>
      {fixture && <>
        <h4>Loopback owner advice</h4>
        <p>{fixture.account} · {fixture.model} · Simulated USD</p>
        <p>Account limit {usd(fixture.accountLimitMicros)} · Reserved {usd(fixture.accountUse.heldMicros)} · Estimated usage {usd(fixture.accountUse.estimatedMicros)}</p>
        <p>Task limit {usd(fixture.taskLimitMicros)} · Reserved {usd(fixture.taskUse.heldMicros)} · Estimated usage {usd(fixture.taskUse.estimatedMicros)} · Run limit {usd(fixture.runLimitMicros)}</p>
        {fixture.wait && <p role="status">{fixture.wait.kind} wait{fixture.wait.retryAt ? " · Recheck after " + new Date(fixture.wait.retryAt * 1000).toLocaleString() : ""}</p>}
        {fixture.authorized && <form onSubmit={ask}>
          <label>Question for the fixture owner<textarea value={question} maxLength={2000} onChange={e => setQuestion(e.target.value)} /></label>
          <button disabled={busy || !question.trim()}>Request fixture advice</button>
        </form>}
      </>}
      {data.history.map(h => <article key={h.id + "/" + h.latest.attempt}>
        <h4>{h.question}</h4>
        <p>{h.account} · Attempt {h.latest.attempt} · {h.latest.state} · {date(h.latest.updatedUtc)}</p>
        <p>{h.latest.reason}</p>
        {h.latest.receipt && <><p>{h.latest.receipt.answer}</p><p>Input {h.latest.receipt.inputTokens} · Output {h.latest.receipt.outputTokens} tokens · Estimated {usd(h.latest.estimatedMicros)} simulated</p></>}
        {fixture && latest.get(h.id) === h.latest.attempt && (["InFlight", "Unconfirmed", "CancelPending"].includes(h.latest.state) ?
          <button disabled={busy} onClick={() => act("/advice/" + h.id + "/reconcile", { attempt: h.latest.attempt })}>Find original receipt</button> :
          ["Budget", "Quota", "Authentication"].includes(h.latest.state) &&
          <button disabled={busy} onClick={() => act("/advice/" + h.id + "/resume", { attempt: h.latest.attempt })}>Check and resume</button>)}
        {latest.get(h.id) === h.latest.attempt && ["InFlight", "Unconfirmed", "CancelPending", "Budget", "Quota", "Authentication"].includes(h.latest.state) &&
          <button disabled={busy} onClick={() => act("/advice/" + h.id + "/cancel", { attempt: h.latest.attempt })}>{h.latest.state === "CancelPending" ? "Retry cancellation" : "Cancel advice request"}</button>}
      </article>)}
    </>}
  </section>;
}
