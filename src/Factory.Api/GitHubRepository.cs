using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Factory;

public record ApprovedIssue(int Number, long Id, string ScopeHash, string Scenario);
public record HostedGitHubApproval(string BaseBranch, ApprovedIssue Issue);
public record GitHubProject(string Repository, long RepositoryId, string BaseBranch, ApprovedIssue[] Issues,
    string ApiUrl = "https://api.github.com", bool LocalAtomicDraft = false, bool OperatorStart = false)
{
    // Set only after the controller verifies the installed policy against its intake file.
    internal ApprovedIssue? HostedIssue { get; private init; }

    public static GitHubProject Load(string path, IControlledProject project)
    {
        var config = JsonSerializer.Deserialize<GitHubProject>(File.ReadAllText(path), Wire.ResultJson)!;
        if (config.RepositoryId <= 0 || !Regex.IsMatch(config.Repository, @"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
            || config.Repository.Split('/').Any(p => p is "." or "..")
            || !Regex.IsMatch(config.BaseBranch, @"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")
            || config.Issues is not { Length: > 0 } || config.Issues.Select(i => i.Number).Distinct().Count() != config.Issues.Length
            || config.Issues.Any(i => i.Id <= 0 || i.Number <= 0 || !Regex.IsMatch(i.ScopeHash, "^[0-9a-f]{64}$") || !project.Supports(i.Scenario)))
            throw new InvalidDataException("Invalid GitHub repository or issue approval.");
        var url = new Uri(config.ApiUrl);
        if (url != new Uri("https://api.github.com") && !(url.Scheme == "http" && IPAddress.TryParse(url.Host, out var ip)
            && IPAddress.IsLoopback(ip) && url.AbsolutePath == "/" && url.Query == "" && url.Fragment == "" && url.UserInfo == ""))
            throw new InvalidDataException("Use the GitHub API or a loopback qualification endpoint.");
        if (config.LocalAtomicDraft && !url.IsLoopback) throw new InvalidDataException("Atomic Drafts require the local Git provider.");
        var ci = project.Ci;
        if (ci?.Rules.HostedGitHub is not null && !ci.RequiresCodex)
            throw new InvalidDataException("Hosted factory admission requires live Codex workers.");
        if (project is Factory.Fixtures.FactorySummaryProject || ci?.Rules.RepositoryLayout is not null)
        {
            var rules = ci!.Rules.Settings;
            if (config.Repository != rules.GetProperty("repository").GetString() ||
                config.RepositoryId != rules.GetProperty("repositoryId").GetInt64())
                throw new InvalidDataException("Factory source differs from the installed repository identity.");
            if (ci.Rules.HostedGitHub is { } hosted)
            {
                if (config.ApiUrl != "https://api.github.com" || config.BaseBranch != hosted.BaseBranch ||
                    config.Issues is not [var issue] || issue != hosted.Issue ||
                    !Regex.IsMatch(rules.GetProperty("base").GetString() ?? "", @"\A[0-9a-f]{40}\z"))
                    throw new InvalidDataException("Hosted factory destination or issue differs from the installed approval.");
                return config with { HostedIssue = issue, OperatorStart = true };
            }
            if (!url.IsLoopback || config.BaseBranch != "main")
                throw new InvalidDataException("Factory-source qualification requires its approved loopback repository.");
        }
        return config;
    }
}

public sealed class UnknownPublication(string message) : CommandConflict(message);
public sealed class GitHubFailure(string message) : Exception(message);

// Only the publisher has this client. Its write surface is immutable objects, a new task ref and a Draft PR.
public sealed class GitHubRepository : IDisposable
{
    private readonly HttpClient http;
    private readonly GitHubProject config;
    private readonly bool subscriptionWorkers;
    private readonly RepositoryLayout? layout;
    private readonly GitHubAppInstallation? installation;

    public GitHubRepository(GitHubProject config, string? policyVersion = null, RepositoryLayout? layout = null)
    {
        this.config = config;
        this.layout = layout;
        subscriptionWorkers = layout is not null || policyVersion == Factory.Fixtures.FactorySummaryProject.LiveVersion;
        http = new(new HttpClientHandler { AllowAutoRedirect = false })
        {
            BaseAddress = new Uri(config.ApiUrl.TrimEnd('/') + "/"),
            Timeout = TimeSpan.FromSeconds(15), MaxResponseContentBufferSize = 4 * 1024 * 1024
        };
        http.DefaultRequestHeaders.UserAgent.ParseAdd("Factory-Draft-Publisher/1");
        http.DefaultRequestHeaders.Accept.ParseAdd("application/vnd.github+json");
        http.DefaultRequestHeaders.Add("X-GitHub-Api-Version", "2022-11-28");
        if (!http.BaseAddress.IsLoopback)
        {
            var token = Environment.GetEnvironmentVariable("FACTORY_GITHUB_TOKEN");
            installation = GitHubAppInstallation.FromEnvironment(http, config.RepositoryId);
            if (!string.IsNullOrEmpty(token))
            {
                if (installation is not null || config.HostedIssue is not null)
                    throw new InvalidDataException("The hosted factory publisher requires its GitHub App installation.");
                http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
            }
        }
    }
    public void Dispose() => http.Dispose();

    private async Task<JsonNode?> Call(string path, object? body = null, bool allowMissing = false)
    {
        using var request = new HttpRequestMessage(body is null ? HttpMethod.Get : HttpMethod.Post, "repos/" + config.Repository + (path.Length == 0 ? "" : "/" + path));
        if (installation is not null)
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", await installation.AccessToken());
        else if (config.HostedIssue is not null && body is not null)
            throw new GitHubFailure("Hosted writes require the configured GitHub App.");
        if (body is not null) request.Content = JsonContent.Create(body, options: Wire.Json);
        using var response = await http.SendAsync(request);
        if (response.StatusCode == HttpStatusCode.Unauthorized && installation is not null)
            await installation.Rejected(request.Headers.Authorization!.Parameter!);
        if (allowMissing && response.StatusCode == HttpStatusCode.NotFound) return null;
        if (!response.IsSuccessStatusCode) throw new GitHubFailure($"GitHub returned HTTP {(int)response.StatusCode} for {request.Method} {path}.");
        try { return JsonNode.Parse(await response.Content.ReadAsStringAsync()) ?? throw new GitHubFailure("Empty GitHub response."); }
        catch (JsonException) { throw new GitHubFailure("Invalid JSON in the GitHub response."); }
    }

    public async Task<JsonNode> Issue(int number)
    {
        var repo = (await Call(""))!;
        if ((long?)repo["id"] != config.RepositoryId || !string.Equals((string?)repo["full_name"], config.Repository, StringComparison.OrdinalIgnoreCase))
            throw new CommandConflict("Configured repository identity changed.");
        return (await Call($"issues/{number}"))!;
    }
    public async Task<IssueObservation> ObserveIssue(int number, ApprovedIssue approval, IssueOrigin? origin)
    {
        if (number != approval.Number || !config.Issues.Contains(approval))
            throw new CommandConflict("Issue is outside the configured approval.");
        return Observe(await Issue(number), approval, origin);
    }

    public IssueObservation Observe(JsonNode issue, ApprovedIssue approval, IssueOrigin? origin)
    {
        var title = (string?)issue["title"] ?? "";
        var body = (string?)issue["body"] ?? "";
        var scope = Wire.Digest(title + "\n" + body);
        if (issue["id"] is not JsonValue identity || !identity.TryGetValue<long>(out var id) ||
            issue["labels"] is not JsonArray labelList || (string?)issue["updated_at"] is not { } updatedAt)
            throw new GitHubFailure("Incomplete issue response.");
        var labels = labelList.Select(l => (string?)l?["name"]).ToHashSet(StringComparer.OrdinalIgnoreCase);
        var reason = id != approval.Id || (int?)issue["number"] != approval.Number || issue["pull_request"] is not null ? "Issue identity is not authorized"
            : (string?)issue["state"] != "open" ? "Issue is closed"
            : labels.Overlaps(["hold", "factory:hold"]) ? "Issue is held"
            : labels.Overlaps(["duplicate", "superseded", "factory:covered"]) ? "Issue is already covered or superseded"
            : !labels.Contains("bug") && config.HostedIssue != approval ? "Issue is not an eligible bug"
            : string.IsNullOrWhiteSpace(title) || string.IsNullOrWhiteSpace(body) || scope != approval.ScopeHash ? "Issue scope needs approval"
            : "Eligible";

        if (origin is not null && (origin.IssueId != approval.Id || origin.ScopeHash != scope || origin.Scenario != approval.Scenario))
            reason = "The admitted issue or project scope has changed";
        return new(id, title, body, updatedAt, reason);
    }

    private static string Sha(JsonNode? value) => value is JsonValue v && v.TryGetValue<string>(out var sha)
        && Regex.IsMatch(sha, "^[0-9a-f]{40}$") ? sha : throw new GitHubFailure("Invalid Git object ID.");
    private async Task<string?> Ref(string branch)
    {
        var reference = await Call("git/ref/heads/" + branch, allowMissing: true);
        if (reference is null) return null;
        if ((string?)reference["ref"] != "refs/heads/" + branch || (string?)reference["object"]?["type"] != "commit")
            throw new CommandConflict("Unexpected Git reference.");
        return Sha(reference["object"]?["sha"]);
    }
    public async Task<string> BaseHead() => await Ref(config.BaseBranch) ?? throw new CommandConflict("Configured base branch is absent.");

    private sealed record TreeEntry(string Mode, string Type, string Sha);
    private async Task<Dictionary<string,TreeEntry>> Tree(string sha)
    {
        var tree = (await Call("git/trees/" + sha))!;
        if (Sha(tree["sha"]) != sha || (bool?)tree["truncated"] != false || tree["tree"] is not JsonArray entries)
            throw new CommandConflict("Incomplete Git tree.");
        var files = new Dictionary<string,TreeEntry>(StringComparer.Ordinal);
        foreach (var item in entries)
        {
            if (item?["mode"]?.GetValue<string>() is not { } mode || item["type"]?.GetValue<string>() is not { } type ||
                item["path"]?.GetValue<string>() is not { Length: > 0 } name || name.Contains('/') ||
                !files.TryAdd(name, new(mode, type, Sha(item["sha"]))))
                throw new CommandConflict("Invalid or duplicate Git tree entry.");
        }
        return files;
    }

    private async Task VerifyChange(string before, string after, string path, string blob)
    {
        var parts = path.Split('/');
        for (int i = 0; i < parts.Length; i++)
        {
            var original = await Tree(before);
            var changed = await Tree(after);
            original.Remove(parts[i], out var old);
            if (!changed.Remove(parts[i], out var entry) || original.Count != changed.Count ||
                original.Any(kv => !changed.TryGetValue(kv.Key, out var other) || kv.Value != other))
                throw new CommandConflict("Candidate tree differs outside the sealed change.");
            if (i == parts.Length - 1)
            {
                if (entry != new TreeEntry("100644", "blob", blob) || old is not (null or { Mode: "100644", Type: "blob" }))
                    throw new CommandConflict("Invalid candidate blob or file mode.");
            }
            else
            {
                if (old is not { Mode: "040000", Type: "tree" } || entry is not { Mode: "040000", Type: "tree" })
                    throw new CommandConflict("The admitted parent directory has changed type.");
                before = old.Sha;
                after = entry.Sha;
            }
        }
    }

    private static string Blob(string content)
    {
        var bytes = Encoding.UTF8.GetBytes(content);
        var header = Encoding.ASCII.GetBytes($"blob {bytes.Length}\0");
        return Convert.ToHexStringLower(SHA1.HashData([..header, ..bytes]));
    }

    public async Task<(string Tree, SourcePacket Source, string Path)> Snapshot(string revision, IControlledProject project, CiProfile? ci = null)
    {
        var commit = (await Call("git/commits/" + revision))!;
        if (Sha(commit["sha"]) != revision) throw new CommandConflict("Base commit identity changed.");
        var tree = Sha(commit["tree"]?["sha"]);
        if (project is Factory.Fixtures.FactorySummaryProject || project.Ci?.Rules.RepositoryLayout is not null)
        {
            if (revision != project.Ci!.Rules.Settings.GetProperty("base").GetString())
                throw new CommandConflict("Factory source differs from the approved revision.");
            var snapshot = project.Snapshot();
            var documents = project.Describe(snapshot).Documents;
            foreach (var doc in documents)
            {
                var directory = tree;
                var parts = doc.Path.Split('/');
                for (int index = 0; index < parts.Length; index++)
                {
                    var level = await Tree(directory);
                    if (!level.TryGetValue(parts[index], out var item))
                        throw new CommandConflict("Missing admitted source: " + doc.Path);
                    if (index == parts.Length - 1)
                    {
                        if (item != new TreeEntry("100644", "blob", Blob(snapshot.Content(doc.Part, doc.Path))))
                            throw new CommandConflict("Factory source bytes or mode differ from admission.");
                        var response = (await Call("git/blobs/" + item.Sha))!;
                        if (Sha(response["sha"]) != item.Sha || (string?)response["encoding"] != "base64" ||
                            (string?)response["content"] is not { Length: <= 90000 } encoded)
                            throw new GitHubFailure("Incomplete or oversized admitted source blob.");
                        var data = Convert.FromBase64String(encoded);
                        var content = new UTF8Encoding(false, true).GetString(data);
                        if (data.Length > 65536 || Blob(content) != item.Sha)
                            throw new CommandConflict("Fetched file differs from the admitted Git revision.");
                        if (content != snapshot.Content(doc.Part, doc.Path))
                            throw new CommandConflict("Fetched content differs from the installed project source.");
                    }
                    else if (item is { Mode: "040000", Type: "tree" }) directory = item.Sha;
                    else throw new CommandConflict("Factory source parent is not a directory.");
                }
            }
            return (tree, snapshot, project.Ci.Rules.RepositoryLayout?.Source ?? documents.Single(d => d.Part == SourcePart.Source).Path);
        }
        var entries = await Tree(tree);
        if (entries.Values.Any(e => e.Mode != "100644" || e.Type != "blob"))
            throw new CommandConflict("Repository tree exceeds the supported controlled fixture.");
        var files = entries.ToDictionary(e => e.Key, e => e.Value.Sha);
        var sample = project.Snapshot();
        var docs = project.Describe(sample).Documents;
        if (ci is not null)
        {
            foreach (var (name, approved) in new[] { ("factory-policy.json", ci.Manifest), (ci.Rules.Script, ci.Script) })
            {
                if (!files.TryGetValue(name, out var oid)) throw new CommandConflict($"Missing project policy input: {name}.");
                var blob = (await Call("git/blobs/" + oid))!;
                if (Sha(blob["sha"]) != oid || (string?)blob["encoding"] != "base64" || (string?)blob["content"] is not { } encoded)
                    throw new GitHubFailure("Incomplete project policy blob.");
                byte[] bytes;
                try { bytes = Convert.FromBase64String(encoded); }
                catch (FormatException) { throw new GitHubFailure("Invalid encoding in the project policy blob."); }
                if (Blob(approved) != oid || !bytes.AsSpan().SequenceEqual(Encoding.UTF8.GetBytes(approved)))
                    throw new CommandConflict($"Pinned repository {name} differs from its trusted project profile.");
            }
        }
        if (files.Count != docs.Length + (ci is null ? 0 : 2)) throw new CommandConflict("The repository must contain the qualified fixture tree.");
        var contents = new Dictionary<SourcePart,string>();
        foreach (var doc in docs)
        {
            var lf = sample.Content(doc.Part).ReplaceLineEndings("\n");
            if (!files.TryGetValue(doc.Path, out var blob)) throw new CommandConflict("Missing admitted file: " + doc.Path);
            contents[doc.Part] = new[] { lf, lf.ReplaceLineEndings("\r\n") }.FirstOrDefault(text => Blob(text) == blob)
                ?? throw new CommandConflict("Source differs from the qualified fixture: " + doc.Path);
        }
        return (tree, new(contents[SourcePart.Source], contents[SourcePart.Decisions], contents[SourcePart.Cases]),
            docs.Single(d => d.Part == SourcePart.Source).Path);
    }

    public async Task<string> BuildCommit(IssueOrigin origin, DraftIntent intent)
    {
        var parent = (await Call("git/commits/" + origin.BaseSha))!;
        if (Sha(parent["sha"]) != origin.BaseSha || Sha(parent["tree"]?["sha"]) != origin.BaseTree)
            throw new CommandConflict("Publication base tree differs from the admitted commit.");
        var files = layout?.CandidateFiles(intent.Candidate) ?? new Dictionary<string,string> { [origin.Path] = intent.Candidate };
        var tree = origin.BaseTree;
        // Prove each single-path tree transition before constructing the one candidate commit.
        foreach (var (path, content) in files.OrderBy(pair => pair.Key, StringComparer.Ordinal))
        {
            var created = (await Call("git/trees", new { base_tree = tree,
                tree = new[] { new { path, mode = "100644", type = "blob", content } } }))!;
            var updated = Sha(created["sha"]);
            await VerifyChange(tree, updated, path, Blob(content));
            tree = updated;
        }
        var identity = new { name = subscriptionWorkers ? "AI Coding Factory" : "Factory fixture", email = "factory@users.noreply.github.com",
            date = intent.OpenedAt.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'") };
        var subject = subscriptionWorkers ? "Reviewed Codex candidate" : "Reviewed fixture candidate";
        var commit = (await Call("git/commits", new { message = $"{subject} {intent.TaskId:N}", tree,
            parents = new[] { origin.BaseSha }, author = identity, committer = identity }))!;
        if (Sha(commit["tree"]?["sha"]) != tree || commit["parents"] is not JsonArray { Count: 1 } parents || Sha(parents[0]?["sha"]) != origin.BaseSha)
            throw new CommandConflict("Created commit does not match the admitted parent and tree.");
        return Sha(commit["sha"]);
    }

    public async Task EnsureBranch(DraftIntent intent, string head, Func<bool> reserve)
    {
        var current = await Ref(intent.Branch);
        if (current is null)
        {
            if (!reserve()) throw new UnknownPublication("Branch creation outcome is unknown. Inspect the original ref.");
            try { await Call("git/refs", new { @ref = "refs/heads/" + intent.Branch, sha = head }); }
            catch (GitHubFailure) { if (await Ref(intent.Branch) != head) throw; }
            current = await Ref(intent.Branch);
        }
        if (current != head) throw new CommandConflict("Task branch points to a different commit. Inspect the existing ref.");
    }

    public async Task<DraftReceipt?> FindDraft(DraftIntent intent, string head)
    {
        var owner = config.Repository.Split('/')[0];
        var query = $"pulls?state=all&head={Uri.EscapeDataString(owner + ":" + intent.Branch)}&base={Uri.EscapeDataString(config.BaseBranch)}&per_page=100";
        var pulls = (await Call(query))!.AsArray();
        if (pulls.Count == 0) return null;
        if (pulls.Count != 1) throw new CommandConflict("Multiple pull requests cover the task branch.");
        return Receipt(pulls[0]!, intent, head);
    }

    private DraftReceipt Receipt(JsonNode pr, DraftIntent intent, string head)
    {
        if ((string?)pr["state"] != "open" || (bool?)pr["draft"] != true ||
            (long?)pr["head"]?["repo"]?["id"] != config.RepositoryId || (long?)pr["base"]?["repo"]?["id"] != config.RepositoryId ||
            (string?)pr["head"]?["ref"] != intent.Branch || (string?)pr["head"]?["sha"] != head ||
            (string?)pr["base"]?["ref"] != config.BaseBranch || !((string?)pr["body"] ?? "").Split('\n').Any(line => line.TrimEnd('\r') == intent.Marker))
            throw new CommandConflict("The existing PR differs in repository, branch, candidate, marker or Draft state.");
        var number = pr["number"]?.GetValue<int>() ?? 0;
        if (number <= 0) throw new GitHubFailure("Missing pull request number.");
        var web = http.BaseAddress!.IsLoopback ? config.ApiUrl.TrimEnd('/') : "https://github.com";
        var url = $"{web}/{config.Repository}/pull/{number}";
        if (!string.Equals((string?)pr["html_url"], url, StringComparison.OrdinalIgnoreCase))
            throw new GitHubFailure("Pull request URL does not match the configured repository.");
        return new(number, url, head);
    }

    public async Task<Guid> LocalJournal()
    {
        if (!http.BaseAddress!.IsLoopback) throw new CommandConflict("Local publication required.");
        var descriptor = await Call("factory/journal");
        if ((string?)descriptor?["format"] != "factory-git-draft/v1" ||
            !Guid.TryParseExact((string?)descriptor["journal"],"N",out var journal) || journal==Guid.Empty)
            throw new CommandConflict("Missing local publication journal identity.");
        return journal;
    }

    public async Task<DraftReceipt?> LocalReceipt(IssueOrigin origin, DraftIntent intent, string head)
    {
        if (!http.BaseAddress!.IsLoopback) throw new CommandConflict("Local publication required.");
        var record = await Call($"factory/journals/{origin.LocalJournal:N}/drafts/{intent.TaskId:N}");
        if ((string?)record?["format"] != "factory-git-draft/v1")
            throw new CommandConflict("Authoritative local publication journal unavailable.");
        if (record!["pull"] is not { } pr) return null;
        if (await Ref(intent.Branch) != head) throw new CommandConflict("Recorded Draft branch changed.");
        return Receipt(pr,intent,head);
    }

    public async Task<DraftReceipt> CreateDraft(IssueOrigin origin, DraftIntent intent, string head)
    {
        var reviewer = subscriptionWorkers ? "Independent Codex reviewer run" : "Independent fixture reviewer";
        var evidence = subscriptionWorkers ? "Subscription-backed Codex implementation and review." : "Controlled fixture evidence.";
        var transport = http.BaseAddress!.IsLoopback ? "Local GitHub fixture." : "Hosted GitHub delivery.";
        var title = subscriptionWorkers ? $"Reviewed Codex change for issue #{origin.Number}" : $"Reviewed fixture change for issue #{origin.Number}";
        var summary = intent.Summary ?? throw new InvalidOperationException("Missing qualified task summary.");
        var body = string.Join("\n", intent.Marker, "", summary.Markdown(), "", "<details>", "<summary>Factory evidence</summary>", "", $"Related issue #{origin.Number}.",
            $"Pinned base: {origin.BaseSha}", $"Candidate SHA-256: {intent.CandidateHash}",
            $"Passing verifier: {intent.Verifier}", $"{reviewer}: {intent.Reviewer}",
            $"Review gate SHA-256: {intent.GateHash}", $"{evidence} {transport} Hosted checks and human review pending.");
        if (intent.Ci is { } ci)
            body += $"\nLocal CI execution: {ci.ExecutionId}\nProject policy SHA-256: {ci.PolicyHash}\nEnvironment: {ci.Environment}\nProvider receipt SHA-256: {ci.ReceiptHash}";
        body += "\n\n</details>";
        var pull = new { title, head = intent.Branch, @base = config.BaseBranch, body, draft = true, maintainer_can_modify = false };
        if (origin.LocalJournal is not null && !http.BaseAddress!.IsLoopback) throw new CommandConflict("Local publication required.");
        var pr = origin.LocalJournal is not null
            ? (await Call($"factory/journals/{origin.LocalJournal:N}/drafts/{intent.TaskId:N}", new { pull, repoId=origin.RepoId, issue=origin.Number,
                issueId=origin.IssueId, scopeHash=origin.ScopeHash, baseSha=origin.BaseSha, headSha=head }))!
            : (await Call("pulls", pull))!;
        return Receipt(pr, intent, head);
    }
}
