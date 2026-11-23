import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [, , desk, url, number, action] = process.argv;
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.goto(desk);
  await page.getByLabel("Local sign-in token").fill(process.env.FACTORY_LOCAL_TOKEN);
  await page.getByRole("button", { name: "Sign in" }).click();
  if (action === "reconcile") {
    await page.getByRole("button", { name: "Reconcile original publication" }).click();
  }
  for (let visit = 0; visit < 2; visit++) {
    if (visit) await page.reload();
    await page
      .locator(".task-heading")
      .getByText("Draft PR #" + number)
      .waitFor();
    const summary = page.getByRole("region", { name: "Task summary" });
    await summary.getByText("The factory changed shipping-rule.json.").waitFor();
    await summary.getByText("The independent reviewer approved these changes.").waitFor();
    const delivery = page.getByRole("region", { name: "Repository delivery" });
    const link = delivery.getByRole("link", { name: "View PR #" + number });
    assert.equal(await link.getAttribute("href"), url);
    assert.equal(
      await delivery.getByRole("link", { name: "Issue #1" }).getAttribute("href"),
      url.replace(/\/pull\/\d+$/, "/issues/1"),
    );
  }
} finally {
  await browser.close();
}
