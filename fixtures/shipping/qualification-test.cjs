// Executed as repository code in the read-only Docker lease. Only synthetic inputs are present.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const {marker, sibling} = JSON.parse(fs.readFileSync("/work/lease-fixture.json", "utf8"));
assert.equal(fs.readFileSync("/work/" + marker, "utf8"), marker);
for (const file of ["/work/" + sibling, "/var/run/docker.sock", "/tmp/control-canary", "/tmp/runtime-home/auth-canary"])
  assert.throws(() => fs.readFileSync(file), {code:"ENOENT"});
assert.equal(process.getuid(), 1000);
assert.throws(() => fs.writeFileSync("/work/unauthorized-change", "denied"), {code:"EROFS"});
assert(Object.values(os.networkInterfaces()).flat().every(iface => iface.internal));
assert(!Object.keys(process.env).some(k => /^(OPENAI_API_KEY|CODEX_API_KEY|GH_TOKEN|GITHUB_TOKEN)$/.test(k)));
console.log(JSON.stringify({verified:true, marker}));
