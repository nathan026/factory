import assert from "node:assert/strict";
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import { once } from "node:events";
import { fileURLToPath } from "node:url";

const marker = randomUUID();
const expected = [
  {status: "needs_input", answer: "Which boundary is approved?"},
  {status: "completed", answer: marker},
  {status: "completed", answer: "fresh"}
];
let calls = 0;
const server = createServer(async (req, res) => {
  if (req.method !== "POST" || req.url !== "/v1/responses") { res.writeHead(404).end(); return; }
  let raw = "";
  for await (const chunk of req) raw += chunk;
  const body = JSON.parse(raw);
  const i = calls++;
  if (i === expected.length) {
    res.writeHead(429, {"Content-Type":"application/json"}).end(JSON.stringify({
      error:{message:"Fixture quota exhausted", type:"insufficient_quota", code:"insufficient_quota"}
    }));
    return;
  }
  assert(i < expected.length, "Unexpected provider retry");
  assert.equal(body.text.format.type, "json_schema");
  assert.deepEqual(body.text.format.schema.required, ["status", "answer"]);
  assert.equal(JSON.stringify(body.input).includes(marker), i < 2, "Unexpected conversation context");
  assert(!req.headers.authorization, "Credential attached to fixture request");
  const item = {type: "message", role: "assistant", id: `msg_${i}`, status: "completed",
    content: [{type: "output_text", text: JSON.stringify(expected[i]), annotations: []}]};
  const response = {id: `resp_${i}`, object: "response", status: "completed", output: [item],
    usage: {input_tokens: 1, output_tokens: 1, total_tokens: 2}};
  res.writeHead(200, {"Content-Type":"text/event-stream"});
  for (const event of [
    {type:"response.created", response:{...response, status:"in_progress", output:[]}},
    {type:"response.output_item.added", output_index:0, item:{...item, status:"in_progress", content:[]}},
    {type:"response.output_item.done", output_index:0, item},
    {type:"response.completed", response}
  ]) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  res.end();
});

async function invoke(args, expectedError) {
  const child = spawn(process.execPath, [fileURLToPath(new URL("./sdk-turn.mjs", import.meta.url))],
    {stdio: ["pipe", "pipe", "pipe"], timeout: 50000});
  const stopped = once(child, "close");
  let out = "", err = "";
  child.stdout.on("data", s => out += s);
  child.stderr.on("data", s => err += s);
  child.stdin.end(JSON.stringify({state:"/tmp/runtime-home", workspace:"/tmp/work", model:"factory-test", ...args}));
  const [code] = await stopped;
  if (expectedError) {
    assert.equal(code, 1); assert.equal(err.trim(), expectedError); return;
  }
  assert.equal(code, 0, err);
  return JSON.parse(out);
}

mkdirSync("/tmp/work", {recursive:true});
server.listen(0, "127.0.0.1");
await once(server, "listening");
try {
  const fixturePort = server.address().port;
  await invoke({prompt:"Do not start an unsigned turn"}, "Verified ChatGPT sign-in required");
  const first = await invoke({fixturePort, prompt:`Remember ${marker}. Ask one boundary question.`});
  assert.deepEqual(first.output, expected[0]);
  const resumed = await invoke({fixturePort, threadId:first.threadId, prompt:"Approved. Return the original marker."});
  assert.equal(resumed.threadId, first.threadId); assert.deepEqual(resumed.output, expected[1]);
  const fresh = await invoke({fixturePort, prompt:"Start an independent review."});
  assert.notEqual(fresh.threadId, first.threadId); assert.deepEqual(fresh.output, expected[2]);
  assert.equal(calls, 3);
  await invoke({fixturePort, prompt:"Exercise the controlled quota failure."}, "Wait for provider quota");
  assert.equal(calls, 4);
  console.log(JSON.stringify({sdk:"0.171.0", provider:"loopback fixture", structuredTurns:3,
    unsignedDenied:true, resumedAcrossProcess:true, freshContext:true, fixtureQuotaStopped:true}));
} finally { server.closeAllConnections(); server.close(); }
