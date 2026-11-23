using System.Text.RegularExpressions;

namespace Factory;

// A read model of accepted task evidence. It never changes the publication identity or a phase decision.
public sealed record DeliverySummary(string Issue, string? Request, string[] Work, string[] Checks)
{
    public static DeliverySummary From(WorkItem task, string sourcePath, RepositoryLayout? layout, bool fromIssue)
    {
        var opening = fromIssue ? task.OpeningMessage?.Split('\n', 2) : null;
        var issue = opening?.FirstOrDefault()?.Trim();
        var request = opening is [_, var body] ? Regex.Replace(body.Trim(), @"\s+", " ") : null;
        request = task.FollowUp?.Scope ?? request;
        if (request?.Length > 250) request = request[..(char.IsHighSurrogate(request[249]) ? 249 : 250)] + "…";
        var work = new List<string>();
        var checks = new List<string>();
        if (task.Candidate is { } candidate)
        {
            var files = layout?.CandidateFiles(candidate) ?? new() { [sourcePath] = candidate };
            foreach (var (path, contents) in files)
            {
                var original = task.Source.Content(SourcePart.Source, path);
                if (original == contents) continue;
                work.Add($"The factory changed {path}.");
                DescribeEdits(work, path, original, contents);
            }
            if (work.Count == 0) work.Add("The candidate matches the original files.");
            var hash = Wire.Digest(candidate);
            var receipt = task.Receipts.LastOrDefault();
            if (receipt is not null && task.Policy is not null && ContractGate.ReceiptError(task, receipt) is null)
            {
                foreach (var check in receipt.Checks)
                    checks.Add(check.Passed ? $"The {check.Id} check passed." : $"The {check.Id} check failed.");
                if (receipt.Passed && task.Review is { } review && review.RunId != task.Writer &&
                    review.CandidateHash == hash && task.Memory is not null && ContractGate.ReviewError(task, review) is null)
                    checks.Add(review.Approved ? "The independent reviewer approved these changes." : "The independent reviewer requested changes.");
                else if (receipt.Passed) checks.Add("Independent review is pending.");
            }
        }
        else work.Add("The factory has not recorded a candidate.");
        if (checks.Count == 0) checks.Add("Verification is pending.");
        return new(string.IsNullOrWhiteSpace(issue) ? task.Goal : issue, request, [.. work], [.. checks]);
    }

    // Quote a bounded sample of actual edits. Do not infer a change in program behaviour.
    private static void DescribeEdits(List<string> work, string path, string original, string candidate)
    {
        var before = original.ReplaceLineEndings("\n").Split('\n');
        var after = candidate.ReplaceLineEndings("\n").Split('\n');
        if (before.Length != after.Length)
        {
            var common = 0;
            while (common < Math.Min(before.Length, after.Length) && before[common] == after[common]) common++;
            var suffix = 0;
            while (suffix < Math.Min(before.Length, after.Length) - common &&
                before[^(suffix + 1)] == after[^(suffix + 1)]) suffix++;
            work.Add($"The first change starts at line {common + 1}.");
            if (common + suffix < before.Length && before[common].Trim() is { Length: > 0 and <= 96 } oldLine)
                work.Add($"The original text starts with “{oldLine}”.");
            if (common + suffix < after.Length && after[common].Trim() is { Length: > 0 and <= 96 } newLine)
                work.Add($"The new text starts with “{newLine}”.");
            return;
        }
        var changes = Enumerable.Range(0, before.Length).Where(i => before[i] != after[i]).ToArray();
        foreach (var i in changes.Take(3))
        {
            var oldText = before[i].Trim();
            var newText = after[i].Trim();
            if (path.EndsWith(".css", StringComparison.OrdinalIgnoreCase) && ColourChange(before, after, i) is { } colour)
                work.Add(colour);
            else if (oldText.Length is > 0 and <= 96 && newText.Length is > 0 and <= 96 && oldText != newText)
                work.Add($"Line {i + 1} changed from “{oldText}” to “{newText}”.");
            else work.Add($"The factory changed line {i + 1}.");
        }
        if (changes.Length > 3) work.Add($"The file has {changes.Length} changed lines in total.");
    }

    // Only describe simple colour declarations with an unchanged selector and property.
    private static string? ColourChange(string[] before, string[] after, int line)
    {
        const string declaration = @"^\s*([a-z-]+):\s*(#(?:[a-fA-F0-9]{3,4}|[a-fA-F0-9]{6}|[a-fA-F0-9]{8}));\s*$";
        var oldValue = Regex.Match(before[line], declaration);
        var newValue = Regex.Match(after[line], declaration);
        if (!newValue.Success || !oldValue.Success || newValue.Groups[1].Value != oldValue.Groups[1].Value) return null;
        for (var i = line - 1; i >= 0; i--)
        {
            if (after[i].Contains('}') || before[i].Contains('}')) return null;
            if (!after[i].Contains('{') && !before[i].Contains('{')) continue;
            if (before[i] != after[i] || i > 0 && after[i - 1].TrimEnd().EndsWith(',')) return null;
            var selector = Regex.Match(after[i], @"^\s*([.#]?[a-zA-Z][a-zA-Z0-9.# -]{0,60})\s*\{\s*$");
            if (!selector.Success) return null;
            return $"For {selector.Groups[1].Value.Trim()}, the factory changed {newValue.Groups[1].Value} from {oldValue.Groups[2].Value} to {newValue.Groups[2].Value}.";
        }
        return null;
    }

    // Titles and file excerpts can contain Markdown, HTML or mentions. Render them as literal text.
    internal static string Literal(string value) => Regex.Replace(value,
        @"[&<>\\`*_{}\[\]()#+!|~@]", m => $"&#{(int)m.Value[0]};");

    public string Markdown() => string.Join("\n", "## Issue", "", Literal(Issue) + (string.IsNullOrEmpty(Request) ? "" : "\n\n" + Literal(Request)), "", "## Work done", "",
        string.Join("\n", Work.Select(line => "- " + Literal(line))), "", "## Checks and review", "",
        string.Join("\n", Checks.Select(line => "- " + Literal(line))), "", "## Next action", "",
        "Review the changes in **Files changed**.");
}
