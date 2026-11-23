// Run only in the trusted runtime on the approved qualification VM.
import assert from "node:assert/strict";
import {randomUUID} from "node:crypto";
import {lstatSync, mkdtempSync, readFileSync, renameSync, rmdirSync, writeFileSync} from "node:fs";
import {dirname, isAbsolute, join} from "node:path";
import {requireSubscription, turn} from "./sdk-turn.mjs";

async function main() {
  const [phase, state, checkpoint, model] = process.argv.slice(2);
  assert(process.platform === "linux" && ["auth","begin","resume"].includes(phase),
    "Expected auth, begin or resume on the approved Linux host");
  assert(state && isAbsolute(state), "Supply the private qualification Codex state directory");
  const dir = lstatSync(state);
  assert(dir.isDirectory() && dir.uid === process.getuid() && !(dir.mode & 0o077), "A private owned directory is required");
  requireSubscription(state);
  if (phase === "auth") { console.log("ChatGPT sign-in verified by pinned CLI"); return; }
  assert(checkpoint && isAbsolute(checkpoint) && model, "Supply an absolute checkpoint path and supported model ID");
  const parent = lstatSync(dirname(checkpoint));
  assert(parent.isDirectory() && parent.uid === process.getuid() && !(parent.mode & 0o077), "Private checkpoint directory required");
  const workspace = mkdtempSync("/tmp/factory-m0-live-");
  try {
    if (phase === "begin") {
      const marker = randomUUID();
      writeFileSync(checkpoint, JSON.stringify({stage:"starting", model}), {flag:"wx", mode:0o600});
      const first = await turn({state, workspace, model, prompt:
        `Remember this opaque qualification marker: ${marker}. Return status needs_input and ask which boundary is approved in answer. Use no tools.`});
      assert.equal(first.output.status, "needs_input");
      assert(first.output.answer.trim().length > 0);
      const next = checkpoint + "." + randomUUID();
      writeFileSync(next, JSON.stringify({stage:"waiting", model, marker, threadId:first.threadId}), {flag:"wx", mode:0o600});
      renameSync(next, checkpoint);
      console.log("Structured question received. Checkpoint ready for runtime restart.");
    } else {
      const saved = JSON.parse(readFileSync(checkpoint, "utf8"));
      assert(saved.stage === "waiting" && saved.model === model && saved.threadId && saved.marker,
        "Expected a waiting checkpoint with matching model");
      // Reserve once before an external call. An interrupted or ambiguous attempt needs reconciliation.
      const attempt = checkpoint + ".resume";
      writeFileSync(attempt, JSON.stringify({stage:"resuming"}), {flag:"wx", mode:0o600});
      const resumed = await turn({state, workspace, model, threadId:saved.threadId, prompt:
        "The inclusive boundary is approved. Return status completed and the original opaque marker alone in answer. Use no tools."});
      assert(resumed.threadId === saved.threadId && resumed.output.status === "completed" && resumed.output.answer === saved.marker);
      const fresh = await turn({state, workspace, model, prompt:
        "Return status completed. If a previous user turn gave you an opaque qualification marker, return it alone in answer. Otherwise answer fresh. Use no tools."});
      assert(fresh.threadId !== saved.threadId && fresh.output.status === "completed" && fresh.output.answer === "fresh");
      writeFileSync(attempt, JSON.stringify({stage:"passed", funding:"ChatGPT", resumeVerified:true, freshThread:true}));
      console.log("Subscription-backed restart, continuation and fresh thread verified.");
    }
  } finally { rmdirSync(workspace); }
}

try { await main(); } catch (err) {
  const safe = ["Verified ChatGPT sign-in required", "Clean qualification configuration required", "Wait for provider quota"];
  console.error(safe.includes(err.message) ? err.message : "Live qualification stopped. Inspect the local checkpoint before retrying.");
  process.exitCode = 1;
}
