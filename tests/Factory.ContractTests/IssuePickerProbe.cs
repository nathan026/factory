using System.Text.Json;
using Factory;

// The caller owns the loopback Git fixture and the disposable SQLite directory.
// Each invocation opens the controller state afresh, exercising persistence across restarts.
internal static class IssuePickerProbe
{
    public static async Task Run(string url, string directory)
    {
        if (new Uri(url) is not { IsLoopback: true, Scheme: "http" } || !Path.IsPathFullyQualified(directory))
            throw new InvalidDataException("A loopback Git fixture and an absolute state directory are required.");
        var profile = CiProfile.Load(directory, "picker/v1");
        var project = new HostedRepositoryProject(directory, profile);
        var db = new FactoryDatabase(directory, null);
        var store = new TaskStore(db, new Lifecycle(project, profile), () => project, ciProfile: profile, liveOwner:true);
        store.BindProject(profile.Hash);
        using var doc = JsonDocument.Parse(Console.In.ReadToEnd());
        var input = doc.RootElement;
        var approved = profile.Rules.HostedGitHub!.Issue;
        var bindings = input.GetProperty("bindings").Deserialize<IssuePolicyBinding[]>(Wire.Json);
        var config = new GitHubProject("fixture/checkout", 723, "main", [approved], url,
            OperatorStart: true, DeskScopeApprovals: input.GetProperty("enable").GetBoolean(), ScopePolicies: bindings);
        using var repo = new GitHubRepository(config, project.Policy.Version, profile.Rules.RepositoryLayout);
        var sync = new GitHubSync(config, repo, store, project, profile);
        var number = input.GetProperty("number").GetInt32();
        object? result;
        try
        {
            result = input.GetProperty("command").GetString() switch
            {
                "discussion" => await sync.IssueDiscussion(number,false),
                "transcript" => await sync.IssueDiscussion(number,true),
                "message" => await sync.PostIssueDiscussion(number,input.GetProperty("body").Deserialize<ScopeMessage>(Wire.Json)!),
                "ownerClaim" => ClaimOwner(store),
                "planningClaim" => ClaimPlan(store,project),
                "receive" => store.Receive(input.GetProperty("body").GetProperty("id").GetString()!,
                    input.GetProperty("body").GetProperty("token").GetString()!,input.GetProperty("body").GetProperty("result")),
                "queue" => store.Queue(),
                "list" => await sync.ListIssues(number),
                "preview" => await sync.ScopePreview(number),
                "approve" => await sync.ApproveScope(number, input.GetProperty("body").Deserialize<ScopeRequest>(Wire.Json)!),
                "start" => await sync.Start(number, input.GetProperty("body").Deserialize<IssueStart>(Wire.Json)!),
                "poll" => await sync.Poll(number, automatic: true),
                "pollNumbers" => sync.PollNumbers().ToArray(),
                "tasks" => store.List(),
                _ => throw new InvalidDataException("Unsupported fixture command")
            };
        }
        catch (CommandConflict error) { result = new { conflict = error.Message }; }
        Console.WriteLine(JsonSerializer.Serialize(result, Wire.Json));
    }
    private static WorkOrder ClaimPlan(TaskStore store, IControlledProject project)
    {
        var id=store.Next(remote:true).Id ?? throw new Exception("Missing planning dispatch.");
        var claim=store.Claim(id,"fixture-"+id,Guid.NewGuid()) ?? throw new Exception("Missing planning claim.");
        if (claim.Order.Action != WorkAction.Plan) throw new Exception("Unexpected first coding action.");
        claim.Order.Validate(project);
        return claim.Order;
    }

    private static object ClaimOwner(TaskStore store)
    {
        var id=store.Next(remote:true).Id ?? throw new Exception("Missing owner discussion dispatch.");
        var session=Guid.NewGuid();
        var claim=store.Claim(id,"fixture-"+id,session) ?? throw new Exception("Missing owner claim.");
        var question=claim.Order.Discussion ?? throw new Exception("Coding started before scope admission.");
        return new {id,session,claim.Token,claim.WorkerMode,question,requestHash=GateInput.Hash(question)};
    }
}
