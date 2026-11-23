// Deterministic in-flight provider request for the supervisor's whole-lease cancellation probe.
import {createServer} from "node:http";
import {writeFileSync} from "node:fs";
import {once} from "node:events";
import {turn} from "./sdk-turn.mjs";

const server = createServer((req, res) => {
  if (req.method !== "POST" || req.url !== "/v1/responses") { res.writeHead(404).end(); return; }
  req.resume();
  writeFileSync("/work/inflight", "SDK request reached fixture");
  const heartbeat = setInterval(() => writeFileSync("/work/heartbeat", String(Date.now())), 100);
  res.on("close", () => clearInterval(heartbeat));
});
server.listen(0, "127.0.0.1");
await once(server, "listening");
try {
  await turn({prompt:"Wait for the controlled provider", fixturePort:server.address().port,
    model:"factory-test", state:"/tmp/runtime-home", workspace:"/work"});
} finally { server.closeAllConnections(); server.close(); }
