import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { chromium } from "playwright-core";

const [, , link, task, other] = process.argv;
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto(link);
  await page.evaluate(id => sessionStorage.setItem("factory-selected",id), other);
  await page.getByLabel("Local sign-in token").fill(process.env.FACTORY_LOCAL_TOKEN);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByText("TASK / " + task.slice(0,8), { exact: true }).waitFor();
  await page.reload();
  await page.getByText("TASK / " + task.slice(0,8), { exact: true }).waitFor();
  const missing = new URL(link);
  missing.searchParams.set("task", randomUUID());
  await page.goto(missing.href);
  await page.getByText("This linked task is unavailable on this controller.").waitFor();
  assert.equal(await page.locator(".task-heading").count(), 0);
  await page.getByRole("button", { name: /Show queue/ }).click();
  await page.locator(".queue-task").filter({ hasText: other.slice(0,8) }).click();
  await page.getByText("TASK / " + other.slice(0,8), { exact: true }).waitFor();
  assert.equal(new URL(page.url()).searchParams.has("task"), false);
} finally {
  await browser.close();
}
