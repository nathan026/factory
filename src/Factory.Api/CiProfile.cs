using System.Text.Json;

namespace Factory;

public record RepositoryLayout(string Source, string Decisions, string Cases, string[] AdditionalReads, string[][] Commands, string[]? AdditionalWrites = null)
{
    [System.Text.Json.Serialization.JsonIgnore]
    public string[] Paths => [Source, Decisions, Cases, .. AdditionalReads, .. AdditionalWrites ?? []];
    [System.Text.Json.Serialization.JsonIgnore]
    public string[] Writable => [Source, .. AdditionalWrites ?? []];

    public Dictionary<string,string> CandidateFiles(string candidate)
    {
        if (AdditionalWrites is not { Length: > 0 }) return new() { [Source] = candidate };
        using var doc = JsonDocument.Parse(candidate);
        var files = new Dictionary<string,string>(StringComparer.Ordinal);
        if (doc.RootElement.ValueKind != JsonValueKind.Object) throw new InvalidDataException("Expected a candidate file map.");
        foreach (var entry in doc.RootElement.EnumerateObject())
        {
            if (entry.Value.ValueKind != JsonValueKind.String || !Writable.Contains(entry.Name) ||
                entry.Value.GetString() is not { Length: > 0 } content || content.Contains('\0') ||
                System.Text.Encoding.UTF8.GetByteCount(content) > 16384 || !files.TryAdd(entry.Name, content))
                throw new InvalidDataException("Invalid, repeated or out-of-scope candidate file.");
        }
        if (files.Count != Writable.Length) throw new InvalidDataException("The candidate is missing an approved file.");
        return files;
    }
    public bool Valid()
    {
        if (AdditionalReads is null || AdditionalReads.Length + (AdditionalWrites?.Length ?? 0) > 8 || AdditionalWrites?.Length > 3 || Commands is not { Length: > 0 and <= 4 })
            return false;
        var paths = Paths;
        return paths.All(p => p is { Length: > 0 and <= 240 })
            && paths.All(p => p.Split('/').All(part =>
                   System.Text.RegularExpressions.Regex.IsMatch(part, @"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$"))
                   && !paths.Any(other => other.StartsWith(p + "/", StringComparison.OrdinalIgnoreCase)))
            && paths.Distinct(StringComparer.OrdinalIgnoreCase).Count() == paths.Length
            && !paths.Any(p => p.Split('/')[0] is "candidate.json" or "verify.cjs" or "factory-candidate")
            && Commands.All(c => c is ["node", var script] && AdditionalReads.Contains(script)
                              || c is ["node", "--check", var checkedFile] && paths.Contains(checkedFile));
    }
}

public record CiRules(ProjectPolicy Policy, string Goal, JsonElement Settings, string Environment, string Script,
    string ScriptHash, string[] RequiredRoles, string Publication, string Promotion, BlueprintPolicy? Blueprint = null, RepositoryLayout? RepositoryLayout = null);

public record CiProfile(CiRules Rules, string Manifest, string Script)
{
    public bool RequiresCodex => Rules.RepositoryLayout is not null || Rules.Policy.Version == Factory.Fixtures.FactorySummaryProject.LiveVersion;
    public string Hash => Wire.Digest(Manifest);
    public static CiProfile Load(string directory, string version)
    {
        string Read(string name) => File.ReadAllText(Path.Combine(directory, name)).ReplaceLineEndings("\n");
        var match = System.Text.RegularExpressions.Regex.Match(version, @"^[a-z][a-z0-9-]*/(v[1-9][0-9]*)$");
        if (!match.Success) throw new InvalidDataException("Invalid CI project version.");
        var versioned = match.Groups[1].Value + "-policy.json";
        var manifest = Read(File.Exists(Path.Combine(directory, versioned)) ? versioned : "factory-policy.json");
        var rules = JsonSerializer.Deserialize<CiRules>(manifest, Wire.ResultJson)!;
        if (rules.RepositoryLayout?.Valid() == false || rules.RepositoryLayout is not null && rules.Blueprint is null || !rules.Policy.Valid() || rules.Policy.Version != version || rules.Blueprint?.Valid(rules.Policy) == false ||
            string.IsNullOrWhiteSpace(rules.Goal) || rules.Script is null || !(System.Text.RegularExpressions.Regex.IsMatch(rules.Script, @"^[A-Za-z0-9_-]+\.cjs$")
                || rules.RepositoryLayout?.AdditionalReads.Contains(rules.Script) == true && rules.Script.EndsWith(".cjs", StringComparison.Ordinal)) ||
            !System.Text.RegularExpressions.Regex.IsMatch(rules.Environment, @"^[a-z0-9./:_-]+@sha256:[0-9a-f]{64}$") ||
            !rules.RequiredRoles.SequenceEqual(["Planner", "Implementer", "Verifier", "Reviewer"]) ||
            rules.Publication != "reviewed-draft" || rules.Promotion != "human")
            throw new InvalidDataException("Unsupported project policy, role, environment or publication rule.");
        var script = Read(rules.Script);
        if (Wire.Digest(script) != rules.ScriptHash) throw new InvalidDataException("CI script differs from the trusted project policy.");
        return new(rules, manifest, script);
    }
}
