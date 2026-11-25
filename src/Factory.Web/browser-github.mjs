import assert from "node:assert/strict";
import { chromium } from "playwright-core";

const [, , desk, url, number, action] = process.argv;
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  const issueUrl = url.replace(/\/pull\/\d+$/, "/issues/1");
  const earlierNumber = Number(number) + 1;
  const earlierUrl = url.replace(/\/pull\/\d+$/, "/pull/" + earlierNumber);
  const commentId = 123456;
  const context = page.context();
  await context.route("**/api/dashboard/scenarios", async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), issueActions: true } });
  });
  // Supply completed issue history without posting to GitHub or closing an issue.
  await context.route("**/api/tasks/*/workspace", async route => {
    const response = await route.fetch();
    if (!response.ok()) return route.fulfill({ response });
    const task = await response.json();
    if (task.repository?.draft?.url !== url) return route.fulfill({ response });
    task.previousDrafts = [{
      number: earlierNumber, url: earlierUrl, headSha: task.repository.draft.headSha,
    }];
    task.issueAction = { kind: "finish", stage: "finished", commentId };
    await route.fulfill({ response, json: task });
  });
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
    await delivery.getByText("Earlier deliveries", { exact: true }).click();
    const links = [
      [delivery.getByRole("link", { name: "View PR #" + number }), url],
      [delivery.getByRole("link", { name: "Issue #1" }), issueUrl],
      [delivery.getByRole("link", { name: "PR #" + earlierNumber, exact: true }), earlierUrl],
      [delivery.getByRole("link", { name: "Read the final summary" }), issueUrl + "#issuecomment-" + commentId],
    ];
    const taskUrl = page.url();
    for (const [link, destination] of links) {
      assert.equal(await link.getAttribute("href"), destination);
      const expectedUrl = new URL(destination);
      assert.equal(await link.getAttribute("target"), "_blank");
      if (visit) continue;

      // A context route also intercepts the first request from the new tab.
      const requestUrl = new URL(expectedUrl);
      requestUrl.hash = "";
      await context.route(requestUrl.href, route => route.fulfill({ body: "GitHub destination" }));
      const [popup] = await Promise.all([
        page.waitForEvent("popup"),
        link.click(),
      ]);
      await popup.waitForURL(expectedUrl.href);
      assert.equal(await popup.evaluate(() => window.opener === null), true);
      await popup.close();
      await context.unroute(requestUrl.href);
      assert.equal(page.url(), taskUrl);
      assert.ok(await summary.isVisible());
      assert.ok(await link.isVisible());
    }
    await delivery.getByText("Issue closed.", { exact: false }).waitFor();
    assert.equal(await delivery.getByRole("button", { name: "Request follow-up" }).count(), 0);
    assert.equal(await delivery.getByRole("button", { name: "Finish issue" }).count(), 0);
    if (visit) continue;

    await page.getByRole("button", { name: "Conversation", exact: true }).click();
    const composer = page.getByLabel("Message the lifecycle owner");
    const message = "First line\nSecond line";
    const reply = "Browser test acknowledgement.";
    let submissions = 0;
    await context.route("**/api/tasks/*/messages", async route => {
      submissions++;
      const { id, text } = route.request().postDataJSON();
      assert.equal(route.request().method(), "POST");
      assert.equal(text, message);
      await route.fulfill({ json: { id, text, reply } });
    });
    await composer.fill("First line");
    await composer.press("End");
    await composer.press("Enter");
    await page.keyboard.type("Second line");
    assert.equal(await composer.inputValue(), message);
    assert.equal(submissions, 0);
    await composer.press("Control+Enter");
    await page.getByRole("region", { name: "Latest owner reply" }).getByText(reply).waitFor();
    assert.equal(await composer.inputValue(), "");
    assert.equal(submissions, 1);
    assert.equal(page.url(), taskUrl);
  }
} finally {
  await browser.close();
}
