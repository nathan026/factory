using System.Text.Json;
using Factory;

namespace Factory.Fixtures;

// Controlled worker outputs. Only the controller dispatches phases and changes task state.
public sealed class SampleRuntime : IControlledProject
{
    public ExpertProfile ExpertProfile { get; } = JsonSerializer.Deserialize<ExpertProfile>(
        File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "profiles", "shipping-expert-v1.json")), Wire.ResultJson)!;
    public CiProfile? Ci { get; }
    private readonly string sampleDirectory;
    private string Proposal { get; } = "4000";
    private string SourcePath => Ci?.Rules.RepositoryLayout?.Source ?? "shipping-rule.json";
    private string DecisionPath => Ci?.Rules.RepositoryLayout?.Decisions ?? "decisions.json";
    private string CasesPath => Ci?.Rules.RepositoryLayout?.Cases ?? "acceptance.json";
    public string Goal => Ci?.Rules.Goal ?? "Correct the free shipping boundary in the sample checkout rule.";
    public ProjectPolicy Policy { get; }

    private readonly bool ownerDiscussion;

    public SampleRuntime(string version = "shipping/v1", bool ownerDiscussion = false, CiProfile? installed = null)
    {
        this.ownerDiscussion = ownerDiscussion && version == "shipping/v1";
        sampleDirectory = Path.Combine(AppContext.BaseDirectory, "sample");
        if (version != "shipping/v1")
        {
            if (!System.Text.RegularExpressions.Regex.IsMatch(version, @"^[a-z][a-z0-9-]*/v[1-9][0-9]*$"))
                throw new InvalidDataException("Invalid project profile name.");
            sampleDirectory = Path.Combine(AppContext.BaseDirectory, "projects", version.Split('/')[0]);
            Ci = installed ?? CiProfile.Load(sampleDirectory, version);
            if (!Ci.Rules.Settings.TryGetProperty("proposedThreshold", out var value) ||
                !value.TryGetInt32(out var amount) || amount < 0)
                throw new InvalidDataException("The checkout profile requires a nonnegative proposed threshold.");
            Proposal = amount.ToString(System.Globalization.CultureInfo.InvariantCulture);
        }
        Policy = Ci?.Rules.Policy ?? JsonSerializer.Deserialize<ProjectPolicy>(
            File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "profiles", "shipping-v1.json")), Wire.Json)!;
    }
    public WorkerResult<string> Plan(Assignment assignment, PhaseContext? context = null) => new(assignment.Reference(), ResultStatus.Completed,
        context is null ? "Change only the shipping rule. Verify orders below, at and above the accepted threshold."
            : $"{context.Brief.Goal} Verify {string.Join(", ", context.Brief.Requirements.Criteria.Select(c => c.Id))}.",
        context is null ? null : new(Ci?.Rules.RepositoryLayout?.Writable ?? [SourcePath], assignment.RequiredChecks,
            [DecisionPath, CasesPath], "An exclusive comparison fails the exact threshold.",
            GateInput.Hash(context.Brief)));
    public ScenarioOption[] Scenarios => Ci?.Rules.RepositoryLayout is not null
        ? [new("known-decision", "Resolve a boundary conflict")] :
        [new("known-decision", "Resolve a boundary conflict"), new("scope-change", "Evaluate a scope change"),
         new("expert-comparison", "Choose the boundary comparison"),
         .. (ownerDiscussion ? new[] { new ScenarioOption("restated-decision", "Ask about the accepted boundary in different words") } : [])];
    public bool Supports(string scenario) => Scenarios.Any(s => s.Id == scenario);
    public DecisionOption[] Choices(SourcePacket source) => Standard(source) is { } standard
        ? [new("keep", $"Keep {standard.Value} cents"), new("approve", $"Approve {Proposal} cents")]
        : [new("approve", $"Approve {Proposal} cents")];
    public ProjectBrief Describe(SourcePacket snapshot)
    {
        var examples = JsonSerializer.Deserialize<Example[]>(snapshot.Cases, Wire.Json)!;
        return new(new(Criteria(examples), ["Use the accepted threshold for this task."],
            ["Changes to pricing outside the approved shipping boundary."]),
            [SourceDocument.Capture(SourcePart.Source, SourcePath, snapshot),
             SourceDocument.Capture(SourcePart.Decisions, DecisionPath, snapshot),
             SourceDocument.Capture(SourcePart.Cases, CasesPath, snapshot),
             .. (Ci?.Rules.RepositoryLayout?.AdditionalWrites ?? []).Select(path => SourceDocument.Capture(SourcePart.Source, path, snapshot)),
             .. (Ci?.Rules.RepositoryLayout?.AdditionalReads ?? []).Select(path => SourceDocument.Capture(SourcePart.Cases, path, snapshot))]);
    }
    private static Criterion[] Criteria(Example[] examples) =>
        examples.Select(e => new Criterion(FormattableString.Invariant($"order-offset/{e.OffsetCents}"),
            FormattableString.Invariant($"At offset {e.OffsetCents} cents from the accepted threshold, free shipping must be {e.FreeShipping}."))).ToArray();

    private record Rule(int ThresholdCents, string Comparison);
    private record Example(int OffsetCents, bool FreeShipping);

    public SourcePacket Snapshot()
    {
        string Read(string file) => File.ReadAllText(Path.Combine(sampleDirectory, file));
        if (Ci?.Rules.RepositoryLayout is { } layout)
        {
            string Pinned(string path) => Read(path).ReplaceLineEndings("\n");
            return new(Pinned(SourcePath), Pinned(DecisionPath), Pinned(CasesPath),
                JsonSerializer.Serialize(layout.AdditionalReads.Concat(layout.AdditionalWrites ?? []).ToDictionary(path => path, Pinned), Wire.Json));
        }
        return new(Read(SourcePath), Read(DecisionPath), Read(CasesPath));
    }

    public WorkerResult<Question> Ask(Assignment assignment, string scenario, PhaseContext? context = null) =>
        new(assignment.Reference(), ResultStatus.NeedsInput, scenario == "restated-decision"
            ? AdmittedQuestion(scenario) with { Text = "When a basket totals precisely fifty dollars, does it qualify for free shipping?" }
            : AdmittedQuestion(scenario));

    public Question AdmittedQuestion(string scenario) => scenario switch
    {
        "known-decision" or "restated-decision" => new("standard-threshold", "The current rule excludes the exact threshold. Should I change the amount or the comparison?", false),
        "scope-change" => new("lower-threshold", $"Should the fix also lower the threshold to {Proposal} cents?", true),
        "expert-comparison" => new("comparison-semantics", "Which checkout comparison implements the accepted boundary without changing the price threshold?", false),
        _ => throw new CommandConflict("Unknown controlled scenario.")
    };

    private static Decision? Standard(SourcePacket packet) => Recorded(packet.Decisions, "standard-threshold");

    private static Decision? Recorded(string decisions, string subject)
    {
        var applicable = JsonSerializer.Deserialize<Decision[]>(decisions, Wire.Json)!
            .Where(d => d.Subject == subject).ToArray();
        return applicable is [var decision] && !string.IsNullOrWhiteSpace(decision.Id) && !string.IsNullOrWhiteSpace(decision.Text)
            && int.TryParse(decision.Value, out var amount) && amount >= 0 ? decision : null;
    }

    public Decision? Investigate(Question question, SourcePacket packet, BriefRevision? brief = null)
    {
        var known = Standard(packet);
        var existing = JsonSerializer.Deserialize<Rule>(packet.Source, Wire.Json)!;
        var cases = JsonSerializer.Deserialize<Example[]>(packet.Cases, Wire.Json)!;
        if (known is null || existing.ThresholdCents != int.Parse(known.Value)
            || (brief is not null && !brief.Requirements.Criteria.SequenceEqual(Criteria(cases)))
            || existing.Comparison is not ("greater" or "greaterOrEqual") || !InclusiveExamples(cases)) return null;
        if (question.Subject == known.Subject) return known;
        // A specific existing ruling on this proposal can settle a reserved question.
        if (question.Subject == "lower-threshold" && Recorded(packet.Decisions, question.Subject) is { } ruling
            && (ruling.Value == known.Value || ruling.Value == Proposal)) return ruling;
        return null;
    }

    private static bool InclusiveExamples(Example[] cases) =>
        new[] { -1, 0, 1 }.All(offset => cases.Any(c => c.OffsetCents == offset))
        && cases.All(c => c.FreeShipping == (c.OffsetCents >= 0));

    public string[] ExpertSuggestions(Question question) =>
        question.Reserved ? ["keep", "approve"] : ["inclusive-comparison"];

    public ExpertOpinion? Consult(ExpertRequest request)
    {
        if (request.ProfileVersion != "checkout-policy/v1") return null;
        var hash = GateInput.Hash(request);
        string[] citations = [DecisionPath, CasesPath];
        var documents = request.Sources.ToDictionary(s => s.Path, s => s.Content);
        var accepted = documents.TryGetValue(citations[0], out var decisions) ? Recorded(decisions, "standard-threshold") : null;
        if (accepted is null || !documents.TryGetValue(citations[1], out var cases)
            || !InclusiveExamples(JsonSerializer.Deserialize<Example[]>(cases, Wire.Json)!))
            return new(hash, "The available evidence does not establish an inclusive boundary.",
                "Investigate the conflicting or missing project evidence.", "Defer the change.",
                "Proceeding could change checkout prices.", "The recorded examples describe checkout eligibility.",
                "The accepted threshold or its examples are unresolved.", citations, null);

        string assumption = $"Decision {accepted.Id} applies to this checkout.";
        if (!request.Question.Reserved)
            return new(hash, "Equality at the accepted threshold must qualify.",
                "Use greaterOrEqual at the existing threshold.", "Use an explicit equality branch with the greater-than comparison.",
                "Both include the exact threshold. The single comparator fits the admitted rule format.", assumption,
                "The verifier still needs to check the implemented boundary.", citations, "inclusive-comparison");

        int current = int.Parse(accepted.Value);
        string consequence = current == int.Parse(Proposal) ? "The proposed amount already matches the accepted threshold."
            : $"Changing the threshold changes eligibility for orders from {Math.Min(current, int.Parse(Proposal))} to {Math.Max(current, int.Parse(Proposal)) - 1} cents.";
        return new(hash, $"The accepted boundary is {accepted.Value} cents.",
            $"Keep the accepted {accepted.Value}-cent threshold and fix the comparison.",
            $"Approve {Proposal} cents for this sample task.", consequence,
            assumption, "The cited sources do not establish authority for a new pricing change.", citations, "keep");
    }

    public Decision? ResolveWithinScope(Question question, string? suggestedOption, SourcePacket source, BriefRevision brief)
    {
        if (question != AdmittedQuestion("expert-comparison") || suggestedOption != "inclusive-comparison"
            || Investigate(AdmittedQuestion("known-decision"), source, brief) is not { } standard) return null;
        return new("owner-inclusive-comparison", question.Subject,
            $"Use greaterOrEqual at {standard.Value} cents under {standard.Id}.", standard.Value);
    }

    public DecisionResolution ApplyChoice(string choice, SourcePacket packet)
    {
        var standard = Standard(packet);
        return choice switch
        {
            "keep" when standard is not null => new(standard, null),
            "approve" => new(new($"operator-{Proposal}", "lower-threshold",
                $"Operator approved free shipping from {Proposal} cents for this task.", Proposal), standard?.Id),
            _ => throw new CommandConflict("The supplied choice cannot resolve this question.")
        };
    }

    public WorkerResult<string> Implement(Assignment assignment, SourcePacket source, Decision answer, Receipt? feedback, PhaseContext? context = null)
    {
        var baseline = JsonSerializer.Deserialize<Rule>(source.Source, Wire.Json)!;
        // The scripted first attempt keeps the original comparator. Its success claim must fail.
        var checks = context is null ? feedback?.Checks : context.Findings.LastOrDefault()?.Checks;
        var repair = checks?.Any(check => !check.Passed) == true || context?.ReviewFeedback is not null
            || context?.Blueprint?.LocalFeedback.Any(c => !c.Passed) == true;
        var comparison = repair ? "greaterOrEqual" : baseline.Comparison;
        return new(assignment.Reference(), ResultStatus.Completed,
            JsonSerializer.Serialize(new Rule(int.Parse(answer.Value), comparison), Wire.Json));
    }

    public string? CandidateError(string candidate)
    {
        if (Ci?.Rules.RepositoryLayout is not { AdditionalWrites.Length: > 0 } layout) return RuleError(candidate);
        try { return layout.CandidateFiles(candidate).Values.Select(RuleError).FirstOrDefault(error => error is not null); }
        catch (Exception e) when (e is InvalidDataException or JsonException) { return "Candidate file map differs from the approved scope."; }
    }

    private static string? RuleError(string candidate)
    {
        try
        {
            using var json = JsonDocument.Parse(candidate);
            var root = json.RootElement;
            if (root.ValueKind == JsonValueKind.Object && root.EnumerateObject().Count() == 2
                && root.TryGetProperty("thresholdCents", out var threshold) && threshold.ValueKind == JsonValueKind.Number
                && threshold.TryGetInt32(out var amount) && amount >= 0
                && root.TryGetProperty("comparison", out var comparison) && comparison.ValueKind == JsonValueKind.String
                && comparison.GetString() is "greater" or "greaterOrEqual") return null;
        }
        catch (JsonException) { }
        return "Candidate must contain the scoped shipping rule with a threshold and supported comparison.";
    }

    private List<Check> ExpectedChecks(string candidate, Decision answer, Example[] examples)
    {
        if (Ci?.Rules.RepositoryLayout is not { AdditionalWrites.Length: > 0 } layout)
            return RuleChecks(candidate, answer, examples);
        return layout.CandidateFiles(candidate).Values.SelectMany(file => RuleChecks(file, answer, examples))
            .GroupBy(check => check.Id).Select(group => group.First() with { Passed = group.All(check => check.Passed) }).ToList();
    }

    private static List<Check> RuleChecks(string candidate, Decision answer, Example[] examples)
    {
        var rule = JsonSerializer.Deserialize<Rule>(candidate, Wire.Json)!;
        return examples.Select(example =>
        {
            var amount = int.Parse(answer.Value) + example.OffsetCents;
            var actual = rule.Comparison switch
            {
                "greater" => amount > rule.ThresholdCents,
                "greaterOrEqual" => amount >= rule.ThresholdCents,
                _ => throw new InvalidDataException("Unknown sample comparison")
            };
            return new Check($"Order of {amount} cents should have free shipping = {example.FreeShipping}", actual == example.FreeShipping,
                FormattableString.Invariant($"order-offset/{example.OffsetCents}"));
        }).ToList();
    }

    public IReadOnlyList<Check> LocalChecks(string candidate, Decision answer, SourcePacket packet, string[] selectors)
    {
        var examples = JsonSerializer.Deserialize<Example[]>(packet.Cases, Wire.Json)!;
        var selected = examples.Where(e => selectors.Contains(FormattableString.Invariant($"order-offset/{e.OffsetCents}"))).ToArray();
        return ExpectedChecks(candidate, answer, selected);
    }

    public string? VerificationError(Receipt receipt, Decision answer, SourcePacket source)
    {
        var expected = ExpectedChecks(receipt.Candidate, answer, JsonSerializer.Deserialize<Example[]>(source.Cases, Wire.Json)!);
        return expected.Count == receipt.Checks.Count && expected.ToHashSet().SetEquals(receipt.Checks) ? null
            : "Reported checks disagree with the pinned candidate, decision or acceptance examples.";
    }

    // The trusted sample checker interprets declarative data. It never executes repository code.
    public Receipt Verify(Assignment assignment, string candidate, Decision answer, SourcePacket packet, PhaseContext? context = null)
    {
        var examples = JsonSerializer.Deserialize<Example[]>(packet.Cases, Wire.Json)!;
        if (context is not null && !context.Brief.Requirements.Criteria.Select(c => c.Id).ToHashSet()
            .SetEquals(examples.Select(e => FormattableString.Invariant($"order-offset/{e.OffsetCents}"))))
            throw new InvalidDataException("The pinned criteria and source acceptance examples disagree.");
        var checks = ExpectedChecks(candidate, answer, examples);
        return new(assignment.TaskId, assignment.RunId, candidate, Wire.Digest(candidate), packet.Fingerprint(),
            checks, assignment.SchemaVersion, assignment.PolicyVersion, ContractGate.VerificationRule);
    }

    public WorkerResult<Review> Assess(Assignment assignment, string candidate, Decision answer, PhaseContext? context = null)
    {
        var rule = JsonSerializer.Deserialize<Rule>(candidate, Wire.Json)!;
        var covered = context is null || (context.Brief.AcceptedDecision == answer
            && context.Findings.LastOrDefault() is { } verified && verified.CandidateHash == Wire.Digest(candidate)
            && context.Brief.Requirements.Criteria.All(c => verified.Checks.Any(check => check.Id == c.Id && check.Passed)));
        var accepted = covered && rule.Comparison == "greaterOrEqual" && rule.ThresholdCents == int.Parse(answer.Value);
        var verifiedCandidate = context?.Findings.LastOrDefault();
        var evidence = verifiedCandidate is null ? null : new ReviewEvidence(verifiedCandidate.RunId,
            GateInput.Hash(verifiedCandidate), GateInput.Hash(context!.Brief), accepted ? [] :
            [new(context.Brief.Requirements.Criteria[0].Id, FindingSeverity.Blocking,
                "The candidate comparison or threshold differs from the accepted rule.", "Apply the accepted shipping boundary.")]);
        return new(assignment.Reference(), ResultStatus.Completed, new(assignment.RunId, Wire.Digest(candidate), accepted, accepted
            ? "Sample reviewer found the inclusive comparison and the agreed threshold."
            : "Candidate differs from the accepted shipping policy.", evidence));
    }
}
