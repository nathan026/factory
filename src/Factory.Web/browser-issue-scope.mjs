import assert from "node:assert/strict";
import { build } from "esbuild";
import { chromium } from "playwright-core";
import { fileURLToPath } from "node:url";

const source = `
  import React from "react";
  import { createRoot } from "react-dom/client";
  import { GitHubIssueForm } from "./issue-picker";
  const item = {number:7,id:17,title:"Reduce scrolling",canReview:true};
  const preview = {issueId:17, title:item.title, body:"- Show a spinner\\n  while a worker runs\\n- [ ] Keep the latest message visible",
    assigned:false, canBegin:false, paths:[], checks:[], saved:null};
  const api = async (route, options) => {
    if (route.startsWith("github/issues?")) return {items:[item]};
    if (route === "github/issues/7/scope") {
      if (options?.body) preview.saved = {id:"approval",request:options.body};
      return preview;
    }
    throw new Error("Unexpected route " + route);
  };
  createRoot(document.body.appendChild(document.createElement("main"))).render(
    <GitHubIssueForm api={api} ready intake={{issues:[]}} goal="Use a racing palette" />);
`;
const bundle = await build({stdin:{contents:source,loader:"tsx",resolveDir:fileURLToPath(new URL(".",import.meta.url))},
  bundle:true, write:false});
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await page.addScriptTag({content:bundle.outputFiles[0].text});
  await page.getByText("Manual scope review",{exact:true}).click();
  await page.getByRole("button",{name:"Review scope"}).click();
  assert.equal(await page.getByLabel("Work to complete").inputValue(), "Reduce scrolling");
  assert.equal(await page.getByLabel("What should work").inputValue(), "Show a spinner while a worker runs\nKeep the latest message visible");
  assert.doesNotMatch(await page.locator("body").innerText(), /racing palette|installed project policy/i);
  await page.getByRole("button",{name:"Save checklist"}).click();
  await page.getByRole("heading",{name:"Saved checklist for issue #7"}).waitFor();
  assert.match(await page.getByRole("status").innerText(), /checks are assigned to issue #7/);
  assert.equal(await page.getByRole("button",{name:"Begin",exact:true}).count(), 0);
} finally {
  await browser.close();
}
