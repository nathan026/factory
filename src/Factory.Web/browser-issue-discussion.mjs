import assert from "node:assert/strict";
import { build } from "esbuild";
import { chromium } from "playwright-core";
import { fileURLToPath } from "node:url";

const source = `
  import React from "react";
  import {createRoot} from "react-dom/client";
  import {IssueDiscussion} from "./issue-discussion";
  const api=(path,options={})=>window.fixtureApi(path,options.body);
  createRoot(document.body.appendChild(document.createElement("main"))).render(
    <IssueDiscussion api={api} ready number={7} created={id=>window.started(id)}/>);
`;
const bundle=await build({stdin:{contents:source,loader:"tsx",resolveDir:fileURLToPath(new URL(".",import.meta.url))},bundle:true,write:false});
const browser=await chromium.launch();
try {
  const page=await browser.newPage();
  let view={repositoryId:44,issueId:77,issueHash:"issue",baseSha:"revision",policyHash:"policy",
    ownerAvailable:true,messages:[],canBegin:false,stale:false,proposal:null};
  let starts=0, sends=0, saves=0;
  await page.exposeFunction("started",id=>{assert.equal(id,"same-task"); starts++;});
  await page.exposeFunction("fixtureApi",(path,body)=>{
    if(path.endsWith("/start")) {
      assert.equal(body.scopeApproval,"exact"); return {taskId:"same-task"};
    }
    if(path.endsWith("/scope")) {
      if(body) {assert.deepEqual(body.checks,view.proposal.checks); assert.equal(body.ownerMessage,view.messages.at(-1).id); saves++;}
      return {saved:null};
    }
    assert.ok(path.startsWith("github/issues/7/discussion"));
    if(body) {
      assert.equal(body.previous,view.messages.at(-1)?.id ?? null);
      sends++;
      view.messages.push({id:body.id,text:body.text,status:"Pending",reply:"Reading the issue."});
    } else if(path.includes("?poll") && view.messages.at(-1)?.status==="Pending") {
      view.messages.at(-1).status="Answered";
      view.messages.at(-1).reply=sends===1 ? "The work needs another file." : "The narrower scope is approved.";
      view.proposal={goal:"Keep messages visible",paths:["desk.css"],checks:["Verify keyboard navigation."]};
      view.canBegin=sends===2;
      view.scopeApproval=view.canBegin?"exact":null;
    }
    return view;
  });
  await page.route("http://127.0.0.1/desk-test",route=>route.fulfill({contentType:"text/html",body:"<html><body></body></html>"}));
  await page.goto("http://127.0.0.1/desk-test");
  await page.addScriptTag({content:bundle.outputFiles[0].text});
  await page.getByRole("button",{name:"Discuss issue #7"}).click();
  await page.getByRole("button",{name:"Record scope request"}).click();
  assert.equal(saves,1);
  assert.equal(await page.getByRole("button",{name:"Begin coding"}).count(),0);
  await page.reload();
  await page.addScriptTag({content:bundle.outputFiles[0].text});
  await page.getByRole("button",{name:"Discuss issue #7"}).click();
  await page.getByRole("region",{name:"Proposed scope"}).waitFor();
  assert.equal(sends,1);
  await page.getByLabel("Message the lifecycle owner").fill("Use the narrower approved layout change.");
  await page.getByRole("button",{name:"Send message"}).click();
  await page.getByRole("button",{name:"Begin coding"}).click();
  assert.equal(sends,2);
  assert.equal(starts,1);
} finally {
  await browser.close();
}
