using System.Text.Json;
using Factory;

namespace Factory.Fixtures;

// A bounded project profile for the existing factory evidence renderer. The controller only interprets data.
public sealed class FactorySummaryProject(string version = FactorySummaryProject.Version) : IControlledProject
{
    public const string Version = "factory-summary/v1";
    public const string LiveVersion = "factory-summary/v2";
    public const string Target = "fixtures/self-development/task-summary.cjs";
    public const string Decisions = "fixtures/self-development/policy.json";
    public const string Cases = "docs/fct020-operator-evidence.json";
    private static readonly string Directory = Path.Combine(AppContext.BaseDirectory, "projects", "factory-summary");
    public CiProfile Ci { get; } = CiProfile.Load(Directory, version);
    public ProjectPolicy Policy => Ci.Rules.Policy;
    public string Goal => Ci.Rules.Goal;
    public ScenarioOption[] Scenarios => [new("render-repairs", Goal)];
    public bool Supports(string scenario) => scenario == "render-repairs";
    public SourcePacket Snapshot()
    {
        string Read(string path) => File.ReadAllText(Path.Combine(Directory, Path.GetFileName(path))).ReplaceLineEndings("\n");
        return new(Read("renderer-base.cjs"), Read(Decisions), Read(Cases));
    }
    public ProjectBrief Describe(SourcePacket source) => new(new(Policy.RequiredChecks.Select(id => new Criterion(id,
        id == "bounded-source" ? "Renderer fits the admitted source limit." : "Report positive and zero counts exactly and preserve legacy Markdown when repairs is absent.")).ToArray(),
        ["Preserve the existing export format and receipt history."], ["Only the scoped renderer can change."]),
        [SourceDocument.Capture(SourcePart.Source, Target, source), SourceDocument.Capture(SourcePart.Decisions, Decisions, source),
         SourceDocument.Capture(SourcePart.Cases, Cases, source)]);
    public WorkerResult<string> Plan(Assignment a, PhaseContext? context) => new(a.Reference(), ResultStatus.Completed, Goal,
        new([Target], a.RequiredChecks, [Decisions, Cases], "Legacy exports can omit the repair count.", GateInput.Hash(context!.Brief)));
    public Question AdmittedQuestion(string scenario) => new("legacy-output", "Should legacy exports preserve their original Markdown?", false);
    public WorkerResult<Question> Ask(Assignment a, string scenario, PhaseContext? context) =>
        new(a.Reference(), ResultStatus.NeedsInput, AdmittedQuestion(scenario));
    public Decision? Investigate(Question question, SourcePacket packet, BriefRevision? brief) =>
        question == AdmittedQuestion("render-repairs") && packet == Snapshot() && brief?.Goal == Goal
            ? new("renderer-scope", question.Subject, "Preserve legacy output as required by the admitted project brief.", "preserve") : null;
    public DecisionOption[] Choices(SourcePacket s) => [];
    public DecisionResolution ApplyChoice(string choice, SourcePacket s) => throw new CommandConflict("No reserved decision on this profile.");
    public ExpertProfile ExpertProfile => new("summary/v1", "Evidence export", [Decisions], 4096);
    public ExpertOpinion? Consult(ExpertRequest request) => null;
    public Decision? ResolveWithinScope(Question q, string? option, SourcePacket s, BriefRevision b) => null;
    public WorkerResult<string> Implement(Assignment a, SourcePacket source, Decision answer, Receipt? feedback, PhaseContext? context)
    {
        if (Policy.Version == LiveVersion) throw new InvalidOperationException("A live Codex writer is required.");
        // Deliberate controlled first submission tests that a real failed CI receipt returns to the same owner.
        var output = feedback is null ? source.Source : source.Source.Replace("for (const [i, receipt]",
            "if (Number.isInteger(task.repairs)) lines.splice(4, 0, \"Repairs: \" + task.repairs);\nfor (const [i, receipt]");
        return new(a.Reference(), ResultStatus.Completed, output);
    }
    public string? CandidateError(string candidate) => System.Text.Encoding.UTF8.GetByteCount(candidate) is > 0 and <= 16384 && !candidate.Contains('\0')
        ? null : "Candidate is outside the bounded renderer format.";
    public IReadOnlyList<Check> LocalChecks(string candidate, Decision answer, SourcePacket source, string[] selectors) =>
        selectors is ["bounded-source"] ? [new("bounded-source", CandidateError(candidate) is null, "bounded-source")] : [];
    public string? VerificationError(Receipt receipt, Decision answer, SourcePacket source) =>
        receipt.Checks.All(c => c.Assertion == c.Id) ? null : "Verifier reported an unknown assertion.";
    public Receipt Verify(Assignment a, string candidate, Decision answer, SourcePacket s, PhaseContext? context) =>
        throw new InvalidOperationException("Factory source requires separate Docker CI.");
    public WorkerResult<Review> Assess(Assignment a, string candidate, Decision answer, PhaseContext? context)
    {
        if (Policy.Version == LiveVersion) throw new InvalidOperationException("A live Codex reviewer is required.");
        var verified = context!.Findings.Last();
        var approved = verified.CandidateHash == Wire.Digest(candidate) && verified.Checks.All(c => c.Passed);
        return new(a.Reference(), ResultStatus.Completed, new(a.RunId, Wire.Digest(candidate), approved,
            "Controlled reviewer inspected the sealed renderer and source-bound verification.", new(verified.RunId,
                GateInput.Hash(verified), GateInput.Hash(context.Brief), [])));
    }
}
