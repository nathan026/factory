import { Codex } from "@openai/codex-sdk";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const binary = fileURLToPath(new URL("./node_modules/.bin/codex", import.meta.url));
const schema = {
  type: "object", additionalProperties: false, required: ["status", "answer"],
  properties: {status: {enum: ["needs_input", "completed"]}, answer: {type: "string"}}
};

function runtimeEnv(state) {
  return {PATH: "/usr/local/bin:/usr/bin:/bin", HOME: "/tmp", CODEX_HOME: state, TMPDIR: "/tmp"};
}

export function requireSubscription(state) {
  if (existsSync(path.join(state, "config.toml"))) throw new Error("Clean qualification configuration required");
  const status = spawnSync(binary, ["-c", 'forced_login_method="chatgpt"', "login", "status"],
    {env:runtimeEnv(state), encoding:"utf8", timeout:10000});
  if (status.status !== 0 || !/^logged in using chatgpt$/im.test((status.stdout + status.stderr).trim()))
    throw new Error("Verified ChatGPT sign-in required");
}

// Each invocation is a new SDK client process. The caller supplies a dedicated session store.
export async function turn({prompt, threadId, fixturePort, model, state, workspace}) {
  const env = runtimeEnv(state);
  mkdirSync(state, {recursive: true, mode: 0o700});
  const config = {
    analytics: {enabled: false}, feedback: {enabled: false},
    features: {shell_tool: false}, shell_environment_policy: {inherit: "none"}
  };
  if (fixturePort) {
    config.model_provider = "factory_fixture";
    config.model_providers = {factory_fixture: {
      name: "Local qualification fixture", base_url: `http://127.0.0.1:${fixturePort}/v1`,
      wire_api: "responses", request_max_retries: 0, stream_max_retries: 0
    }};
  } else {
    requireSubscription(state);
    config.forced_login_method = "chatgpt";
    config.model_provider = "openai";
  }
  const client = new Codex({env, config});
  const options = {workingDirectory: workspace, model, sandboxMode: "read-only", approvalPolicy: "never",
    skipGitRepoCheck: true, webSearchMode: "disabled", networkAccessEnabled: false};
  const thread = threadId ? client.resumeThread(threadId, options) : client.startThread(options);
  const {events} = await thread.runStreamed(prompt, {outputSchema: schema, signal: AbortSignal.timeout(45000)});
  let completed = false, output, started = false;
  for await (const event of events) {
    if (event.type === "turn.started") started = true;
    if (event.type === "turn.failed" || event.type === "error") {
      const reason = event.error?.message ?? event.message ?? "";
      if (/\b429\b|quota|rate.limit|usage.limit/i.test(reason)) throw new Error("Wait for provider quota");
      throw new Error("Provider turn failed");
    }
    if (event.type === "turn.completed") completed = true;
    if (event.type === "item.completed" && event.item.type === "agent_message") output = JSON.parse(event.item.text);
  }
  if (!thread.id || !started || !completed || !output || !["completed","needs_input"].includes(output.status)
      || typeof output.answer !== "string" || Object.keys(output).length !== 2) throw new Error("Incomplete structured turn");
  return {threadId: thread.id, output};
}

// Qualification driver invokes this entry point over a private pipe, never a network endpoint.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    let input = "";
    for await (const chunk of process.stdin) input += chunk;
    console.log(JSON.stringify(await turn(JSON.parse(input))));
  } catch (e) {
    // Runtime errors may include provider responses. Keep live diagnostic output bounded and credential-free.
    const allowed = ["Verified ChatGPT sign-in required", "Wait for provider quota", "Clean qualification configuration required"];
    console.error(allowed.includes(e.message) ? e.message : "SDK turn did not complete");
    process.exitCode = 1;
  }
}
