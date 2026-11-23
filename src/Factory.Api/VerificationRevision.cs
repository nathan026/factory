namespace Factory;

public record RevisionCommand(Guid TaskId, string Checkpoint, string BaseSha, string PolicyHash);
public record RevisionPreview(RevisionCommand Approval, string Goal, string[] Paths, Criterion[] Criteria);

public sealed partial class TaskStore
{
    private CiProfile? ProfileFor(WorkItem task) =>
        previousProfile is not null && previousProfile.Rules.Policy.Version == task.Policy?.Version ? previousProfile : ciProfile;

    // A controller has one project binding. A different project can use this state only
    // for this explicit transition from a single, quiescent, failed verification.
    private (WorkItem Task, IssueOrigin Origin, string Checkpoint) RevisionCheckpoint(Session s, Guid id)
    {
        var task = ReadTask(s, id);
        var idText = id.ToString();
        task.Gates = ReadGates(s, id);
        var gate = task.Gates.LastOrDefault();
        var receipt = task.Receipts.LastOrDefault();
        var hosted = ciProfile?.Rules.HostedGitHub;
        var previous = previousProfile;
        if (hosted is null || previous is null || previous.Rules.HostedGitHub != hosted ||
            ciProfile!.Rules.Revision?.PreviousHash != previous.Hash || task.Policy?.Version != previous.Rules.Policy.Version ||
            s.Text("SELECT profile FROM controller_project WHERE id=1") != previous.Hash ||
            s.Text("SELECT id FROM tasks WHERE id<>@id LIMIT 1", ("id", idText)) is not null ||
            task.Activity != Activity.Blocked || task.Phase != Phase.Verification || task.Run is not null ||
            gate is not { Outcome: GateOutcome.Block, Input.Phase: Phase.Verification } ||
            receipt is not { Passed: false } || gate.ReceiptHash != GateInput.Hash(receipt) ||
            gate.Input.RunId != receipt.RunId || task.RejectedReceipt?.Index == task.Receipts.Count - 1 ||
            ContractGate.ReceiptError(task, receipt) is not null || Recovery(s, task) is not null ||
            s.Text("SELECT task_id FROM github_publications WHERE task_id=@id", ("id", idText)) is not null ||
            s.Text("SELECT operation FROM discussion_jobs WHERE task_id=@id AND pending=1", ("id", idText)) is not null ||
            s.Text("SELECT h.id FROM dispatch_holds h JOIN dispatch_outbox o ON o.id=h.id WHERE o.task_id=@id AND h.resolution IS NULL", ("id", idText)) is not null)
            throw new CommandConflict("Only a settled verification failure on the approved previous policy can be revised.");

        var settings = ciProfile.Rules.Settings;
        var status = IssueStatus(s, settings.GetProperty("repositoryId").GetInt64(), hosted.Issue.Number);
        if (status.TaskId != id || status.Origin is not { } origin || origin.PolicyHash != previous.Hash ||
            origin.RepoId != settings.GetProperty("repositoryId").GetInt64() || origin.Number != hosted.Issue.Number ||
            origin.Repository != settings.GetProperty("repository").GetString() || origin.IssueId != hosted.Issue.Id ||
            origin.ScopeHash != hosted.Issue.ScopeHash || origin.Scenario != hosted.Issue.Scenario ||
            origin.Base != hosted.BaseBranch || origin.SourceHash != task.Source.Fingerprint() ||
            gate.Input.TaskId != id || gate.Input.ProfileHash != GateInput.Hash(task.Policy) ||
            gate.Input.SourceHash != origin.SourceHash || gate.Input.CandidateHash != receipt.CandidateHash)
            throw new CommandConflict("The recorded issue or policy differs from the installed revision.");
        return (task, origin, GateInput.Hash(gate));
    }

    internal (IssueOrigin Origin, string Checkpoint) InspectRevision(Guid taskId) => database.Read(s =>
    {
        var state = RevisionCheckpoint(s, taskId);
        return (state.Origin, state.Checkpoint);
    });

    private static object[] ArchivedVerification(Session s, Guid taskId)
    {
        using var command = s.Command("SELECT archived_task FROM verification_revisions WHERE task_id=@t ORDER BY ci_total", ("t",taskId.ToString()));
        using var rows = command.ExecuteReader();
        var rounds = new List<object>();
        while (rows.Read())
        {
            var prior = Decode(rows.GetString(0));
            rounds.Add(new { policy = prior.Policy!.Version, prior.Goal, attempts = prior.Receipts.Select(r => new {
                r.CandidateHash, failed = r.Checks.Where(c => !c.Passed).Select(c => c.Id) }) });
        }
        return rounds.ToArray();
    }

    internal bool RevisionApplied(RevisionCommand command) => database.Read(s =>
        s.Text("SELECT approval_id FROM verification_revisions WHERE task_id=@t AND checkpoint=@c AND base_sha=@b AND policy_hash=@p",
            ("t", command.TaskId.ToString()), ("c", command.Checkpoint), ("b", command.BaseSha), ("p", command.PolicyHash)) is not null);

    internal void ApproveRevision(RevisionCommand approval, IssueOrigin origin, SourcePacket source) => database.Write(s =>
    {
        var (task, old, checkpoint) = RevisionCheckpoint(s, approval.TaskId);
        if (checkpoint != approval.Checkpoint || ciProfile!.Hash != approval.PolicyHash || origin != (old with
            { BaseSha = approval.BaseSha, BaseTree = origin.BaseTree, Path = ciProfile.Rules.RepositoryLayout!.Source,
              PolicyHash = ciProfile.Hash, SourceHash = source.Fingerprint() }))
            throw new CommandConflict("This preview is stale. Review the current task, base and rules.");

        var grant = new RoundApproval(Guid.NewGuid(), task.Id, ciProfile.Rules.Goal, approval.BaseSha, source.Fingerprint(), ciProfile.Hash);
        s.Execute("""
            INSERT INTO verification_revisions(task_id,checkpoint,approval_id,archived_task,origin,base_sha,policy_hash,ci_total)
            VALUES(@t,@c,@a,@doc,@o,@b,@p,(SELECT COUNT(*) FROM ci_runs WHERE task_id=@t))
            """, ("t",task.Id.ToString()), ("c",checkpoint), ("a",grant.Id.ToString()), ("doc",Encode(task)), ("o",Encode(old)),
            ("b",approval.BaseSha), ("p",ciProfile.Hash));
        grant.Persist();
        var next = lifecycle.Start(task.Id, task.Scenario, task.OpeningMessage, source, task, grant);
        next.Record("Operator", $"Approved revised files and checks under {ciProfile.Rules.Policy.Version} at {approval.BaseSha}. The failed round is archived.");
        if (s.Execute("UPDATE controller_project SET profile=@new WHERE id=1 AND profile=@old", ("old",old.PolicyHash!), ("new",ciProfile.Hash)) != 1)
            throw new CommandConflict("The controller binding changed.");
        s.Execute("UPDATE github_intake SET origin=@o,version=version+1 WHERE task_id=@t", ("o",Encode(origin)), ("t",task.Id.ToString()));
        Save(s,next);
        return true;
    });
}

public sealed partial class GitHubSync
{
    public async Task<RevisionPreview> PreviewRevision(int number, Guid taskId)
    {
        var approval = Approval(number);
        var (origin, checkpoint) = store.InspectRevision(taskId);
        if (ci?.Rules.Revision is not { } revision || !RoundApproval.Configured || origin.ApiUrl != config.ApiUrl ||
            origin.Number != number || (await repo.ObserveIssue(number, approval, origin)).Reason != "Eligible")
            throw new CommandConflict("The issue must be open and eligible for this revision.");
        return new(new(taskId,checkpoint,await repo.BaseHead(),ci.Hash),ci.Rules.Goal,ci.Rules.RepositoryLayout!.Writable,revision.Criteria);
    }

    public async Task<Guid> ResumeRevision(int number, RevisionCommand command)
    {
        if (ci?.Rules.HostedGitHub?.Issue.Number != number || command.PolicyHash != ci.Hash)
            throw new CommandConflict("The proposed issue or project policy changed.");
        if (store.RevisionApplied(command)) return command.TaskId;
        var (origin, _) = store.InspectRevision(command.TaskId);
        var preview = await PreviewRevision(number,command.TaskId);
        if (preview.Approval != command) throw new CommandConflict("The preview changed. Review the new base and rules.");
        var snapshot = await repo.Snapshot(command.BaseSha,project,ci,followUp:true);
        if ((await PreviewRevision(number,command.TaskId)).Approval != command)
            throw new CommandConflict("The issue changed during approval.");
        store.ApproveRevision(command,origin with { BaseSha=command.BaseSha, BaseTree=snapshot.Tree, Path=snapshot.Path,
            SourceHash=snapshot.Source.Fingerprint(), PolicyHash=ci.Hash },snapshot.Source);
        return command.TaskId;
    }
}
