using System.Text.Json;
using System.Text.Json.Nodes;
using Factory;
using Factory.Fixtures;

static class HostedGitHubChecks
{
    public static void Run()
    {
        var name = "hosted-check-" + Guid.NewGuid().ToString("N");
        var directory = Directory.CreateDirectory(Path.Combine(AppContext.BaseDirectory, "projects", name));
        try
        {
            var seed = Path.Combine(AppContext.BaseDirectory, "projects", "checkout-scope");
            var manifest = JsonNode.Parse(File.ReadAllText(Path.Combine(seed, "v2-policy.json")))!;
            var script = (string)manifest["script"]!;
            var verifier = Path.Combine(directory.FullName, script);
            Directory.CreateDirectory(Path.GetDirectoryName(verifier)!);
            File.Copy(Path.Combine(seed, script), verifier);
            manifest["policy"]!["version"] = name + "/v1";
            var issue = new ApprovedIssue(7, 417, Wire.Digest("Accent\nApproved scope"), "known-decision");
            var hosted = new HostedGitHubApproval("stable", issue);
            var settings = manifest["settings"]!;
            settings["repository"] = "example/test";
            settings["repositoryId"] = 123;
            var manifestPath = Path.Combine(directory.FullName, "v1-policy.json");
            var input = Path.Combine(directory.FullName, "intake.json");
            var config = new GitHubProject("example/test", 123, "stable", [issue]);
            SampleRuntime Install()
            {
                File.WriteAllText(manifestPath, manifest.ToJsonString());
                return new SampleRuntime(name + "/v1");
            }
            GitHubProject Load(GitHubProject candidate, IControlledProject project)
            {
                File.WriteAllText(input, JsonSerializer.Serialize(candidate, Wire.Json));
                return GitHubProject.Load(input, project);
            }
            void Refuse(GitHubProject candidate, IControlledProject project)
            {
                try { Load(candidate, project); }
                catch (InvalidDataException) { return; }
                throw new Exception("Unexpected hosted admission");
            }
            Refuse(config, Install());
            manifest["hostedGitHub"] = JsonSerializer.SerializeToNode(hosted, Wire.Json);
            var project = Install();
            var approved = Load(config, project);
            foreach (var invalid in new[] {
                config with { RepositoryId = 124 }, config with { Repository = "other/test" },
                config with { BaseBranch = "main" }, config with { ApiUrl = "http://127.0.0.1:8080" },
                config with { ApiUrl = "https://api.github.com/#other" }, config with { LocalAtomicDraft = true },
                config with { Issues = [issue with { Id = 418 }] }, config with { Issues = [issue with { Number = 8 }] },
                config with { Issues = [issue with { ScopeHash = Wire.Digest("new") }] },
                config with { Issues = [issue, issue with { Id = 418, Number = 8 }] } })
                Refuse(invalid, project);
            using var repo = new GitHubRepository(approved);
            var response = JsonNode.Parse("""{"id":417,"number":7,"state":"open","title":"Accent","body":"Approved scope","updated_at":"2026-01-01T00:00:00Z","labels":[]}""")!;
            void Check(JsonNode observed, string expected)
            {
                if (repo.Observe(observed, issue, null).Reason != expected)
                    throw new Exception("Unexpected issue eligibility for " + expected);
            }
            Check(response, "Eligible");
            foreach (var (key, value, reason) in new (string, JsonNode?, string)[] {
                ("body", "different", "Issue scope needs approval"), ("id", 418L, "Issue identity is not authorized"),
                ("pull_request", new JsonObject(), "Issue identity is not authorized"), ("state", "closed", "Issue is closed"),
                ("labels", JsonNode.Parse("""[{"name":"factory:hold"}]"""), "Issue is held"),
                ("labels", JsonNode.Parse("""[{"name":"superseded"}]"""), "Issue is already covered or superseded") } )
            {
                var changed = response.DeepClone();
                changed[key] = value;
                Check(changed, reason);
            }
            settings["base"] = "HEAD";
            Refuse(config, Install());
            Console.WriteLine("Passed: single hosted issue approval, repository, revision, scope and holds");
        }
        finally { directory.Delete(recursive: true); }
    }
}
