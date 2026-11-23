import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const browser = await chromium.launch();
try {
  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const desktop = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const phone = await mobile.newPage();
  const desk = await desktop.newPage();
  const errors = [];
  const reads = [];
  const messageIds = [];
  for (const page of [phone, desk]) {
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("request", (r) => {
      const path = new URL(r.url()).pathname;
      if (r.method() === "GET") reads.push(path);
      if (path.endsWith("/messages") && r.method() === "POST") messageIds.push(r.postDataJSON().id);
    });
  }
  async function signIn(page) {
    await page.goto(process.argv[2]);
    await page.getByLabel("Local sign-in token").fill(process.env.FACTORY_LOCAL_TOKEN);
    await page.getByRole("button", { name: "Sign in" }).click();
  }
  const button = (page, name) => page.getByRole("button", { name, exact: true });
  const keep = (page) => button(page, "Keep 5000 cents");
  const thread = (page) => page.locator(".thread");
  const latest = (page) => page.getByRole("region", {name:"Latest owner reply"});
  async function expectReplyInView(page) {
    await page.waitForFunction(() => {
      const reply = document.querySelector(".latest-reply .reply-text")?.getBoundingClientRect();
      return reply && reply.top >= 0 && reply.bottom <= innerHeight;
    });
  }
  await signIn(phone);
  await phone.getByLabel("Sample task").selectOption("scope-change");
  const opening = "Please keep a record of the threshold discussion.";
  await phone.getByLabel(/Opening message/).fill(opening);
  // Recover a committed admission from the queue when the client misses the response.
  await phone.route("**/api/tasks", async route => {
    assert.equal((await route.fetch()).status(), 204);
    await route.abort("failed");
  }, { times: 1 });
  await button(phone, "Start task").click();
  await keep(phone).waitFor();
  await phone.getByText("Expert reasoning and sources").click();
  await phone.getByText(/The cited sources do not establish authority/).waitFor();
  await phone.getByText(/Decision authority.*Operator/).waitFor();
  await thread(phone).getByText(opening, { exact: true }).waitFor();

  assert.equal(await phone.evaluate(() => sessionStorage.getItem("factory-create-id")), null);

  const note = "Approve 4000 cents? Explain the consequences first.";
  await phone.getByLabel("Message the lifecycle owner").fill(note);
  // Drop the acknowledgement after the controller commits, then retry from the same tab.
  await phone.route(
    "**/messages",
    async (route) => {
      assert.equal((await route.fetch()).status(), 200);
      await route.abort("failed");
    },
    { times: 1 },
  );
  await button(phone, "Send message").click();
  await phone.getByRole("alert").waitFor();
  await button(phone, "Send message").click();
  await latest(phone).getByText(/Use a decision button to record your choice/).waitFor();
  await expectReplyInView(phone);
  await latest(phone).getByText("Your message", {exact:true}).click();
  await latest(phone).getByText(note, {exact:true}).waitFor();
  assert.equal(await phone.getByText(note, {exact:true}).count(), 1);
  assert.equal(messageIds.length, 2);
  assert.equal(messageIds[0], messageIds[1]);
  await button(phone, "Brief").click();
  await phone.getByText("No decision recorded yet.").waitFor();
  await phone.getByRole("heading", { name: "Acceptance criteria" }).waitFor();
  await keep(phone).waitFor();
  // Two successive idle polls of the parked task transfer only summaries.
  const poll = () => phone.waitForResponse((r) => r.url().endsWith("/api/dashboard/queue") && r.ok());
  await poll();
  const mark = reads.length;
  await poll();
  await poll();
  assert.deepEqual([...new Set(reads.slice(mark))], ["/api/dashboard/queue"]);
  await mobile.setOffline(true);
  await phone.getByText(/Controller unavailable/).waitFor();
  assert.equal(await keep(phone).isDisabled(), true);

  await signIn(desk);
  await keep(desk).waitFor();
  await button(desk, "+ New task").click();
  await desk.getByLabel("Sample task").selectOption("expert-comparison");
  await button(desk, "Start task").click();
  await desk.getByRole("heading", { name: "Choose the boundary comparison" }).waitFor();
  assert.equal(await thread(desk).getByText(note, { exact: true }).count(), 0);
  await button(desk, "Brief").click();
  await desk.getByText("Lifecycle owner · Delegated implementation decision").waitFor();
  await desk.getByText("Expert reasoning and sources").click();
  await desk.getByText(/The verifier still needs to check/).waitFor();
  await desk.getByRole("button", { name: /^Decisions 1$/ }).click();
  assert.equal(await desk.locator(".queue-task").count(), 1);
  await desk.locator(".queue-task").click();
  await latest(desk).getByText(/Use a decision button to record your choice/).waitFor();
  await keep(desk).focus();
  await desk.keyboard.press("Enter");
  await thread(desk).getByText("Local evidence package ready for inspection.", { exact: true }).waitFor();
  assert.equal(await desk.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await desk.getByLabel("Message the lifecycle owner").fill("What has happened?");
  await button(desk,"Send message").click();
  await latest(desk).getByText(/The task reached delivery/).waitFor();
  await expectReplyInView(desk);
  await thread(desk).getByText(note, {exact:true}).waitFor();
  await mobile.setOffline(false);
  await phone.locator(".task-heading").getByText("Delivered locally").waitFor();
  await phone.getByText("Operator · Reserved product decision").waitFor();
  await phone.reload();
  await phone.getByRole("heading", { name: "Evaluate a scope change" }).waitFor();
  await latest(phone).getByText(/The task reached delivery/).waitFor();
  await thread(phone).getByText(note, {exact:true}).waitFor();
  assert.equal(
    reads.some((p) => p === "/api/tasks" || p.endsWith("/evidence") || p.includes("/owner/documents/")),
    false,
  );
  await button(phone, "Evidence").click();
  await phone.getByText(/^Failed ·/).click();
  await phone.getByText(/Fail · Order of 5000 cents/).waitFor();
  await phone.getByText(/^Passed ·/).click();
  await phone.getByText(/Pass · Order of 5000 cents/).waitFor();
  await phone.getByText("Review · Pass", { exact: true }).waitFor();
  await phone.getByText("shipping-rule.json", { exact: true }).click();
  await phone.locator("details[open] pre").filter({ hasText: "thresholdCents" }).waitFor();
  assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
}
