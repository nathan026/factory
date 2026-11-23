using System.Text.Json;
using Factory;
using Microsoft.Data.Sqlite;

internal static class OwnerDiscussionChecks
{
    private static void CheckWorkerResume(IControlledProject project, FactoryDatabase db, Session journal)
    {
        TaskStore Restart() => new(db,new Lifecycle(project,ownerRuntime:"codex-subscription"),()=>project,liveOwner:true);
        foreach (var supported in new[] { true, false })
        {
            var store = Restart();
            var task = store.Create(Guid.NewGuid(),"restated-decision");
            DispatchClaim? owner = null;
            var session = Guid.NewGuid();
            for (int i = 0; i < 16 && owner is null; i++)
            {
                var ticket = store.Next(true);
                if (ticket.Id is null) continue;
                var isOwner = ticket.Id.EndsWith(".AnswerWorker", StringComparison.Ordinal);
                var claim = store.Claim(ticket.Id, "controlled-" + ticket.Id, isOwner ? session : Guid.NewGuid())!;
                if (isOwner) owner = claim;
                else store.Receive(ticket.Id,claim.Token,claim.Order.Execute(project));
            }
            if (owner?.Order.Discussion is not { } q || owner.WorkerMode != "codex-subscription" || q.Recipient != owner.Order.Assignment.Reference())
                throw new Exception("The asking worker never reached its bound owner.");
            var value = JsonSerializer.SerializeToElement(new { fact = supported ? 0 : -1, requestHash = GateInput.Hash(q) },Wire.Json);
            try { store.Receive(owner.Order.Id,owner.Token,value); throw new Exception("Unsigned worker answer accepted."); }
            catch (CommandConflict) { }
            var events = JsonSerializer.Serialize(new[]{new { tool="read_file",path=q.Evidence.Path,sha256=q.Evidence.Digest,view="base" }});
            journal.Execute("""
                INSERT INTO codex_attempts VALUES(@run,@task,'AnswerWorker','sealed',@thread,@run,@result,@events,@source,@policy);
                INSERT INTO leases VALUES(@run,@task,'AnswerWorker','owner','released',@run,@digest,NULL,@policy);
                """, ("run",session.ToString()), ("task",task.Id.ToString()), ("thread",Guid.NewGuid().ToString()),
                ("result",value.GetRawText()), ("events",events), ("source",q.SourceRevision), ("policy",GateInput.Hash(q)),
                ("digest",Wire.Digest(value.GetRawText())));
            store.Receive(owner.Order.Id,owner.Token,value);
            store = Restart();
            store.Receive(owner.Order.Id,owner.Token,value);
            store.Next(true);
            var recovered = store.List().Single(t => t.Id == task.Id);
            if (!supported)
            {
                if (recovered.Activity != Activity.Blocked || recovered.OwnerReply is not null)
                    throw new Exception("Unsupported fact resumed implementation.");
                continue;
            }
            var ticketAfterRestart = store.Next(true);
            var writer = store.Claim(ticketAfterRestart.Id!, "resumed", Guid.NewGuid())!;
            if (writer.Order.Action != WorkAction.Implement || writer.Order.Assignment.Reference() != q.Recipient ||
                writer.Order.Context?.OwnerReply != recovered.OwnerReply || recovered.OwnerReply?.AskingRun != q.MessageId ||
                recovered.OwnerReply.Question == project.AdmittedQuestion(task.Scenario).Text ||
                !recovered.OwnerReply.Answer.Contains(recovered.Answer!.Text, StringComparison.Ordinal))
                throw new Exception("The answer did not reach the originating implementation assignment.");
            store.Receive(writer.Order.Id,writer.Token,writer.Order.Execute(project));
            for (var i=0;i<40 && store.Tick();i++) { }
            var funding = JsonSerializer.SerializeToElement(store.FundingReport(task.Id,new FundingProfile()),Wire.Json);
            if (funding.GetProperty("subscriptionWorkerClaims").GetInt32() != 1 || funding.GetProperty("controlledWorkerClaims").GetInt32() < 2)
                throw new Exception("Subscription owner claim was counted as a controlled worker.");
            using var zip = new System.IO.Compression.ZipArchive(new MemoryStream(store.PilotEvidence(task.Id)));
            using var exported = JsonDocument.Parse(zip.GetEntry("task.json")!.Open());
            if (exported.RootElement.GetProperty("evidenceClass").GetString() != "subscription-owner" ||
                zip.GetEntry("codex-attempts.json") is null) throw new Exception("Mixed task exported as a controlled-only observation.");
            if (store.List().Single(t => t.Id == task.Id).Activity != Activity.Delivered)
                throw new Exception("Verification or independent review failed after the worker resumed.");
        }
        Console.WriteLine("Passed: synthetic owner turn, restart before apply, exact worker continuation and unsupported reply block");
    }

    // This test constructs a synthetic receipt to exercise the controller side of the trust boundary.
    internal static void Run(IControlledProject project)
    {
        var temp = Directory.CreateTempSubdirectory();
        var prior = Environment.GetEnvironmentVariable("FACTORY_LEASE_DB");
        try
        {
            var path = Path.Combine(temp.FullName,"synthetic-journal.sqlite");
            Environment.SetEnvironmentVariable("FACTORY_LEASE_DB",path);
            var db = new FactoryDatabase(temp.FullName,null);
            TaskStore Start() => new(db,new Lifecycle(project),()=>project,liveOwner:true);
            var store = Start();
            var task = Guid.NewGuid();
            store.Create(task,"known-decision");
            for (var i=0; i<100 && store.Tick(); i++) { }
            if (store.List().Single().Activity != Activity.Delivered) throw new Exception("Fixture did not deliver.");
            const string text = "What happens when the basket reaches exactly fifty dollars?";
            var msg = Guid.NewGuid();
            var pending = store.Discuss(task,msg,text);
            var id = pending.Operation!;
            if (pending.Status != "Pending" || store.Discuss(task,msg,text) != pending || store.Next(true).Id != id)
                throw new Exception("Discussion intent was lost or duplicated.");
            var session = Guid.NewGuid();
            var claim = store.Claim(id,"test-scheduler",session)!;
            var question = claim.Order.Discussion!;
            var output = JsonSerializer.SerializeToElement(new { fact=0, requestHash=GateInput.Hash(question) },Wire.Json);
            if (claim.WorkerMode != "codex-subscription" || store.Claim(id,"test-scheduler",Guid.NewGuid()) is not null)
                throw new Exception("Owner claim had a substitute or duplicate worker.");
            try { claim.Order.Execute(project); throw new Exception("Fixture executed an owner turn."); }
            catch (InvalidOperationException e) when (e.Message.Contains("native Codex")) { }
            try { store.Receive(id,claim.Token,output); throw new Exception("Unsigned answer accepted."); }
            catch (CommandConflict) { }

            using var native = new SqliteConnection(new SqliteConnectionStringBuilder { DataSource=path }.ToString());
            native.Open();
            var local = new Session(native,null);
            local.Execute("""
                CREATE TABLE codex_attempts(run,task,action,status,thread,generation,result,events,source,policy);
                CREATE TABLE leases(run,task,operation,role,state,generation,outcome_hash,candidate_hash,policy);
                """);
            var receipt = output.GetRawText();
            var hash = GateInput.Hash(question);
            var events = JsonSerializer.Serialize(new[]{new { tool="read_file",path=OwnerQuestion.Path,sha256=question.Evidence.Digest,view="base" }});
            local.Execute("""
                INSERT INTO codex_attempts VALUES(@run,@task,'Discuss','sealed',@thread,'test',@result,@events,@source,@policy);
                INSERT INTO leases VALUES(@run,@task,'Discuss','owner','released','test',@digest,NULL,@policy);
                """, ("run",session.ToString()),("task",task.ToString()),("thread",Guid.NewGuid().ToString()),("result",receipt),
                ("events",events),("source",question.SourceRevision),("policy",hash),("digest",Wire.Digest(receipt)));
            local.Execute("UPDATE codex_attempts SET events='[]'");
            try { store.Receive(id,claim.Token,output); throw new Exception("Unread evidence accepted."); }
            catch (CommandConflict) { }
            local.Execute("UPDATE codex_attempts SET events=@events", ("events",events));
            store.Receive(id,claim.Token,output);
            var restarted = Start();
            var answer = restarted.Discuss(task,msg,text);
            var repeat = restarted.Discuss(task,Guid.NewGuid(),text);
            if (answer.Status != "Answered" || answer.EvidenceHash != question.Evidence.Digest ||
                answer.Reply != JsonSerializer.Deserialize<string[]>(question.Evidence.Content)![0] ||
                repeat.Operation != id || repeat.Reply != answer.Reply || restarted.Next(true).Id is not null)
                throw new Exception("Restart or repeat lost the accepted exchange.");
            restarted.Receive(id,claim.Token,output);
            var follow = restarted.Discuss(task,Guid.NewGuid(),"Which amount applies to another basket?");
            var current = restarted.List().Single();
            var changed = JsonSerializer.SerializeToNode(current,Wire.Json)!;
            changed["ownerId"] = Guid.NewGuid();
            db.Write(s => s.Execute("UPDATE tasks SET document=@doc WHERE id=@id",
                ("doc",changed.ToJsonString()), ("id",task.ToString())));
            if (restarted.Claim(follow.Operation!,"stale",Guid.NewGuid()) is not null ||
                restarted.Discuss(task,follow.Id,follow.Text).Status != "Unavailable")
                throw new Exception("A stale owner question received a claim.");
            var late = restarted.Discuss(task,Guid.NewGuid(),"How much must the basket contain?");
            var lateSession = Guid.NewGuid();
            var lateClaim = restarted.Claim(late.Operation!,"before-change",lateSession)!;
            var lateHash = GateInput.Hash(lateClaim.Order.Discussion);
            var lateResult = JsonSerializer.SerializeToElement(new { fact=0, requestHash=lateHash },Wire.Json);
            local.Execute("""
                UPDATE codex_attempts SET run=@run,result=@result,policy=@policy;
                UPDATE leases SET run=@run,outcome_hash=@digest,policy=@policy;
                """, ("run",lateSession.ToString()),("result",lateResult.GetRawText()),
                ("digest",Wire.Digest(lateResult.GetRawText())), ("policy",lateHash));
            changed["ownerId"] = Guid.NewGuid();
            db.Write(s => s.Execute("UPDATE tasks SET document=@doc WHERE id=@id",
                ("doc",changed.ToJsonString()), ("id",task.ToString())));
            restarted.Receive(late.Operation!,lateClaim.Token,lateResult);
            if (restarted.Discuss(task,late.Id,late.Text).Status != "Stale")
                throw new Exception("An authenticated result answered a superseded owner checkpoint.");
            var held = restarted.Discuss(task,Guid.NewGuid(),"How high is the freight cutoff?");
            var heldClaim = restarted.Claim(held.Operation!,"quota",Guid.NewGuid())!;
            restarted.ParkRuntime(held.Operation!,new(heldClaim.Token,"Quota","Subscription"));
            if (restarted.Discuss(task,held.Id,held.Text).Status != "Quota" || restarted.Next(true).Id is not null)
                throw new Exception("A subscription wait retried or advanced the phase.");
            var timeout = restarted.Discuss(task,Guid.NewGuid(),text);
            db.Write(s => s.Execute("UPDATE discussion_jobs SET deadline=0 WHERE operation=@id",("id",timeout.Operation!)));
            restarted.ReconcileStalls();
            if (restarted.Discuss(task,timeout.Id,text).Status != "Expired" || restarted.Next(true).Id is not null)
                throw new Exception("Expired discussion stayed eligible.");
            CheckWorkerResume(new Factory.Fixtures.SampleRuntime(ownerDiscussion:true), db, local);
            Console.WriteLine("Passed: controlled owner discussion scope, unsigned and unread evidence, restart, replay, stale owner and timeout");
        }
        finally
        {
            Environment.SetEnvironmentVariable("FACTORY_LEASE_DB",prior);
            SqliteConnection.ClearAllPools();
            temp.Delete(true);
        }
    }
}
