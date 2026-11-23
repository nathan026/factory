import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [, , url, task] = process.argv;
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.goto(url);
  await page.getByLabel("Local sign-in token").fill(process.env.FACTORY_LOCAL_TOKEN);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.locator(".queue-task").filter({ hasText: task }).click();
  const openFunding = () => page.getByRole("button", { name: "Funding", exact: true }).click();
  await openFunding();
  const question = page.getByLabel("Question for the fixture owner");
  await question.fill("Summarize the next fixture check.");
  await page.route("**/funding/advice", async (route) => {
    assert.equal((await route.fetch()).status(), 200);
    await route.abort("failed");
  }, { times: 1 });
  const send = page.getByRole("button", { name: "Request fixture advice" });
  await send.click();
  await page.getByRole("alert").waitFor();
  await page.reload();
  await openFunding();
  assert.equal(await question.inputValue(), "Summarize the next fixture check.");
  await send.click();
  await page.waitForFunction(() => document.querySelector("textarea")?.value === "");
  const panel = page.getByRole("region", { name: "Task funding" });
  await panel.getByText(/Paid calls disabled/).waitFor();
  assert.equal(await panel.getByRole("heading", { name: "Summarize the next fixture check." }).count(), 1);
} finally {
  await browser.close();
}
