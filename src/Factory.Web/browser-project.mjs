import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [, , url, mode, expected] = process.argv;
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.goto(url);
  await page.getByLabel("Local sign-in token").fill(process.env.FACTORY_LOCAL_TOKEN);
  await page.getByRole("button", { name: "Sign in" }).click();
  if (mode === "decision") {
    await page.getByRole("button", { name: expected, exact: true }).waitFor();
  } else {
    await page.getByRole("button", { name: "Evidence", exact: true }).click();
    const section = page.getByRole("region", { name: "Local CI" });
    await section.getByText(expected, { exact: false }).waitFor();
    assert.equal(await section.locator("summary").filter({ hasText: "Failed" }).count(), mode === "blueprint" ? 0 : 1);
    if (mode === "blueprint") {
      await page.getByText("Implementation · Revise", {exact:true}).click();
      await page.getByText(/local\/order-offset\/0/).filter({visible:true}).first().waitFor();
    }
    assert.equal(await section.locator("summary").filter({ hasText: "Passed" }).count(), 1);
  }
} finally {
  await browser.close();
}
