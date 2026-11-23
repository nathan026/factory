import { randomUUID } from "node:crypto";
import { mkdtempSync, rmdirSync, rmSync, writeFileSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { Hatchet, IdempotencyCollisionError, type JsonObject } from "@hatchet-dev/typescript-sdk";

type Ticket = { id: string | null, schedulerId: string | null };
type Delivery = { token: string, output: JsonObject };
type Claim = { token: string, order: JsonObject, workerMode: "codex-subscription" | "controlled" };

const hatchet = Hatchet.init();
const api = process.env.FACTORY_CONTROLLER_URL ?? "http://127.0.0.1:5187";
const token = process.env.FACTORY_DISPATCH_TOKEN;
if (!token) throw new Error("FACTORY_DISPATCH_TOKEN is required.");

async function post<T>(path: string, data: unknown = {}): Promise<T> {
    const response = await fetch(new URL("/internal/dispatch/" + path, api), {
        method: "POST", headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify(data), signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error("Controller returned HTTP " + response.status + " for " + path);
    return await response.json() as T;
}

const release = process.env.FACTORY_CONTROLLER_RELEASE;
if (release !== undefined && !isAbsolute(release)) throw new Error("Controller release directory must be absolute.");
const dll = fileURLToPath(new URL("../Factory.Api/bin/Debug/net10.0/Factory.Api.dll", import.meta.url));
const fixture = release ? join(release, process.platform === "win32" ? "Factory.Api.exe" : "Factory.Api") : "dotnet";
const fixtureArgs = release ? ["--fixture-worker"] : [dll, "--fixture-worker"];
const python = process.platform === "linux" ? "python3" : "python";

function childJson(binary: string, args: string[], input: unknown, env: NodeJS.ProcessEnv, timeout = 30000): Promise<Delivery["output"]> {
    return new Promise((resolve, reject) => {
        const child = execFile(binary, args, { env, windowsHide: true, timeout, maxBuffer: 1024 * 1024 },
            (error, stdout) => {
                if (error) return reject(new Error("Controlled worker or environment provider failed."));
                try { resolve(JSON.parse(stdout)); } catch (err) { reject(err); }
            });
        child.stdin!.on("error", reject);
        child.stdin!.end(JSON.stringify(input));
    });
}

const executionSeconds = 60; // The installed Hatchet execution limit, shared with the native supervisor.

async function execute({order, workerMode}: Claim, signal: AbortSignal, deadline: number, sessionId: string): Promise<JsonObject> {
    // The deterministic proposal and review metadata use the existing fixture contracts.
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
        ["path", "systemroot", "temp", "tmp", "dotnet_root"].includes(key.toLowerCase())));
    if (workerMode !== "codex-subscription" && workerMode !== "controlled")
        throw new Error("Missing or unsupported controller worker mode.");
    if (workerMode === "controlled" && order.action !== "Implement" && order.action !== "Review")
        return { output: await childJson(fixture, fixtureArgs, order, env) };
    const script = fileURLToPath(new URL("../../scripts/leased_fixture.py", import.meta.url));
    // The trusted adapter talks to the local engine. Container processes receive only their admitted packet.
    const providerEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
        ["path", "systemroot", "temp", "tmp", "userprofile", "home", "localappdata", "appdata",
         "docker_context", "docker_host", "docker_config", "factory_lease_db", "factory_max_leases",
         "factory_controller_release"].includes(key.toLowerCase())));
    if (workerMode === "codex-subscription") {
        if (!providerEnv.FACTORY_LEASE_DB) throw new Error("Live execution requires the shared lease journal.");
        const directory = mkdtempSync(join(tmpdir(), "factory-dispatch-"));
        const alive = join(directory, "alive");
        const cancelled = join(directory, "cancelled");
        writeFileSync(alive, "");
        const touch = setInterval(() => { const now = new Date(); utimesSync(alive, now, now); }, 1000);
        const cancel = () => writeFileSync(cancelled, "");
        signal.addEventListener("abort", cancel, {once:true});
        try {
            signal.throwIfAborted();
            const live = fileURLToPath(new URL("../../scripts/codex_worker.py", import.meta.url));
            const authEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
                ["codex_home","xdg_runtime_dir","windir","comspec","pathext","systemdrive","homedrive","homepath","programdata","programfiles","programfiles(x86)"].includes(key.toLowerCase())));
            const reply = await childJson(python, [live, cancelled, String(deadline), sessionId], order, {...providerEnv,...authEnv, FACTORY_OPERATION_TIMING:process.env.FACTORY_OPERATION_TIMING}, 90000);
            signal.throwIfAborted();
            return reply;
        } finally {
            clearInterval(touch);
            signal.removeEventListener("abort", cancel);
            rmSync(alive,{force:true});
            rmSync(cancelled,{force:true});
            rmdirSync(directory);
        }
    }
    const result = await childJson(fixture, fixtureArgs, order, env);
    return { output: await childJson(python, [script], {order, result}, providerEnv, 90000) };
}

const task = hatchet.task({
    name: "factory-controlled-operation-v1", retries: 0, executionTimeout: `${executionSeconds}s`,
    idempotency: { strategy: "ttl", expression: "input.id", ttlMs: 86400000 },
    fn: async (input: { id: string }, ctx) => {
        const deadline = Date.now() + executionSeconds * 1000;
        const sessionId = randomUUID();
        const { claim } = await post<{ claim: Claim | null }>(input.id + "/claim", { runId: ctx.workflowRunId(), sessionId });
        if (!claim) throw new Error("Operation has no available execution claim.");
        const started = Date.now();
        try {
            return { token: claim.token, ...await execute(claim, ctx.abortController.signal, deadline, sessionId) };
        } finally {
            if (process.env.FACTORY_OPERATION_TIMING === "1")
                console.log(JSON.stringify({operation:input.id, started, ended:Date.now()}));
        }
    },
});

async function submit(id: string): Promise<string> {
    try {
        return await (await task.runNoWait({ id })).getWorkflowRunId();
    } catch (err) {
        if (err instanceof IdempotencyCollisionError)
            return err.existingRunExternalId;
        throw err;
    }
}

async function dispatch() {
    const { id, schedulerId } = await post<Ticket>("next");
    if (!id) return;
    if (!schedulerId) {
        await post(id + "/submitted", { runId: await submit(id) });
        return;
    }
    const details = await hatchet.runs.get(schedulerId);
    if (details.run.status === "COMPLETED" && details.tasks[0]?.output) {
        const delivery = details.tasks[0].output;
        if ("wait" in delivery && "token" in delivery && !("output" in delivery))
            await post(id + "/wait", { token: delivery.token, kind: delivery.wait, fundingMode: "fundingMode" in delivery ? delivery.fundingMode : "Controlled" });
        else
            await post(id + "/result", delivery);
    } else if (["FAILED", "CANCELLED", "COMPLETED"].includes(details.run.status)) {
        await post(id + "/failed", { runId: schedulerId });
    } else {
        return;
    }
    if (process.env.FACTORY_OPERATION_TIMING === "1") {
        const task = details.tasks[0];
        console.log(JSON.stringify({dispatch:id, schedulerId, terminal:details.run.status,
            taskStatus:task?.status, startedAt:task?.startedAt, endedAt:task?.finishedAt,
            controllerAcknowledgedAt:Date.now()}));
    }
}

const mode = process.argv[2];
if (mode === "register" || mode === "worker") {
    const worker = await hatchet.worker("factory-controlled-worker", { workflows: [task], slots: 1 });
    if (mode === "worker") await worker.start();
    else process.exit(0);
} else if (mode === "dispatch") {
    for (;;) {
        try { await dispatch(); }
        catch (err) { console.error(err instanceof Error ? err.message : "Dispatch reconciliation failed."); }
        await delay(350);
    }
} else {
    throw new Error("Choose register, worker or dispatch.");
}
