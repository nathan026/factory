using System.Text;
using System.Text.RegularExpressions;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Factory;

public record ScopePlanning(string BaseSha, ExpertSource[] Files,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingDefault)] bool RequireAssessment = false)
{
    internal static bool FilePath(string? path) =>
        path is { Length: > 0 and <= 240 } && path.Split('/').All(p => Regex.IsMatch(p, @"\A[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\z"));

    internal bool Valid() =>
        Regex.IsMatch(BaseSha ?? "", @"\A[0-9a-f]{40}\z") && Files is { Length: > 0 and <= 15 } &&
        Files.All(f => f is not null && FilePath(f.Path) && f.Path != OwnerQuestion.Path &&
            f.Content is not null && !f.Content.Contains('\0') && Encoding.UTF8.GetByteCount(f.Content) <= 16384 &&
            Wire.Digest(f.Content) == f.Digest) &&
        Files.DistinctBy(f => f.Path, StringComparer.OrdinalIgnoreCase).Count() == Files.Length &&
        Files.Sum(f => Encoding.UTF8.GetByteCount(f.Content)) <= 128 * 1024;
}

public record ScopeProposal(string Goal, string[] Paths, string[] Checks)
{
    internal bool Valid() => !string.IsNullOrWhiteSpace(Goal) && Goal.Length <= 2000 &&
        Paths is { Length: > 0 and <= 16 } && Paths.All(ScopePlanning.FilePath) &&
        Paths.Distinct(StringComparer.OrdinalIgnoreCase).Count() == Paths.Length &&
        Checks is { Length: > 0 and <= 16 } && Checks.All(c => !string.IsNullOrWhiteSpace(c) && c.Length <= 2000) &&
        Checks.Distinct(StringComparer.Ordinal).Count() == Checks.Length;
}

public record ScopeAssessment(string[] Investigations, string[] Decisions)
{
    private static bool ValidItems(string[]? items) => items is { Length: <= 8 } &&
        items.All(item => !string.IsNullOrWhiteSpace(item) && item.Length <= 400) &&
        items.Distinct(StringComparer.Ordinal).Count() == items.Length;

    internal bool Valid() => ValidItems(Investigations) && ValidItems(Decisions);
    internal bool HasOpenItems() => Investigations.Length > 0 || Decisions.Length > 0;
}

public record ScopeReply(string RequestHash, string Reply, ScopeProposal? Proposal,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingDefault)] JsonElement Assessment = default)
{
    internal bool Valid(OwnerQuestion question) => RequestHash == GateInput.Hash(question) &&
        !string.IsNullOrWhiteSpace(Reply) && Reply.Length <= 4000;

    internal ScopeAssessment? ReadAssessment()
    {
        if (Assessment.ValueKind != JsonValueKind.Object) return null;
        try
        {
            var items = Assessment.Deserialize<ScopeAssessment>(Wire.ResultJson);
            return items is not null && items.Valid() ? items : null;
        }
        catch (JsonException) { return null; }
    }

    // Keep useful discussion text when a suggested scope cannot be admitted.
    public string DiscussionText()
    {
        if (Proposal is null) return Reply;
        var checks = Proposal.Checks is { Length: > 0 and <= 16 } items &&
            items.All(c => !string.IsNullOrWhiteSpace(c) && c.Length <= 2000)
            ? "\n\nSuggested checks\n" + string.Join("\n", items.Select(c => "• " + c)) : "";
        return Reply + checks;
    }
}
