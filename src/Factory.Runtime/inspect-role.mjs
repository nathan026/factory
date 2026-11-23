import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import net from "node:net";

const [own, other, mode] = process.argv.slice(2);
assert.equal(process.getuid(), 1000);
assert.equal(fs.readFileSync("/work/" + own, "utf8"), own);
assert(!fs.existsSync("/work/" + other));
assert(!fs.existsSync("/var/run/docker.sock"));
assert(!fs.existsSync("/root/.ssh"));
for (const path of ["/tmp/runtime-home/auth-canary", "/tmp/control-canary"]) {
  assert.throws(() => fs.readFileSync(path), {code:"ENOENT"});
}
assert(!Object.keys(process.env).some(k => /^(OPENAI_API_KEY|CODEX_API_KEY|GH_TOKEN|GITHUB_TOKEN)$/.test(k)));
assert.throws(() => fs.writeFileSync("/opt/probe/change", "forbidden"), {code:/^(EROFS|EACCES)$/});
if (mode === "ro") assert.throws(() => fs.writeFileSync("/work/change", "forbidden"), {code:"EROFS"});
else fs.writeFileSync("/work/change", "permitted");
assert(Object.values(os.networkInterfaces()).flat().every(iface => iface.internal));
await new Promise((resolve, reject) => {
  const socket = net.connect({host:"192.0.2.1", port:443});
  socket.setTimeout(1500, () => socket.destroy(new Error("egress denial unverified")));
  socket.once("connect", () => socket.destroy(new Error("unexpected external route")));
  socket.once("error", e => ["ENETUNREACH","EHOSTUNREACH","EACCES","EPERM"].includes(e.code) ? resolve() : reject(e));
});
console.log(JSON.stringify({pidNamespace:fs.readlinkSync("/proc/self/ns/pid"), own, mode}));
