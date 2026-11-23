using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Factory;

public record ScopeRequest(long RepositoryId, long IssueId, string IssueHash, string BaseSha, string PolicyHash,
    Guid? Previous, string Goal, string[] Paths, string[] Checks,
    [property: System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
    Guid? OwnerMessage = null);
public record SavedIssueScope(Guid Id, int Number, ScopeRequest Request, bool ApprovedForExecution = false)
{
    public string Goal => Request.Goal;
    public string PolicyHash => Request.PolicyHash;
    public string BaseSha => Request.BaseSha;
}

public sealed partial class GitHubRepository
{
    internal async Task<JsonArray> OpenIssues(int page)
    {
        if (page is < 1 or > 10000) throw new CommandConflict("Invalid issue page.");
        await VerifyRepository();
        return await Call($"issues?state=open&sort=created&direction=desc&per_page=50&page={page}") as JsonArray
            ?? throw new GitHubFailure("GitHub returned an incomplete issue list.");
    }
}

public sealed partial class TaskStore
{
    private static SavedIssueScope? GetScope(Session s,long repository,int number) =>
        s.Text("SELECT document FROM github_desk_scopes WHERE repo_id=@r AND issue_number=@n", ("r",repository),("n",number))
        is { } raw ? JsonSerializer.Deserialize<SavedIssueScope>(raw, Wire.Json) : null;

    internal SavedIssueScope? ScopeFor(long repository,int number) => database.Read(s=>GetScope(s,repository,number));

    internal int[] ScopedTaskNumbers(long repository) => database.Read(s=>{
        using var cmd=s.Command("""
            SELECT a.issue_number FROM github_desk_scopes a JOIN github_intake i ON i.repo_id=a.repo_id AND i.issue_number=a.issue_number
            WHERE a.repo_id=@r AND i.task_id IS NOT NULL
            """,("r",repository));
        using var rows=cmd.ExecuteReader();
        var numbers=new List<int>();
        while(rows.Read()) numbers.Add(rows.GetInt32(0));
        return numbers.ToArray();
    });

    internal SavedIssueScope SaveScope(int number, ScopeRequest request, bool approvedForExecution) =>
        database.Write(s => {
            var planning = PlanningFor(s,request.RepositoryId,number);
            var latest = planning is null ? null : ScopeTranscript(s,planning.TaskId).LastOrDefault();
            if (latest?.Status == "Pending") throw new CommandConflict("Wait for the current owner reply before approving a scope.");
            if (request.OwnerMessage is { } ownerMessage)
            {
                if (planning is null || latest?.Id != ownerMessage || latest.Status != "Answered" || latest.Proposal is not { } proposal ||
                    planning.Pins.IssueId != request.IssueId || planning.Pins.IssueHash != request.IssueHash ||
                    planning.Pins.PolicyHash != request.PolicyHash || planning.Pins.BaseSha != request.BaseSha ||
                    proposal.Goal != request.Goal || !proposal.Paths.SequenceEqual(request.Paths) || !proposal.Checks.SequenceEqual(request.Checks))
                    throw new CommandConflict("The owner proposal changed. Reopen the issue before recording the request.");
            }
            var saved = SaveScope(s,number,request,approvedForExecution && planning is null);
            if (planning?.ExecutionApproval is not null)
                PersistPlanning(s,planning with { ExecutionApproval = null });
            return saved;
        });

    private static SavedIssueScope SaveScope(Session s, int number, ScopeRequest request, bool approvedForExecution)
    {
        var prior=GetScope(s,request.RepositoryId,number);
        if (prior is not null && Encode(prior.Request)==Encode(request) && prior.ApprovedForExecution==approvedForExecution) return prior;
        if (prior?.Id!=request.Previous || IssueStatus(s,request.RepositoryId,number).TaskId is not null)
            throw new CommandConflict("The issue or its scope changed. Reload the scope before editing it.");
        var saved=new SavedIssueScope(Guid.NewGuid(),number,request,approvedForExecution);
        s.Execute("""
            INSERT INTO github_desk_scopes(repo_id,issue_number,document) VALUES(@r,@n,@d)
            ON CONFLICT(repo_id,issue_number) DO UPDATE SET document=excluded.document
            """,("r",request.RepositoryId),("n",number),("d",Encode(saved)));
        return saved;
    }
}

public sealed partial class GitHubSync
{
    private bool CanApprove => config.OperatorStart && config.DeskScopeApprovals && project is HostedRepositoryProject && ci?.Rules.RepositoryLayout is not null && RoundApproval.Configured;
    private string[] InstalledPaths => ci!.Rules.RepositoryLayout!.Writable;
    private string[] InstalledChecks => project.Describe(project.Snapshot()).Requirements.Criteria.Select(c=>c.Text).ToArray();
    private bool PolicyAssigned(int number, long id, string hash) =>
        config.ScopePolicies?.Contains(new IssuePolicyBinding(number, id, hash, ci!.Hash)) == true;
    private bool MatchesInstalled(ScopeRequest r) => CanApprove && r.PolicyHash==ci!.Hash &&
        InstalledPaths.ToHashSet(StringComparer.Ordinal).SetEquals(r.Paths) && InstalledChecks.SequenceEqual(r.Checks);

    private ApprovedIssue? AssignedApproval(int number)
    {
        if (!CanApprove) return null;
        var origin=store.GitHubStatusFor(config.RepositoryId,number).Origin;
        var scope=store.ScopeFor(config.RepositoryId,number);
        return scope is not null && origin?.DeskApproval==scope.Id && origin.PolicyHash==ci!.Hash
            ? new(number,origin.IssueId,origin.ScopeHash,origin.Scenario) : null;
    }

    internal IEnumerable<int> PollNumbers() => config.Issues.Select(i=>i.Number)
        .Concat(CanApprove ? store.ScopedTaskNumbers(config.RepositoryId) : []).Distinct();

    public async Task<object> ListIssues(int page)
    {
        var batch=await repo.OpenIssues(page);
        var items=new List<object>();
        foreach(var issue in batch)
        {
            if (issue is null || issue["pull_request"] is not null || (string?)issue["state"]!="open") continue;
            if ((int?)issue["number"] is not > 0 || (long?)issue["id"] is not > 0 || (string?)issue["title"] is not { } title)
                throw new GitHubFailure("GitHub returned an incomplete issue.");
            var number=(int)issue["number"]!;
            var id=(long)issue["id"]!;
            var configured=config.Issues.SingleOrDefault(i=>i.Number==number && i.Id==id);
            var taskId=store.GitHubStatusFor(config.RepositoryId,number).TaskId;
            var reason=configured is null ? null : repo.Observe(issue,configured,null).Reason;
            items.Add(new {number,id,title,taskId,configured=configured is not null,reason,canReview=CanApprove && configured is null});
        }
        return new {items,nextPage=batch.Count==50 ? (int?)page+1 : null};
    }

    private async Task<(JsonNode Issue,string Hash,string Base)> ScopeInputs(int number)
    {
        if (!CanApprove || config.Issues.Any(i=>i.Number==number) || number<=0)
            throw new CommandConflict("Desk scope approval is unavailable for this issue.");
        var issue=await repo.Issue(number);
        var title=(string?)issue["title"] ?? "";
        var body=(string?)issue["body"] ?? "";
        if (issue["pull_request"] is not null || (int?)issue["number"]!=number || (long?)issue["id"] is not > 0 ||
            (string?)issue["state"]!="open" || string.IsNullOrWhiteSpace(title) || issue["labels"] is not JsonArray labels ||
            labels.Any(l=> ((string?)l?["name"])?.ToLowerInvariant() is "hold" or "factory:hold" or "duplicate" or "superseded" or "factory:covered"))
            throw new CommandConflict("Only an open issue without a hold can receive a scope.");
        return (issue,Wire.Digest(title+"\n"+body),await repo.BaseHead());
    }

    public async Task<object> ScopePreview(int number)
    {
        var (issue,hash,revision)=await ScopeInputs(number);
        var (saved,planning)=store.ScopePreviewState(config.RepositoryId,number);
        var assigned=PolicyAssigned(number,(long)issue["id"]!,hash);
        var current=assigned && saved is { ApprovedForExecution: true } && saved.Request.IssueId==(long)issue["id"]! && saved.Request.IssueHash==hash &&
            saved.BaseSha==revision && MatchesInstalled(saved.Request) && (planning is null || planning.ExecutionApproval==saved.Id);
        return new {repositoryId=config.RepositoryId,issueId=(long)issue["id"]!,issueHash=hash,baseSha=revision,policyHash=ci!.Hash,
            title=(string?)issue["title"],body=(string?)issue["body"],assigned,discussing=planning is not null,
            paths=assigned ? InstalledPaths : [],checks=assigned ? InstalledChecks : [],saved,canBegin=current};
    }

    public async Task<SavedIssueScope> ApproveScope(int number,ScopeRequest request)
    {
        var (issue,hash,revision)=await ScopeInputs(number);
        if (request.RepositoryId!=config.RepositoryId || request.IssueId!=(long)issue["id"]! || request.IssueHash!=hash ||
            request.BaseSha!=revision || request.PolicyHash!=ci!.Hash)
            throw new CommandConflict("The issue, base or project changed. Review the current scope.");
        if (string.IsNullOrWhiteSpace(request.Goal) || request.Goal.Length>2000 ||
            request.Paths is not {Length: <= 16} || request.Checks is not {Length: > 0 and <= 16} ||
            request.Paths.Any(p=>string.IsNullOrEmpty(p) || p.Length>240 || !p.Split('/').All(s=>Regex.IsMatch(s,@"\A[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\z"))) ||
            request.Paths.Distinct(StringComparer.OrdinalIgnoreCase).Count()!=request.Paths.Length ||
            request.Checks.Any(c=>string.IsNullOrWhiteSpace(c) || c.Length>2000) || request.Checks.Distinct().Count()!=request.Checks.Length)
            throw new CommandConflict("Enter a goal and up to 16 distinct checks. If files are specified, use individual repository paths.");
        return store.SaveScope(number,request,PolicyAssigned(number,request.IssueId,hash) && MatchesInstalled(request));
    }

    private async Task<Guid> StartScopedIssue(int number,IssueStart selection)
    {
        var scope=store.ScopeFor(config.RepositoryId,number);
        if (!CanApprove || scope is null || selection.ScopeApproval!=scope.Id || selection.RepositoryId!=config.RepositoryId ||
            scope.Request.IssueId!=selection.IssueId || scope.Request.IssueHash!=selection.ScopeHash)
            throw new CommandConflict("This issue needs an approved scope that matches the installed files and checks.");
        var prior=store.GitHubStatusFor(config.RepositoryId,number);
        if (prior.TaskId is { } existing && prior.Origin?.DeskApproval==scope.Id) return existing;
        var (issue,hash,revision)=await ScopeInputs(number);
        if (hash!=scope.Request.IssueHash || revision!=scope.BaseSha || (long)issue["id"]! != scope.Request.IssueId)
            throw new CommandConflict("The approved issue or base changed. Review and approve the scope again.");
        if (!scope.ApprovedForExecution || !store.PlanningAllowsStart(config.RepositoryId,number,scope.Id) || !PolicyAssigned(number,selection.IssueId,hash) || !MatchesInstalled(scope.Request))
            throw new CommandConflict("This issue needs matching files and automated checks assigned before it can begin.");
        var snapshot=await repo.Snapshot(revision,project,ci,followUp:true);
        var admission=new ApprovedIssue(number,selection.IssueId,hash,ci!.Rules.HostedGitHub!.Issue.Scenario);
        var origin=new IssueOrigin(config.ApiUrl,config.RepositoryId,config.Repository,number,admission.Id,config.BaseBranch,revision,snapshot.Tree,
            snapshot.Source.Fingerprint(),snapshot.Path,admission.Scenario,hash,ci.Hash,DeskApproval:scope.Id);
        var observed=await repo.ObserveIssue(number,admission,origin);
        if (observed.Reason!="Eligible" || await repo.BaseHead()!=revision) throw new CommandConflict("This issue or its base changed before start.");
        var status=store.ObserveIssue(config.RepositoryId,number,prior.Version,observed,origin,snapshot.Source,operatorStart:true,scope:scope);
        return status.TaskId is { } taskId && status.Origin?.DeskApproval==scope.Id ? taskId : throw new CommandConflict("Issue admission changed.");
    }
}
