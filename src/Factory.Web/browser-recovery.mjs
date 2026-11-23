import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const browser = await chromium.launch();
try {
  const phone = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await phone.goto(process.argv[2]);
  await phone.getByLabel("Local sign-in token").fill(process.env.FACTORY_LOCAL_TOKEN);
  await phone.getByRole("button", { name: "Sign in" }).click();
  await phone.getByRole("button", { name: /Show queue/ }).click();
  await phone.getByRole("button", { name: "Blocked 1" }).click();
  await phone.getByRole("button", { name: /Resolve a boundary conflict/ }).click();
  await phone.getByRole("heading", { name: "No progress" }).waitFor();
  const resume = phone.getByRole("button", { name: "Resume from checkpoint" });
  assert.equal(await resume.isDisabled(), true);
  await phone.getByText("Interrupted operation", { exact: true }).click();
  await phone.getByText(/\.Plan$/).waitFor();
  await phone.getByLabel("Recovery note").fill("Checked the interrupted fixture operation.");
  await phone.route(
    "**/recover",
    async (route) => {
      assert.equal((await route.fetch()).status(), 204);
      await route.abort("failed");
    },
    { times: 1 },
  );
  await resume.click();
  await phone.locator(".task-heading").getByText("Delivered locally").waitFor();
  await phone.reload();
  await phone
    .locator(".thread")
    .getByText(/Checked the interrupted fixture operation/)
    .waitFor();
  assert.equal(await resume.count(), 0);
  assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
} finally {
  await browser.close();
}
