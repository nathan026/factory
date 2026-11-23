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
    assigned:true,ownerAvailable:true,messages:[],canBegin:false,stale:false,proposal:null};
  let starts=0, sends=0, saves=0;
  await page.exposeFunction("started",id=>{assert.equal(id,"same-task"); starts++;});
  await page.exposeFunction("fixtureApi",(path,body)=>{
    if(path.endsWith("/start")) {
      assert.equal(body.scopeApproval,"exact"); return {taskId:"same-task"};
    }
    if(path.endsWith("/scope")) {
      if(body) {assert.deepEqual(body.checks,view.proposal.checks); assert.equal(body.ownerMessage,view.messages.at(-1).id); view.requestRecorded=true; saves++;}
      return {saved:null};
    }
    assert.ok(path.startsWith("github/issues/7/discussion"));
    if(body) {
      assert.equal(body.previous,view.messages.at(-1)?.id ?? null);
      if (sends === 0) {
        assert.match(body.text,/Prepare this issue for coding/);
        assert.match(body.text,/investigations/);
        assert.match(body.text,/decisions/);
      }
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
  const readiness = page.getByRole("region",{name:"Coding readiness"});
  assert.equal(await readiness.getByText("Not ready to begin",{exact:true}).count(),1);
  assert.equal(await page.getByRole("button",{name:"Begin coding"}).count(),0);
  await page.reload();
  await page.addScriptTag({content:bundle.outputFiles[0].text});
  await page.getByRole("button",{name:"Discuss issue #7"}).click();
  await page.getByRole("region",{name:"Proposed scope"}).waitFor();
  assert.equal(sends,1);
  assert.equal(await page.getByRole("button",{name:"Record scope request"}).count(),0);
  await page.getByLabel("Message the lifecycle owner").fill("Use the narrower approved layout change.");
  await page.getByRole("button",{name:"Send message"}).click();
  await page.getByRole("button",{name:"Begin coding"}).click();
  assert.equal(sends,2);
  assert.equal(starts,1);
  assert.equal(await readiness.getByText("Ready to begin",{exact:true}).count(),1);

  async function reopen() {
    await page.reload();
    await page.addScriptTag({content:bundle.outputFiles[0].text});
    await page.getByRole("button",{name:"Discuss issue #7"}).click();
    await readiness.waitFor();
  }
  view={...view,assigned:false,stale:true,canBegin:false,proposal:null,scopeApproval:null,
    messages:[{id:"historical",text:"Discuss the desk",reply:"Inspect the frontend before choosing files.\n\nSuggested checks\nKeep the composer visible.",status:"RecoveredReply"}]};
  await reopen();
  assert.equal(sends,2);
  await readiness.getByText("Previous owner finding").waitFor();
  await readiness.getByText("Inspect the frontend before choosing files.").waitFor();
  await readiness.getByText("Project setup needed.").waitFor();
  assert.equal(await readiness.getByRole("button",{name:"Begin coding"}).count(),0);
  await readiness.getByRole("button",{name:"Reassess readiness"}).click();
  assert.equal(sends,3);
  view={...view,stale:false,canBegin:false,proposal:null,scopeApproval:null,
    messages:[{id:"clarify",text:"Review again",reply:"Which screen size should we target?",status:"Answered",
      assessment:{investigations:["Locate the layout component."],decisions:["Which screen size should we target?"]}}]};
  await reopen();
  await readiness.getByRole("listitem").filter({hasText:"Locate the layout component."}).waitFor();
  await readiness.getByRole("listitem").filter({hasText:"Which screen size should we target?"}).waitFor();
  await readiness.getByRole("button",{name:"Answer the owner"}).click();
  assert.equal(await page.getByLabel("Message the lifecycle owner").evaluate(el=>document.activeElement===el),true);
  assert.equal(sends,3);
} finally {
  await browser.close();
}
