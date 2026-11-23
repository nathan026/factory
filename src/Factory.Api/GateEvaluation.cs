using System.Text.Json;
using System.Text.Json.Serialization;

namespace Factory;

public enum GateOutcome { Pass, Revise, Block, Escalate, Resume }
public record GateCriterion(string Id, bool Passed, string Finding);

// Evidence is captured before applying a result, including the brief that the worker actually received.
public record GateInput(Guid TaskId, Phase Phase, Guid? RunId, string? OperationId, Guid? SessionId,
    int? BriefVersion, string? BriefHash, string? ProfileVersion, string? ProfileHash,
    string SourceHash, string? InputHash, string? CandidateHash, string? OutputHash)
{
    public static string Hash<T>(T value) => Wire.Digest(JsonSerializer.Serialize(value, Wire.Json));

    public static GateInput Capture(WorkItem task, WorkOrder? order = null, Guid? session = null, JsonElement? output = null)
    {
        var brief = task.Memory?.Briefs.LastOrDefault();
        var run = order?.Assignment ?? task.Run;
        return new(task.Id, task.Phase, run?.RunId, order?.Id, session,
            brief?.Version, brief is null ? null : Hash(brief), task.Policy?.Version,
            task.Policy is null ? null : Hash(task.Policy), task.Source.Fingerprint(), run?.InputHash,
            task.Candidate is null ? null : Wire.Digest(task.Candidate), output is null ? null : Hash(output.Value));
    }
}

public record GateEvaluation(int Sequence, string RuleVersion, DateTimeOffset EvaluatedAt, GateInput Input,
    GateOutcome Outcome, string? ArtifactHash, string? PlanEvidenceHash, string? NextBriefHash, string? ReceiptHash, GateCriterion[] Criteria, string Reason, Phase NextPhase, Activity NextActivity, int Repairs,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? ExpertProfileHash = null,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? AdviceHash = null)
{
    public const string Rule = "controlled-phase-gates/v1";

    public bool MatchesConsultation(WorkItem task) =>
        (ExpertProfileHash is null || ExpertProfileHash == GateInput.Hash(task.ExpertProfile))
        && (AdviceHash is null || AdviceHash == GateInput.Hash(task.Advice));

    public static void Record(WorkItem task, GateInput input, GateOutcome outcome, string reason,
        IEnumerable<GateCriterion>? checks = null, Receipt? receipt = null)
    {
        var criteria = checks?.ToList() ?? [];
        criteria.Add(new("phase-contract", outcome == GateOutcome.Pass, reason));
        var artifact = input.Phase == Phase.Planning ? task.Plan : task.Candidate;
        var brief = task.Memory?.Briefs.LastOrDefault();
        task.Gates.Add(new(task.Gates.Count + 1, Rule, DateTimeOffset.UtcNow, input, outcome,
            artifact is null ? null : Wire.Digest(artifact),
            task.PlanningEvidence is null ? null : GateInput.Hash(task.PlanningEvidence),
            brief is null ? null : GateInput.Hash(brief), receipt is null ? null : GateInput.Hash(receipt), criteria.ToArray(),
            reason, task.Phase, task.Activity, task.Repairs,
            task.ExpertProfile is null ? null : GateInput.Hash(task.ExpertProfile),
            task.Advice?.Consultation is null ? null : GateInput.Hash(task.Advice)));
    }
}
