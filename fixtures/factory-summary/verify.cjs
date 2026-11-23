// The provider runs this installed check as a separate process from the candidate.
const fs = require("node:fs");
const {spawnSync} = require("node:child_process");
const {candidate, selector} = JSON.parse(fs.readFileSync(0, "utf8"));
if (selector === "bounded-source") {
    console.log(selector);
    process.exit(Buffer.byteLength(candidate) <= 16384 && candidate.length > 0 ? 0 : 1);
}
const task = {format:"factory-task-evidence/v1",taskId:"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    activity:"Delivered", decisions:[], receipts:[]};
if (selector !== "render-repairs") process.exit(2);
function matches(repairs) {
    task.repairs = repairs;
    const result = spawnSync(process.execPath, ["-e", candidate], {input:JSON.stringify(task), timeout:5000, maxBuffer:8192});
    const lines = result.stdout?.toString().trimEnd().split("\n") ?? [];
    const previous = ["# Task " + task.taskId, "", "Activity: Delivered", "Recorded decisions: 0", "", "## Verification", "", "No verification receipts recorded."];
    if (task.repairs !== undefined) previous.splice(4, 0, "Repairs: " + task.repairs);
    return !result.error && result.status === 0 && !result.stderr.length && JSON.stringify(lines) === JSON.stringify(previous);
}
// Each assertion exercises the actual submitted program in a separate child process.
const passed = [3, 0, undefined].map(matches).every(Boolean);
console.log(selector);
process.exit(passed ? 0 : 1);
