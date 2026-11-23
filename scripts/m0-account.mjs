// Read capabilities from the pinned CLI. No thread is opened and raw account data stays private.
import {spawn} from "node:child_process";
import {createInterface} from "node:readline";
import {fileURLToPath} from "node:url";
import {readFileSync} from "node:fs";
import {isAbsolute} from "node:path";

const runtime = new URL("../src/Factory.Runtime/", import.meta.url);
const paths = JSON.parse(readFileSync(new URL("native-cli.json", runtime), "utf8"));
const pinned = paths[process.platform + "-" + process.arch];
if (!pinned) throw new Error("Unsupported pinned Codex platform");
if (process.platform === "linux" && !isAbsolute(process.env.CODEX_HOME ?? "")) {
  throw new Error("Linux subscription checks require an absolute deployment CODEX_HOME");
}
const exe = fileURLToPath(new URL("node_modules/" + pinned, runtime));
const keys = ["PATH","SYSTEMROOT","WINDIR","COMSPEC","TEMP","TMP","USERPROFILE","LOCALAPPDATA","APPDATA","CODEX_HOME"];
if (process.platform === "linux") keys.push("HOME");
const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => keys.includes(k.toUpperCase())));
env.CODEX_EXEC_SERVER_URL = "none";
const child = spawn(exe, ["-c",'forced_login_method="chatgpt"', "app-server","--stdio"], {
  env, stdio:["pipe","pipe","ignore"], windowsHide:true
});
const reader = createInterface({input:child.stdout});
const pending = new Map();
let seq = 0;
reader.on("line", line => {
  const msg = JSON.parse(line);
  if (pending.has(msg.id)) {
    const {resolve,reject} = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error("Metadata call unavailable"));
    else resolve(msg.result);
  }
});
function stop() {
  for (const p of pending.values()) p.reject(new Error("Metadata process stopped"));
  pending.clear();
}
child.on("error",stop);
child.on("close",stop);
function rpc(method, params) {
  const id = ++seq;
  return new Promise((resolve,reject) => {
    pending.set(id,{resolve,reject});
    child.stdin.write(JSON.stringify({jsonrpc:"2.0",id,method,params})+"\n");
  });
}
const signInRequired = new Error("ChatGPT subscription sign-in required");
const timeout = setTimeout(() => {stop();child.kill();},30000);
try {
  await rpc("initialize",{clientInfo:{name:"factory_qualification",version:"1.0"},capabilities:{experimentalApi:true}});
  child.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n');
  const {account} = await rpc("account/read",{refreshToken:false});
  if (account?.type !== "chatgpt") throw signInRequired;
  const report = {observedUtc:new Date().toISOString(),auth:"ChatGPT"};
  const [quota,models] = await Promise.allSettled([rpc("account/rateLimits/read",{}),rpc("model/list",{limit:100})]);
  if (quota.status === "fulfilled") {
    const limits = quota.value.rateLimits;
    report.windows = [limits?.primary,limits?.secondary].filter(Boolean).map(w =>
      ({minutes:w.windowDurationMins, usedPercent:w.usedPercent, resetsAt:w.resetsAt}));
  }
  if (models.status === "fulfilled") report.advertisedDefaultModel = models.value.data.find(m=>m.isDefault)?.model;
  console.log(JSON.stringify(report,null,2));
} catch (error) {
  console.error(error === signInRequired ? signInRequired.message : "Subscription metadata was unavailable");
  process.exitCode=1;
} finally {
  clearTimeout(timeout);child.stdin.end();child.kill();reader.close();
}
