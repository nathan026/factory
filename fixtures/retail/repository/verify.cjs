// Each selector runs in a fresh container. The provider records the exit status.
const fs = require("node:fs");
const { candidate, cases, decision, selector } = JSON.parse(fs.readFileSync(0, "utf8"));
const rule = JSON.parse(candidate);
const example = JSON.parse(cases).find(row => "order-offset/" + row.offsetCents === selector);
if (!example || !["greater", "greaterOrEqual"].includes(rule.comparison)) process.exit(2);
const amount = Number(decision.value) + example.offsetCents;
const actual = rule.comparison === "greater" ? amount > rule.thresholdCents : amount >= rule.thresholdCents;
console.log(`Order of ${amount} cents should have free shipping = ${example.freeShipping ? "True" : "False"}`);
process.exit(actual === example.freeShipping ? 0 : 1);
