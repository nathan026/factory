using System.Text;

namespace Factory;

// A deployment-installed brief admits one source file. All implementation, CI and review are external phases.
public sealed class HostedRepositoryProject : IControlledProject
{
    private readonly RepositoryLayout layout;
    private readonly SourcePacket snapshot;
    private readonly string scenario;
    public CiProfile Ci { get; }
    public ProjectPolicy Policy => Ci.Rules.Policy;
    public string Goal => Ci.Rules.Goal;
    public ScenarioOption[] Scenarios => [new(scenario, Goal)];
    public ExpertProfile ExpertProfile => new("repository/v1", "Approved repository scope", [layout.Decisions], 4096);

    public HostedRepositoryProject(string directory, CiProfile ci)
    {
        Ci = ci;
        layout = ci.Rules.RepositoryLayout ?? throw new InvalidDataException("Expected a scoped repository layout.");
        scenario = ci.Rules.HostedGitHub?.Issue?.Scenario ?? throw new InvalidDataException("Missing approved issue scenario.");
        if (layout.AdditionalWrites is { Length: > 0 } || layout.AdditionalReads.Length != 0 ||
            ci.Rules.Blueprint?.EarlyChecks is not ["bounded-source"] || layout.Commands is not [["node", "verify.cjs"]])
            throw new InvalidDataException("This hosted profile requires one source, bounded-source and the installed verifier.");
        string Read(string path) => File.ReadAllText(Path.Combine(directory, path)).ReplaceLineEndings("\n");
        snapshot = new(Read(layout.Source), Read(layout.Decisions), Read(layout.Cases), "{}");
    }

    public bool Supports(string value) => scenario == value;
    public SourcePacket Snapshot() => snapshot;
    public ProjectBrief Describe(SourcePacket source) => new(new(Policy.RequiredChecks.Select(id => new Criterion(id,
        id == "bounded-source" ? "Keep the candidate within the admitted source limit." : Goal)).ToArray(),
        [Goal], ["Write only " + layout.Source + "."]),
        [SourceDocument.Capture(SourcePart.Source, layout.Source, source), SourceDocument.Capture(SourcePart.Decisions, layout.Decisions, source),
         SourceDocument.Capture(SourcePart.Cases, layout.Cases, source)]);
    public WorkerResult<string> Plan(Assignment a, PhaseContext? context) => new(a.Reference(), ResultStatus.Completed, Goal,
        new(layout.Writable, a.RequiredChecks, [layout.Decisions, layout.Cases], "Preserve behavior outside the approved change.", GateInput.Hash(context!.Brief)));
    public Question AdmittedQuestion(string value) => Supports(value)
        ? new("approved-scope", "Which change and file did the operator approve?", false)
        : throw new CommandConflict("Issue scenario differs from the installed approval.");
    public WorkerResult<Question> Ask(Assignment a, string value, PhaseContext? context) =>
        new(a.Reference(), ResultStatus.NeedsInput, AdmittedQuestion(value));
    public Decision? Investigate(Question q, SourcePacket s, BriefRevision? brief) =>
        q == AdmittedQuestion(scenario) && s == snapshot && brief?.Goal == Goal
            ? new("repository-scope", q.Subject, Goal, layout.Source) : null;
    public DecisionOption[] Choices(SourcePacket s) => [];
    public DecisionResolution ApplyChoice(string choice, SourcePacket s) => throw new CommandConflict("No reserved choice in the approved scope.");
    public ExpertOpinion? Consult(ExpertRequest request) => null;
    public Decision? ResolveWithinScope(Question q, string? option, SourcePacket source, BriefRevision brief) => null;
    public string? CandidateError(string candidate) => Encoding.UTF8.GetByteCount(candidate) is > 0 and <= 16384 && !candidate.Contains('\0')
        ? null : "Candidate is outside the bounded source format.";
    public IReadOnlyList<Check> LocalChecks(string candidate, Decision answer, SourcePacket source, string[] selectors) =>
        selectors is ["bounded-source"] ? [new("bounded-source", CandidateError(candidate) is null, "bounded-source")] : [];
    public string? VerificationError(Receipt r, Decision a, SourcePacket s) =>
        r.Checks.All(c => c.Assertion == c.Id) ? null : "Unexpected verification assertion.";
    public WorkerResult<string> Implement(Assignment a, SourcePacket s, Decision d, Receipt? feedback, PhaseContext? context) =>
        throw new CommandConflict("Hosted implementation requires live Codex.");
    public Receipt Verify(Assignment a, string candidate, Decision d, SourcePacket s, PhaseContext? context) =>
        throw new CommandConflict("Hosted verification requires independent CI.");
    public WorkerResult<Review> Assess(Assignment a, string candidate, Decision d, PhaseContext? context) =>
        throw new CommandConflict("Hosted review requires a fresh Codex session.");
}
