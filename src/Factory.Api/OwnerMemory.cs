namespace Factory;

public record Criterion(string Id, string Text);
public record Requirements(Criterion[] Criteria, string[] Constraints, string[] Exclusions);
public record BriefRevision(int Version, string Goal, Requirements Requirements, string? Plan, Decision? AcceptedDecision);
public enum SourcePart { Source, Decisions, Cases }
public record SourceDocument(SourcePart Part, string Path, string Revision, string Digest, string Excerpt, bool Complete)
{
    public bool Matches(SourcePacket snapshot, string revision)
    {
        if (!Enum.IsDefined(Part) || string.IsNullOrWhiteSpace(Path) || Revision != revision || Excerpt is null) return false;
        var content = snapshot.Content(Part, Path);
        return Digest == Wire.Digest(content) && content.StartsWith(Excerpt, StringComparison.Ordinal)
            && Complete == (Excerpt.Length == content.Length);
    }

    public static SourceDocument Capture(SourcePart part, string path, SourcePacket snapshot)
    {
        var content = snapshot.Content(part, path);
        int length = Math.Min(content.Length, 512);
        if (length < content.Length && char.IsHighSurrogate(content[length - 1])) length--;
        return new(part, path, snapshot.Fingerprint(), Wire.Digest(content), content[..length], length == content.Length);
    }
}
public record ProjectBrief(Requirements Requirements, SourceDocument[] Documents);
public record DecisionResolution(Decision Decision, string? Supersedes);
public record DecisionRecord(Decision Decision, string QuestionSubject, string Actor, string Authority, Guid Scope,
    string Rationale, DateTimeOffset? RecordedAt, string[] Sources, int? HistoryEntry, string? Supersedes);
public record VerificationFinding(Guid RunId, string CandidateHash, List<Check> Checks);

// The checkpoint holds the evidence. Views and worker packets can be reconstructed without a runtime session.
public sealed class OwnerMemory
{
    public required SourceDocument[] Documents { get; init; }
    public List<BriefRevision> Briefs { get; init; } = [];
    public List<DecisionRecord> Decisions { get; init; } = [];
    public string? LegacyOperationId { get; init; }

    public static OwnerMemory Initialize(WorkItem task, ProjectBrief brief, bool legacy)
    {
        var memory = new OwnerMemory
        {
            Documents = brief.Documents,
            LegacyOperationId = legacy && task.Activity == Activity.Running && task.Run is { } run
                ? WorkOrder.OperationId(run, Lifecycle.ActionFor(task)) : null
        };
        memory.Briefs.Add(new(1, task.Goal, brief.Requirements, null, null));
        if (legacy && task.Plan is not null) memory.RecordPlan(task.Plan);
        if (legacy && task.Answer is not null)
            memory.RecordDecision(task, "Recovered from the stored task answer.", null);
        return memory;
    }

    public void RecordPlan(string plan)
    {
        var prior = Briefs[^1];
        Briefs.Add(prior with { Version = prior.Version + 1, Plan = plan });
    }

    public void RecordDecision(WorkItem task, string rationale, DateTimeOffset? recordedAt, string? supersedes = null,
        string? authority = null, string? actor = null)
    {
        var decision = task.Answer!;
        var subject = task.Question?.Subject ?? decision.Subject;
        actor ??= task.HumanChoice is null ? "Lifecycle owner" : "Operator";
        var entry = task.History.LastOrDefault(e => e.Actor == actor && e.Text.Contains(decision.Text, StringComparison.Ordinal));
        var record = new DecisionRecord(decision, subject, actor,
            authority ?? (task.HumanChoice is null ? "Accepted project decision" : "Reserved product decision"), task.Id,
            rationale, recordedAt, Documents.Select(d => d.Path).ToArray(), entry?.Number,
            supersedes ?? Decisions.LastOrDefault(d => d.QuestionSubject == subject)?.Decision.Id);
        Decisions.Add(record);
        var prior = Briefs[^1];
        Briefs.Add(prior with { Version = prior.Version + 1, AcceptedDecision = decision });
    }

    public bool Matches(WorkItem task)
    {
        var latest = Briefs.LastOrDefault();
        var revision = task.Source.Fingerprint();
        return latest is not null && latest.Goal == task.Goal && latest.Plan == task.Plan
            && latest.Version == Briefs.Count && latest.AcceptedDecision == task.Answer
            && latest.Requirements?.Criteria is { Length: > 0 }
            && Decisions.LastOrDefault()?.Decision == task.Answer && Decisions.All(d => d.Scope == task.Id)
            && Documents is { Length: > 0 } && Documents.All(d => d.Matches(task.Source, revision));
    }

    public PhaseContext Packet(WorkItem task, WorkAction action)
    {
        var findings = action switch
        {
            WorkAction.Review => Findings(task.Receipts.Where(r => r.SchemaVersion == ContractGate.Version)),
            WorkAction.Implement when task.Repairs > 0 => Findings(task.Receipts.TakeLast(1).Where(r => r.SchemaVersion == ContractGate.Version)),
            _ => []
        };
        var reviewFeedback = action == WorkAction.Implement && task.Gates.LastOrDefault() is
            { Outcome: GateOutcome.Revise, Input.Phase: Phase.Review } ? task.Review : null;
        return new(1, task.OwnerId, task.Run!.Reference(), action, Briefs[^1], task.Policy!, Decisions.ToArray(), Documents, findings, reviewFeedback);
    }

    private static VerificationFinding[] Findings(IEnumerable<Receipt> receipts) =>
        receipts.Select(r => new VerificationFinding(r.RunId, r.CandidateHash, r.Checks)).ToArray();

    public OwnerView View(WorkItem task)
    {
        var next = task.Activity switch
        {
            Activity.Waiting => "Await operator decision",
            Activity.Blocked => task.History.LastOrDefault()?.Text ?? "Inspect blocked task",
            Activity.Delivered => "Inspect delivery evidence",
            Activity.Running => task.Run?.Objective ?? "Reconcile assignment",
            _ => $"Admit {task.Phase} assignment"
        };
        WorkAction? pending = task.Activity is Activity.Queued or Activity.Running ? Lifecycle.ActionFor(task) : null;
        var operation = task.Run is { } run && pending is { } action ? WorkOrder.OperationId(run, action) : null;
        return new(task.Id, task.OwnerId, task.Phase, task.Activity, task.Run?.RunId, pending, operation, next, task.Plan,
            Briefs, Decisions, task.Answer is null ? task.Question : null, task.Advice, task.Policy,
            Documents, Findings(task.Receipts));
    }
}

public record PhaseContext(int SchemaVersion, Guid OwnerId, RunReference Assignment, WorkAction Action, BriefRevision Brief, ProjectPolicy Policy,
    DecisionRecord[] Decisions, SourceDocument[] Documents, VerificationFinding[] Findings,
    [property: System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
    Review? ReviewFeedback = null,
    [property: System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
    BlueprintGrant? Blueprint = null,
    [property: System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
    WorkerReply? OwnerReply = null);
public record OwnerView(Guid TaskId, Guid OwnerId, Phase Phase, Activity Activity, Guid? RunId, WorkAction? PendingAction, string? OperationId, string NextAction,
    string? Plan, List<BriefRevision> Briefs, List<DecisionRecord> Decisions, Question? OpenQuestion, Advice? Advice,
    ProjectPolicy? Policy, SourceDocument[] Documents, VerificationFinding[] Findings);
