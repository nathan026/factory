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
        var store = new TaskStore(db, new Lifecycle(project, profile), () => project, ciProfile: profile);
        store.BindProject(profile.Hash);
        using var doc = JsonDocument.Parse(Console.In.ReadToEnd());
        var input = doc.RootElement;
        var approved = profile.Rules.HostedGitHub!.Issue;
        var config = new GitHubProject("fixture/checkout", 723, "main", [approved], url,
            OperatorStart: true, DeskScopeApprovals: input.GetProperty("enable").GetBoolean());
        using var repo = new GitHubRepository(config, project.Policy.Version, profile.Rules.RepositoryLayout);
        var sync = new GitHubSync(config, repo, store, project, profile);
        var number = input.GetProperty("number").GetInt32();
        object? result;
        try
        {
            result = input.GetProperty("command").GetString() switch
            {
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
}
