using System.Text.Json;
using Factory;

internal static class BlockedRevisionChecks
{
    // Reproduce the retained verification failure using the existing synthetic hosted issue fixture.
    public static void Run(string directory, HostedRepositoryProject previous)
    {
        var old = previous.Ci;
        var version = old.Rules.Policy.Version.Replace("/v1", "/v2");
        var layout = old.Rules.RepositoryLayout! with { AdditionalWrites = ["shell.html"] };
        var checks = new[] { new Criterion("bounded-source", "Use only the approved files."), new Criterion("theme", "Apply the new theme.") };
        var rules = old.Rules with {
            Policy = new(version, checks.Select(c => c.Id).ToArray(), 1), RepositoryLayout = layout,
            Goal = "Update the theme and page shell.", Revision = new(old.Rules.Policy.Version,old.Hash,checks)
        };
        File.WriteAllText(Path.Combine(directory, "shell.html"), "<main>Desk</main>");
        File.WriteAllText(Path.Combine(directory, "v2-policy.json"), JsonSerializer.Serialize(rules, Wire.Json));
        var ci = CiProfile.Load(directory,version);
        var project = new HostedRepositoryProject(directory,ci);
        var state = Directory.CreateDirectory(Path.Combine(directory,"blocked-round")).FullName;
        var db = new FactoryDatabase(state,null);
        var oldApprovalDirectory = Environment.GetEnvironmentVariable("FACTORY_FOLLOWUP_APPROVALS");
        Environment.SetEnvironmentVariable("FACTORY_FOLLOWUP_APPROVALS",state);
        try
        {
            var task = new Lifecycle(previous,old).Start(Guid.NewGuid(),previous.Scenarios.Single().Id);
            var origin = SeedFailure(db,task,old);
            TaskStore Open() => new(db,new Lifecycle(project,ci),()=>project,ciProfile:ci,previousProfile:old);
            var store = Open();
            store.BindProject(ci.Hash);
            var (_,checkpoint) = store.InspectRevision(task.Id);
            var command = new RevisionCommand(task.Id,checkpoint,new string('b',40),ci.Hash);
            var source = project.Snapshot();
            var nextOrigin = origin with { BaseSha=command.BaseSha, BaseTree=new string('c',40), SourceHash=source.Fingerprint(), PolicyHash=ci.Hash };
            void Refuse(RevisionCommand invalid)
            {
                try { store.ApproveRevision(invalid,nextOrigin,source); }
                catch (CommandConflict) { return; }
                throw new Exception("Revision accepted a stale approval.");
            }
            Refuse(command with { Checkpoint = new string('0',64) });
            Refuse(command with { BaseSha = new string('0',40) });
            Refuse(command with { PolicyHash = old.Hash });
            if (store.Next(remote:true).Id is not null || store.RevisionApplied(command) ||
                store.List().Single().Activity != Activity.Blocked)
                throw new Exception("Staging or a stale approval dispatched work.");

            store.ApproveRevision(command,nextOrigin,source);
            store = Open();
            store.BindProject(ci.Hash);
            var resumed = store.List().Single();
            if (!store.RevisionApplied(command) || resumed.Id != task.Id || resumed.OwnerId != task.OwnerId ||
                resumed.Phase != Phase.Planning || resumed.Activity != Activity.Queued ||
                !resumed.History.Take(task.History.Count).SequenceEqual(task.History))
                throw new Exception("Restart lost the approved round, task identity or earlier history.");
            var archive = db.Read(s => s.Text("SELECT archived_task FROM verification_revisions WHERE task_id=@id",("id",task.Id.ToString())));
            var archived = JsonSerializer.Deserialize<WorkItem>(archive!,Wire.Json)!;
            if (GateInput.Hash(archived.Receipts) != GateInput.Hash(task.Receipts) || archived.Candidate != task.Candidate)
                throw new Exception("The failed candidate or evidence was lost.");
            CheckDispatch(store,project,command);
            Console.WriteLine("Passed: blocked verification holds until approval, rejects stale inputs, preserves the failed round and admits the new paths after restart");
        }
        finally
        {
            Environment.SetEnvironmentVariable("FACTORY_FOLLOWUP_APPROVALS",oldApprovalDirectory);
            Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
        }
    }

    private static void CheckDispatch(TaskStore store, HostedRepositoryProject project, RevisionCommand approval)
    {
        List<WorkAction> actions = [];
        for (int i = 0; i < 12; i++)
        {
            if (store.Next(remote:true).Id is not { } id) continue;
            var scheduler = Guid.NewGuid().ToString();
            store.Submitted(id,scheduler);
            var claim = store.Claim(id,scheduler,Guid.NewGuid()) ?? throw new Exception("Fresh worker was denied.");
            var order = claim.Order;
            actions.Add(order.Action);
            order.Validate(project);
            if (order.Action == WorkAction.Implement)
            {
                var paths = order.Context!.Documents.Where(d => d.Part == SourcePart.Source).Select(d => d.Path);
                if (!actions.SequenceEqual([WorkAction.Plan,WorkAction.Ask,WorkAction.Implement]) ||
                    claim.WorkerMode != "codex-subscription" || order.Context.FollowUp?.BaseSha != approval.BaseSha ||
                    order.Context.FollowUp.PolicyHash != approval.PolicyHash ||
                    !paths.ToHashSet().SetEquals(project.Ci.Rules.RepositoryLayout!.Writable))
                    throw new Exception("Writer did not receive the approved base, policy and two-file scope.");
                return;
            }
            store.Receive(id,claim.Token,order.Execute(project));
        }
        throw new Exception("The approved task did not reach implementation.");
    }

    private static IssueOrigin SeedFailure(FactoryDatabase db, WorkItem task, CiProfile old)
    {
        var hosted = old.Rules.HostedGitHub!;
        var issue = hosted.Issue;
        var sourceHash = task.Source.Fingerprint();
        task.Phase = Phase.Verification;
        task.Activity = Activity.Blocked;
        task.Candidate = "body { color: orange; }";
        task.Repairs = old.Rules.Policy.MaxRepairs;
        var hash = Wire.Digest(task.Candidate);
        var assignment = new Assignment(Guid.NewGuid(),task.Phase,"Verify theme",hash,ContractGate.Version,task.Id,
            sourceHash,old.Rules.Policy.Version,old.Rules.Policy.RequiredChecks);
        task.Assignments.Add(assignment);
        var receipt = new Receipt(task.Id,assignment.RunId,task.Candidate,hash,sourceHash,
            [new("bounded-source",true,"bounded-source"),new("accent",false,"accent")],ContractGate.Version,assignment.PolicyVersion,ContractGate.VerificationRule);
        task.Receipts.Add(receipt);
        var input = GateInput.Capture(task) with { RunId=assignment.RunId, OperationId="previous-verifier" };
        GateEvaluation.Record(task,input,GateOutcome.Block,"The accent check failed after repair.",receipt:receipt);
        task.Record("Verifier","The accent check failed after repair.");
        var origin = new IssueOrigin("https://api.github.com",old.Rules.Settings.GetProperty("repositoryId").GetInt64(),
            old.Rules.Settings.GetProperty("repository").GetString()!,issue.Number,issue.Id,hosted.BaseBranch,
            new string('a',40),new string('b',40),sourceHash,old.Rules.RepositoryLayout!.Source,task.Scenario,issue.ScopeHash,old.Hash);
        string Encode<T>(T value) => JsonSerializer.Serialize(value,Wire.Json);
        db.Write(s =>
        {
            var id = task.Id.ToString();
            s.Execute("INSERT INTO controller_project VALUES(1,@profile)",("profile",old.Hash));
            s.Execute("INSERT INTO tasks VALUES(@id,'Blocked',1,@doc)",("id",id),("doc",Encode(task)));
            s.Execute("INSERT INTO gate_evaluations VALUES(@id,1,@gate)",("id",id),("gate",Encode(task.Gates.Single())));
            var observation = new IssueObservation(issue.Id,"Theme","Approved scope","2026-01-01T00:00:00Z","Eligible");
            s.Execute("INSERT INTO github_intake VALUES(@repo,@n,1,@observed,1,@id,@origin)",("repo",origin.RepoId),
                ("n",issue.Number),("observed",Encode(observation)),("id",id),("origin",Encode(origin)));
            // Finished operations retain their dispatch watches.
            s.Execute("INSERT INTO dispatch_outbox(id,task_id,run_id,request) VALUES('previous-verifier',@id,@run,'{}')",
                ("id",id),("run",assignment.RunId.ToString()));
            s.Execute("INSERT INTO dispatch_watch VALUES('previous-verifier',1)");
            return true;
        });
        return origin;
    }
}
