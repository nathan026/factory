using System.Text;
using System.Text.Json;

namespace Factory;

public record ScopeMessage(Guid Id, string Text, long RepositoryId, long IssueId, string IssueHash, string BaseSha,
    string PolicyHash, Guid? Previous);
internal record ScopePins(long RepositoryId, int Number, long IssueId, string IssueHash, string BaseSha, string PolicyHash,
    string Title, string Body, string[] Paths, string[] Checks)
{
    public bool Matches(ScopeMessage m) => RepositoryId == m.RepositoryId && IssueId == m.IssueId &&
        IssueHash == m.IssueHash && BaseSha == m.BaseSha && PolicyHash == m.PolicyHash;
    public bool SameRevision(ScopePins other) => RepositoryId == other.RepositoryId && Number == other.Number &&
        IssueId == other.IssueId && IssueHash == other.IssueHash && BaseSha == other.BaseSha && PolicyHash == other.PolicyHash &&
        Paths.SequenceEqual(other.Paths) && Checks.SequenceEqual(other.Checks);
}
internal record ScopeConversation(Guid TaskId, Guid OwnerId, ScopePins Pins, string Checkpoint, Guid LastMessage,
    Guid? PreviousApproval, Guid? ExecutionApproval);

public sealed partial class GitHubSync
{
    private async Task<ScopePins> DiscussionPins(int number)
    {
        var (issue,hash,revision) = await ScopeInputs(number);
        var id = (long)issue["id"]!;
        var assigned = PolicyAssigned(number,id,hash);
        return new(config.RepositoryId,number,id,hash,revision,ci!.Hash,(string)issue["title"]!,
            (string?)issue["body"] ?? "",assigned ? InstalledPaths : [],assigned ? InstalledChecks : []);
    }

    public async Task<object> IssueDiscussion(int number,bool poll)
    {
        if (!CanApprove || number <= 0 || config.Issues.Any(i => i.Number == number))
            throw new CommandConflict("Desk issue discussions are unavailable for this issue.");
        // Poll the local transcript. Refresh canonical pins on open and on every operator command.
        var pins = poll ? null : await DiscussionPins(number);
        return store.ScopeDiscussionView(config.RepositoryId,number,pins);
    }

    public async Task<object> PostIssueDiscussion(int number,ScopeMessage message)
    {
        if (message.Id == Guid.Empty || string.IsNullOrWhiteSpace(message.Text) || message.Text.Length > 4000)
            throw new CommandConflict("Enter a message of 1 to 4,000 characters.");
        var pins = await DiscussionPins(number);
        if (!pins.Matches(message)) throw new CommandConflict("The issue or repository revision changed. Reopen the discussion.");
        if (!store.ScopeOwnerAvailable) throw new CommandConflict("The subscription-backed lifecycle owner is unavailable on this deployment.");
        var files = await repo.ScopeSources(pins.BaseSha,project);
        return store.AddScopeMessage(pins,message,files);
    }
}

public sealed partial class TaskStore
{
    internal bool ScopeOwnerAvailable => liveOwner;
    internal (SavedIssueScope?,ScopeConversation?) ScopePreviewState(long repo,int number) =>
        database.Read(s => (GetScope(s,repo,number),PlanningFor(s,repo,number)));
    internal bool PlanningAllowsStart(long repo,int number,Guid approval) => database.Read(s =>
        PlanningFor(s,repo,number) is not { } p || p.ExecutionApproval == approval);
    private static ScopeConversation? DecodePlanning(string? raw) =>
        raw is null ? null : JsonSerializer.Deserialize<ScopeConversation>(raw,Wire.Json);
    private static ScopeConversation? PlanningFor(Session s,long repo,int number) => DecodePlanning(
        s.Text("SELECT document FROM github_scope_discussions WHERE repo_id=@r AND issue_number=@n",("r",repo),("n",number)));
    private static ScopeConversation? PlanningFor(Session s,Guid taskId) => DecodePlanning(
        s.Text("SELECT document FROM github_scope_discussions WHERE task_id=@t",("t",taskId.ToString())));
    private static void PersistPlanning(Session s,ScopeConversation planning)
    {
        s.Execute("""
            INSERT INTO github_scope_discussions(repo_id,issue_number,task_id,document) VALUES(@r,@n,@t,@d)
            ON CONFLICT(repo_id,issue_number) DO UPDATE SET document=excluded.document
            """,("r",planning.Pins.RepositoryId),("n",planning.Pins.Number),("t",planning.TaskId.ToString()),("d",Encode(planning)));
    }
    private static List<Discussion> ScopeTranscript(Session s,Guid id)
    {
        using var cmd = s.Command("SELECT exchange FROM task_discussion WHERE task_id=@t ORDER BY seq",("t",id.ToString()));
        using var rows = cmd.ExecuteReader();
        var entries = new List<Discussion>();
        while(rows.Read()) entries.Add(JsonSerializer.Deserialize<Discussion>(rows.GetString(0),Wire.Json)!);
        return entries;
    }

    internal object ScopeDiscussionView(long repo,int number,ScopePins? fresh) => database.Read(s =>
        ScopeDiscussionView(s,repo,number,fresh));

    private object ScopeDiscussionView(Session s,long repo,int number,ScopePins? fresh)
    {
        if (IssueStatus(s,repo,number).TaskId is not null) throw new CommandConflict("This issue has started. Open its existing task.");
        var planning = PlanningFor(s,repo,number);
        var pins = fresh ?? planning?.Pins ?? throw new CommandConflict("Reopen the issue to start a scope discussion.");
        List<Discussion> messages = planning is null ? [] : ScopeTranscript(s,planning.TaskId);
        var saved = GetScope(s,repo,number);
        bool stale = planning is not null && !planning.Pins.SameRevision(pins);
        bool ready = !stale && planning?.ExecutionApproval is { } approval && approval == saved?.Id;
        bool requestRecorded = !stale && saved?.Request.OwnerMessage is { } recorded &&
            messages.LastOrDefault() is { Status: "Answered", Proposal: not null } last && last.Id == recorded &&
            saved.Request.IssueId == pins.IssueId && saved.Request.IssueHash == pins.IssueHash &&
            saved.BaseSha == pins.BaseSha && saved.PolicyHash == pins.PolicyHash;
        return new { requestRecorded,taskId=planning?.TaskId,ownerId=planning?.OwnerId,pins.RepositoryId,pins.IssueId,pins.IssueHash,
            pins.BaseSha,pins.PolicyHash,pins.Title,pins.Body,messages,proposal=stale ? null : messages.LastOrDefault()?.Proposal,
            savedScopeId=saved?.Id,canBegin=ready,scopeApproval=ready ? saved!.Id : (Guid?)null,assigned=pins.Paths.Length>0,ownerAvailable=liveOwner,stale };
    }

    private WorkItem PlanningTask(ScopePins pins)
    {
        var issue = pins.Title + "\n\n" + pins.Body;
        var source = new SourcePacket(issue,"","");
        var task = new WorkItem { Id=Guid.NewGuid(), Scenario="issue-scope-discussion", Goal=pins.Title, Source=source,
            Activity=Activity.Waiting, Policy=ciProfile!.Rules.Policy, OwnerRuntime="codex-subscription", OpenedAt=DateTimeOffset.UtcNow, OpeningMessage=issue };
        var brief = new ProjectBrief(new([new("issue-scope","Agree on the scope before starting work.")],[issue],
            ["This discussion grants no coding permissions."]),[SourceDocument.Capture(SourcePart.Source,"github/issue.txt",source)]);
        task.Memory = OwnerMemory.Initialize(task,brief,legacy:false);
        task.Record("Lifecycle owner",$"Opened the scope discussion for issue #{pins.Number} at {pins.BaseSha}.");
        return task;
    }

    internal object AddScopeMessage(ScopePins pins,ScopeMessage message,ExpertSource[] files) => database.Write(s => {
        if (!ScopeOwnerAvailable || IssueStatus(s,pins.RepositoryId,pins.Number).TaskId is not null)
            throw new CommandConflict("The owner is unavailable or this issue has already started.");
        var planning = PlanningFor(s,pins.RepositoryId,pins.Number);
        if (planning is not null && planning.Pins.IssueId != pins.IssueId)
            throw new CommandConflict("The issue identity changed. Inspect the original discussion.");
        var task = planning is null ? PlanningTask(pins) : ReadTask(s,planning.TaskId);
        var entries = ScopeTranscript(s,task.Id);
        var prior = entries.Find(e => e.Id == message.Id);
        if (prior is not null)
        {
            if (prior.Text != message.Text.Trim()) throw new CommandConflict("Message ID already has different text.");
            return ScopeDiscussionView(s,pins.RepositoryId,pins.Number,pins);
        }
        if (entries.LastOrDefault()?.Id != message.Previous || entries.LastOrDefault()?.Status == "Pending")
            throw new CommandConflict("The owner conversation has changed or a reply is pending. Read the latest exchange.");
        if (planning is null) Save(s,task);
        var previous = GetScope(s,pins.RepositoryId,pins.Number);
        var context = Encode(new { issue = new {pins.Number,pins.Title,pins.Body,pins.BaseSha}, allowedFiles=pins.Paths,
            automatedChecks=pins.Checks, savedObjective=previous?.Request.Goal, savedFiles=previous?.Request.Paths, savedChecks=previous?.Request.Checks,
            transcript=entries.Select(e=>new {e.Text,e.Reply,e.Status,e.Proposal}) });
        if (Encoding.UTF8.GetByteCount(context) > 48*1024)
            throw new CommandConflict("The issue and conversation exceed the owner context limit. Shorten the GitHub issue or finish the scope in the editor.");
        var checkpoint = GateInput.Hash(new { taskId=task.Id,task.OwnerId,pins,messageId=message.Id,context });
        var updated = new ScopeConversation(task.Id,task.OwnerId,pins,checkpoint,message.Id,previous?.Id,null);
        PersistPlanning(s,updated);
        var exchange = QueueScopeDiscussion(s,task.Id,task.OwnerId,pins.BaseSha,message.Id,message.Text.Trim(),checkpoint,context,files);
        s.Execute("INSERT INTO task_discussion(task_id,message_id,seq,exchange) VALUES(@t,@m,@seq,@doc)",
            ("t",task.Id.ToString()),("m",exchange.Id.ToString()),("seq",entries.Count+1),("doc",Encode(exchange)));
        return ScopeDiscussionView(s,pins.RepositoryId,pins.Number,pins);
    });

    private bool ScopeDiscussionCurrent(Session s,OwnerQuestion q)
    {
        var p = PlanningFor(s,q.TaskId);
        return p is not null && p.OwnerId == q.OwnerId && p.LastMessage == q.MessageId && p.Checkpoint == q.BriefHash &&
            p.Pins.BaseSha == q.ScopePlanning?.BaseSha && ciProfile?.Hash == p.Pins.PolicyHash;
    }

    // The native owner result is already validated and bound to this exact pending operation.
    // The controller independently checks its proposed authority against the installed assignment.
    private static void SaveOwnerProposal(Session s,OwnerQuestion q,ScopeProposal proposal)
    {
        var p = PlanningFor(s,q.TaskId)!;
        var pins = p.Pins;
        if (pins.Paths.Length == 0 || !pins.Paths.ToHashSet(StringComparer.Ordinal).SetEquals(proposal.Paths) ||
            pins.Paths.Length != proposal.Paths.Length || !pins.Checks.SequenceEqual(proposal.Checks) ||
            GetScope(s,pins.RepositoryId,pins.Number)?.Id != p.PreviousApproval)
            return;
        var request = new ScopeRequest(pins.RepositoryId,pins.IssueId,pins.IssueHash,pins.BaseSha,pins.PolicyHash,
            p.PreviousApproval,proposal.Goal,proposal.Paths,proposal.Checks);
        var saved = SaveScope(s,pins.Number,request,true);
        PersistPlanning(s,p with {ExecutionApproval=saved.Id});
    }
}
