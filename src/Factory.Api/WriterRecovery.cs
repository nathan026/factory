namespace Factory;

public record WriterRestart(Guid TaskId, string OperationId, int GateSequence, string PreviousBase, string NewBase, string PolicyHash);

public sealed partial class TaskStore
{
    private (WorkItem Task, RecoveryView Hold, IssueOrigin Origin) WriterCheckpoint(Session s, Guid taskId, long repository, int number)
    {
        var task=ReadTask(s,taskId);
        task.Gates=ReadGates(s,taskId);
        var status=IssueStatus(s,repository,number);
        var hold=Recovery(s,task);
        if (ciProfile?.Rules.HostedGitHub is null || task.Phase!=Phase.Implementation || task.Activity!=Activity.Blocked ||
            task.Candidate is not null || task.Receipts.Count!=0 || task.Repairs!=0 || task.Run is not null || task.FollowUp is null ||
            status.TaskId!=taskId || status.Origin is not { } origin || origin.PolicyHash!=ciProfile.Hash || origin.LocalJournal is not null ||
            task.Source.Fingerprint()!=origin.SourceHash || hold is not { Kind:"Outcome unknown", RetainedOutputHash:null } ||
            task.Gates[^1].Input.RunId is not { } run || hold.OperationId!=$"{run:N}.Implement" ||
            !CodexEvidence.ReleasedInterruptedWriter(taskId,run,ciProfile.Hash,origin.SourceHash) ||
            s.Text("SELECT task_id FROM github_publications WHERE task_id=@id",("id",taskId.ToString())) is not null ||
            s.Text("SELECT operation FROM discussion_jobs WHERE task_id=@id AND pending=1",("id",taskId.ToString())) is not null)
            throw new CommandConflict("Recovery needs a released, interrupted writer with no sealed result or publication.");
        return (task,hold,origin);
    }

    internal IssueOrigin? PendingRecoveryOrigin(Guid id) => database.Read(s => {
        var task = ReadTask(s,id);
        if (Recovery(s,task) is null) return null;
        var raw = s.Text("SELECT origin FROM github_intake WHERE task_id=@t", ("t",id.ToString()));
        return raw is null ? null : System.Text.Json.JsonSerializer.Deserialize<IssueOrigin>(raw,Wire.Json);
    });

    internal (IssueOrigin Origin, string Goal, RecoveryView Hold) InspectWriter(Guid taskId,long repository,int number) => database.Read(s=>{
        var (task,hold,origin)=WriterCheckpoint(s,taskId,repository,number);
        return (origin,task.Goal,hold);
    });

    internal void ReplanWriter(WriterRestart command, IssueOrigin updated, SourcePacket source) => database.Write(s=>{
        var (task,hold,old)=WriterCheckpoint(s,command.TaskId,updated.RepoId,updated.Number);
        if (hold.OperationId!=command.OperationId || hold.GateSequence!=command.GateSequence || command.PreviousBase!=old.BaseSha ||
            command.NewBase!=updated.BaseSha || command.PolicyHash!=ciProfile!.Hash ||
            updated!=(old with {BaseSha=command.NewBase,BaseTree=updated.BaseTree,SourceHash=source.Fingerprint()}) ||
            task.Source!=source)
            throw new CommandConflict("The recovery preview or admitted files changed. Review the task before resuming.");
        // Check the original gate and permanently retire the old dispatch before a fresh run can be claimed.
        var note=$"The native writer stopped and released its lease. No result was sealed. Approved base {command.NewBase}.";
        var recovered=Recover(s,task.Id,new(hold.OperationId,hold.GateSequence,note));
        if (old.BaseSha==updated.BaseSha) return true;
        var grant=new RoundApproval(Guid.NewGuid(),task.Id,task.Goal,updated.BaseSha,updated.SourceHash,ciProfile.Hash);
        grant.Persist();
        var next=lifecycle.Start(task.Id,task.Scenario,task.OpeningMessage,source,recovered,grant);
        next.Record("Operator",$"Restarted planning at {updated.BaseSha}. All approved file contents match the previous base {old.BaseSha}.");
        s.Execute("UPDATE github_intake SET origin=@o,version=version+1 WHERE task_id=@t",("o",Encode(updated)),("t",task.Id.ToString()));
        Save(s,next);
        return true;
    });
}

public sealed partial class GitHubSync
{
    public async Task CheckRecoveryBase(Guid id)
    {
        if (ci?.Rules.HostedGitHub is not null && store.PendingRecoveryOrigin(id) is { } origin &&
            await repo.BaseHead()!=origin.BaseSha)
            throw new CommandConflict("Main moved after this task started. Review the writer recovery on the current base.");
    }

    public async Task<object> WriterRecovery(int number,Guid taskId)
    {
        var (origin,goal,hold)=store.InspectWriter(taskId,config.RepositoryId,number);
        var approval=Approval(number);
        if (origin.Repository!=config.Repository || origin.Base!=config.BaseBranch || origin.ApiUrl!=config.ApiUrl ||
            (await repo.ObserveIssue(number,approval,origin)).Reason!="Eligible")
            throw new CommandConflict("The original issue must still be open and eligible.");
        var head=await repo.BaseHead();
        return new {command=new WriterRestart(taskId,hold.OperationId,hold.GateSequence,origin.BaseSha,head,ci!.Hash),goal,
            paths=ci.Rules.RepositoryLayout!.Writable};
    }

    public async Task<Guid> RestartWriter(int number,WriterRestart command)
    {
        var (origin,_,hold)=store.InspectWriter(command.TaskId,config.RepositoryId,number);
        var approved=Approval(number);
        if (origin.Repository!=config.Repository || origin.ApiUrl!=config.ApiUrl || origin.Base!=config.BaseBranch ||
            hold.OperationId!=command.OperationId || hold.GateSequence!=command.GateSequence || command.PolicyHash!=ci!.Hash ||
            command.PreviousBase!=origin.BaseSha || command.NewBase!=await repo.BaseHead())
            throw new CommandConflict("The issue or base changed. Load a fresh recovery preview.");
        var snapshot=await repo.Snapshot(command.NewBase,project,ci,followUp:true);
        if ((await repo.ObserveIssue(number,approved,origin)).Reason!="Eligible" || command.NewBase!=await repo.BaseHead())
            throw new CommandConflict("The issue or base changed before recovery.");
        store.ReplanWriter(command,origin with {BaseSha=command.NewBase, BaseTree=snapshot.Tree,SourceHash=snapshot.Source.Fingerprint()},snapshot.Source);
        return command.TaskId;
    }
}
