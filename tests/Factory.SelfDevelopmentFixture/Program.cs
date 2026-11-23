using System.Text.Json;
using Factory;

// The test driver supplies only a loopback fixture. This executable has no release command.
var request = JsonSerializer.Deserialize<Publication>(Console.In.ReadToEnd(), Wire.Json)!;
var origin = request.Origin;
var manifest = File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "factory-source-policy.json")).ReplaceLineEndings("\n");
var policy = JsonSerializer.Deserialize<Profile>(manifest, Wire.Json)!;
var endpoint = new Uri(origin.ApiUrl);
if (endpoint.Scheme != "http" || endpoint.Host != "127.0.0.1" || endpoint.AbsolutePath != "/" ||
    endpoint.UserInfo != "" || endpoint.Query != "" || endpoint.Fragment != "" ||
    origin.Repository != policy.Repository || origin.RepoId != policy.RepositoryId || origin.Path != policy.Path ||
    origin.Number != policy.Issue || origin.IssueId != policy.IssueId || origin.BaseSha != policy.Base ||
    origin.ScopeHash != Wire.Digest(policy.Title + "\n" + policy.Body) || origin.PolicyHash != Wire.Digest(manifest) ||
    origin.Base != "main" || origin.Scenario != policy.Version)
    throw new InvalidDataException("Unexpected self-development fixture or policy.");
using var repo = new GitHubRepository(new(origin.Repository, origin.RepoId, origin.Base, [], origin.ApiUrl));
var approval = new ApprovedIssue(origin.Number, origin.IssueId, origin.ScopeHash, origin.Scenario);
async Task RequireEligibility()
{
    var issue = await repo.ObserveIssue(origin.Number, approval, origin);
    if (issue.Reason != "Eligible" || await repo.BaseHead() != origin.BaseSha)
        throw new InvalidDataException("Issue or base differs from admission.");
}
await RequireEligibility();
if (args is ["--admit"])
{
    if (request.Intent is not null) throw new InvalidDataException("Admission only reads the approved issue and base.");
    Console.WriteLine("true");
    return;
}
var intent = request.Intent ?? throw new InvalidDataException("Missing reviewed candidate.");
if (args.Length != 0 || intent.CandidateHash != Wire.Digest(intent.Candidate) ||
    intent.Branch != "factory/" + intent.TaskId.ToString() || intent.Reviewer == intent.Verifier ||
    intent.Ci is null || intent.Ci.ExecutionId != intent.Verifier || intent.Ci.PolicyHash != origin.PolicyHash)
    throw new InvalidDataException("Unexpected candidate or evidence.");
var head = await repo.BuildCommit(origin, intent);
await RequireEligibility();
await repo.EnsureBranch(intent, head, () => true);
await RequireEligibility();
var receipt = await repo.FindDraft(intent, head) ?? await repo.CreateDraft(origin, intent, head);
Console.WriteLine(JsonSerializer.Serialize(receipt, Wire.Json));

record Publication(IssueOrigin Origin, DraftIntent? Intent);
record Profile(string Version, string Repository, long RepositoryId, int Issue, long IssueId, string Base,
    string Path, string Title, string Body);
