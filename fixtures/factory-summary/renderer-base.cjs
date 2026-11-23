// Read task.json from a factory-task-evidence/v1 export on stdin.
const fs = require("node:fs");
const task = JSON.parse(fs.readFileSync(0, "utf8"));
if (task.format !== "factory-task-evidence/v1" ||
    !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(task.taskId) ||
    !["Queued", "Running", "Waiting", "Blocked", "Delivered"].includes(task.activity) ||
    !Array.isArray(task.decisions) || !Array.isArray(task.receipts)) {
    throw new Error("Unsupported task evidence");
}
const lines = ["# Task " + task.taskId, "", "Activity: " + task.activity,
    "Recorded decisions: " + task.decisions.length, "", "## Verification", ""];
for (const [i, receipt] of task.receipts.entries()) {
    if (!["Passed", "Failed", "Rejected"].includes(receipt.status) || !/^[a-f0-9]{64}$/.test(receipt.digest))
        throw new Error("Invalid verification receipt");
    lines.push("- Receipt " + (i + 1) + " · " + receipt.status + " · " + receipt.digest);
}
if (!task.receipts.length) lines.push("No verification receipts recorded.");
console.log(lines.join("\n"));
