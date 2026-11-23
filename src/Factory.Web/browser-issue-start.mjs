import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [, , desk, repository, issue] = process.argv;
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.goto(desk);
  await page.getByLabel("Local sign-in token").fill(process.env.FACTORY_LOCAL_TOKEN);
  await page.getByRole("button", { name: "Sign in" }).click();
  const ids = [];
  for (let i = 0; i < 2; i++) {
    await page.getByRole("button", { name: "Start GitHub issue" }).click();
    await page.getByLabel("Open issue").selectOption(issue);
    if (i === 1) {
      await page.getByRole("button", { name: "Open existing task" }).click();
      await page.getByRole("region", { name: "Repository delivery" }).waitFor();
      break;
    }
    const result = page.waitForResponse(res => res.url().endsWith("/issues/" + issue + "/start") && res.request().method() === "POST");
    await page.getByRole("button", { name: "Begin", exact: true }).click();
    const response = await result;
    assert.equal(response.status(), 200);
    ids.push((await response.json()).taskId);
    await page.getByRole("region", { name: "Repository delivery" }).getByRole("link", { name: "Issue #" + issue }).waitFor();
    await page.reload();
  }
  assert.ok(ids[0]);
  assert.equal(ids.length, 1);
} finally {
  await browser.close();
}
