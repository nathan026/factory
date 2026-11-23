import React, {useState} from "react";
import {Api,Workspace} from "./types";

type Preview = {command:{taskId:string;previousBase:string;newBase:string;policyHash:string;operationId:string;gateSequence:number};goal:string;paths:string[]};

export function WriterRecovery({task,api,ready,update}:{task:Workspace;api:Api;ready:boolean;update:()=>void}) {
  const [preview,setPreview]=useState<Preview|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState("");
  const route="github/issues/"+task.repository!.number+"/writer-recovery";
  async function act(){
    setError("");setBusy(true);
    try{
      if(preview){await api(route,{body:preview.command,timeoutMs:90000});setPreview(null);update();}
      else setPreview(await api<Preview>(route+"?taskId="+task.id));
    }catch(e){setError((e as Error).message);setPreview(null);update();}
    finally{setBusy(false);}
  }
  return <section className="decision" aria-label="Writer recovery"><h2>The coding session stopped</h2>
    <p>The workspace was released before the writer sealed its changes. Nothing was submitted for review.</p>
    {preview && <><p>{preview.goal}</p><p>Approved files</p><ul>{preview.paths.map(p=><li key={p}><code>{p}</code></li>)}</ul>
      <p>Start from <code>{preview.command.newBase}</code>.</p>
      {preview.command.previousBase!==preview.command.newBase && <p>Main moved. The factory will check that every admitted file still matches, then plan again on the new revision.</p>}
    </>}
    <button disabled={!ready||busy} onClick={act}>{preview ? "Approve and resume" : "Review writer recovery"}</button>
    {preview && <button className="secondary" disabled={busy} onClick={()=>setPreview(null)}>Cancel</button>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
