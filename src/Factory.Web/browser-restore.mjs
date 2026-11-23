import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [, , url, note] = process.argv;
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto(url);
  await page.getByLabel("Local sign-in token").fill(process.env.FACTORY_LOCAL_TOKEN);
  await page.getByRole("button", { name: "Sign in" }).click();
  const panel = page.getByRole("region", { name: "Restore reconciliation" });
  const release = panel.getByRole("button", { name: "Release restored controller" });
  await release.waitFor();
  assert(await release.isDisabled());
  await panel.getByLabel("Reconciliation evidence").fill(note);
  await panel.getByRole("checkbox").check();
  await release.click();
  await panel.getByText("Release recorded. Restart this controller to enable dispatch.").waitFor();
  await page.reload();
  await panel.getByText(note).waitFor();
} finally {
  await browser.close();
}
