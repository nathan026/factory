import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [, , desk, base, mode, scope] = process.argv;
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.goto(desk);
  await page.getByLabel("Local sign-in token").fill(process.env.FACTORY_LOCAL_TOKEN);
  await page.getByRole("button", {name: "Sign in"}).click();
  await page.getByRole("button", {name: "Request follow-up"}).click();
  const form = page.getByRole("region", {name: "Confirm issue action"});
  await form.getByLabel("Approved follow-up scope").fill(scope || "Check the boundary again.");
  assert.ok((await form.innerText()).includes(base));
  assert.ok(await form.getByRole("button", {name: "Approve scope and start follow-up"}).isEnabled());
  if (mode === "start") {
    const response = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith("/actions/follow-up"));
    await form.getByRole("button", {name:"Approve scope and start follow-up"}).click();
    assert.equal((await (await response).json()).stage, "started");
  } else {
    await form.getByRole("button", {name: "Cancel"}).click();
    await page.getByRole("button", {name: "Finish issue"}).click();
    await form.getByText("Preview the public comment").click();
    assert.ok((await form.innerText()).includes("PR #20"));
    const close = form.getByRole("button", {name: "Post summary and close issue"});
    if (mode === "linked-preview") {
      const cleanup=form.getByRole("list", {name:"Branch cleanup"});
      await cleanup.getByText("feature/linked", {exact:true}).waitFor();
      assert.match(await cleanup.innerText(), /protected.*GitHub protects/);
      assert.ok(!(await cleanup.innerText()).includes("unrelated"));
      await form.getByRole("button", {name:"Cancel"}).click();
    } else if (mode === "finish") {
      await close.click();
      await page.getByRole("link", {name:"Read the final summary"}).waitFor();
    } else {
      assert.ok(await close.isDisabled());
    }
  }
} finally {
  await browser.close();
}
