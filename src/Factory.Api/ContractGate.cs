namespace Factory;

public static class ContractGate
{
    public const int Version = 1;
    public const string VerificationRule = "controlled-verification/v2";

    public static bool MatchesTask(Assignment run, WorkItem task, string inputHash) =>
        run.SchemaVersion == Version && run.TaskId == task.Id && run.RunId != Guid.Empty
        && !string.IsNullOrWhiteSpace(run.Objective) && run.InputHash == inputHash
        && run.SourceHash == task.Source.Fingerprint() && run.PolicyVersion == task.Policy!.Version
        && run.RequiredChecks is not null && run.RequiredChecks.SequenceEqual(task.Policy.RequiredChecks);

    public static string? PlanError(WorkItem task, PlanEvidence? evidence)
    {
        var memory = task.Memory!;
        var sources = memory.Documents.Select(d => d.Path).ToHashSet(StringComparer.Ordinal);
        var writable = memory.Documents.Where(d => d.Part == SourcePart.Source).Select(d => d.Path).ToHashSet(StringComparer.Ordinal);
        if (evidence?.AffectedPaths is not { Length: > 0 } paths || !paths.All(writable.Contains)
            || evidence.Dependencies is null || !evidence.Dependencies.All(sources.Contains)
            || string.IsNullOrWhiteSpace(evidence.Risk) || evidence.BriefHash != GateInput.Hash(memory.Briefs[^1])
            || evidence.RequiredChecks is not { } checks || checks.Length != task.Policy!.RequiredChecks.Length
            || !task.Policy.RequiredChecks.ToHashSet(StringComparer.Ordinal).SetEquals(checks))
            return "Plan does not cover the admitted scope, checks, dependencies, risk and task brief.";
        return null;
    }

    public static string? PrerequisiteError(WorkItem task)
    {
        var prior = task.Gates.LastOrDefault();
        if (prior is not null && !prior.MatchesConsultation(task))
            return "Expert profile or advice differs from the recorded gate.";
        if (prior is not null && (prior.Repairs != task.Repairs || prior.Input.ProfileHash != GateInput.Hash(task.Policy)
            || prior.Input.SourceHash != task.Source.Fingerprint() || prior.NextBriefHash != GateInput.Hash(task.Memory!.Briefs[^1])))
            return "Task differs from its recorded repair count, brief, source or project policy.";
        if (task.Phase == Phase.Planning) return null;
        var planner = task.Assignments.LastOrDefault(a => a.Phase == Phase.Planning);
        if (planner is null || string.IsNullOrWhiteSpace(task.Plan)
            || !Accepted(task, planner, task.Plan, task.LegacyPlanner))
            return "Missing accepted plan from the admitted planner.";
        if (planner.RunId != task.LegacyPlanner && !task.Gates.Any(g => g.Outcome == GateOutcome.Pass
            && g.Input.OperationId == WorkOrder.OperationId(planner, WorkAction.Plan) && g.Input.SessionId is not null
            && g.ArtifactHash == Wire.Digest(task.Plan)
            && g.PlanEvidenceHash == (task.PlanningEvidence is null ? null : GateInput.Hash(task.PlanningEvidence))))
            return "Missing admitted planning gate.";
        if (task.Phase == Phase.Implementation) return null;
        var writer = task.Assignments.LastOrDefault(a => a.Phase == Phase.Implementation);
        if (writer is null || task.Writer != writer.RunId || task.Answer is null || string.IsNullOrWhiteSpace(task.Candidate)
            || !Accepted(task, writer, task.Candidate, task.LegacyWriter))
            return "Candidate has no matching accepted result from its admitted writer.";
        if (writer.RunId != task.LegacyWriter)
        {
            var pass = task.Gates.LastOrDefault(g => g.Input.OperationId == WorkOrder.OperationId(writer, WorkAction.Implement));
            if (pass?.Outcome != GateOutcome.Pass || pass.Input.SessionId is null || pass.ArtifactHash != Wire.Digest(task.Candidate) || !CurrentBindings(task, pass))
                return "The writer gate does not cover the current brief, source and policy.";
        }
        return null;
    }

    private static bool Accepted(WorkItem task, Assignment assignment, string artifact, Guid? historicalRun) =>
        (assignment.SchemaVersion == 0 && historicalRun == assignment.RunId)
        || (assignment.TaskId == task.Id && task.Results.Any(result => result.Assignment == assignment.Reference()
            && result.Status == ResultStatus.Completed && result.OutputHash == GateInput.Hash(artifact)));

    public static bool CurrentBindings(WorkItem task, GateEvaluation gate) =>
        gate.RuleVersion == GateEvaluation.Rule && gate.Input.TaskId == task.Id
        && gate.Input.SourceHash == task.Source.Fingerprint() && gate.Input.ProfileHash == GateInput.Hash(task.Policy)
        && gate.Input.BriefHash == GateInput.Hash(task.Memory!.Briefs[^1]);

    public static string? ReviewError(WorkItem task, Review review)
    {
        var receipt = task.Receipts.LastOrDefault();
        var evidence = review.Evidence;
        if (receipt is null || evidence is null || evidence.VerifierRunId != receipt.RunId
            || evidence.ChecksHash != GateInput.Hash(new VerificationFinding(receipt.RunId, receipt.CandidateHash, receipt.Checks))
            || evidence.BriefHash != GateInput.Hash(task.Memory!.Briefs[^1]))
            return "Reviewer did not identify the admitted brief and verification evidence.";
        if (evidence.Findings is null || evidence.Findings.Any(f => f is null || !Enum.IsDefined(f.Severity)
            || !task.Policy!.RequiredChecks.Contains(f.Criterion) || string.IsNullOrWhiteSpace(f.Evidence)
            || string.IsNullOrWhiteSpace(f.Recommendation))) return "Review findings lack scoped evidence or a recommendation.";
        if (review.Approved && evidence.Findings.Any(f => f.Severity == FindingSeverity.Blocking))
            return "Approved review contains an unresolved blocking finding.";
        return null;
    }

    public static string? ReceiptError(WorkItem task, Receipt? receipt)
    {
        // Select the admitted verifier from controller history, never by an ID offered in the receipt.
        var verifier = task.Assignments.LastOrDefault(a => a.Phase == Phase.Verification);
        if (receipt is null || verifier is null || task.Candidate is null)
            return "Missing verification receipt, assignment or candidate.";
        var digest = Wire.Digest(task.Candidate);
        if (!MatchesTask(verifier, task, digest))
            return "Verifier assignment does not match the pinned task, candidate or policy.";
        if (receipt.SchemaVersion != Version || receipt.GateVersion != VerificationRule || receipt.PolicyVersion != verifier.PolicyVersion)
            return "Verification receipt has an unsupported schema or stale policy version.";
        if (receipt.TaskId != task.Id || receipt.RunId != verifier.RunId)
            return "Verification receipt belongs to a different task or verifier run.";
        if (receipt.Candidate != task.Candidate || receipt.CandidateHash != digest || receipt.SourceHash != verifier.SourceHash)
            return "Verification receipt is bound to different source or candidate bytes.";
        if (receipt.Checks is null || receipt.Checks.Any(c => c is null || string.IsNullOrWhiteSpace(c.Id)
                || string.IsNullOrWhiteSpace(c.Assertion)) || receipt.Checks.Count != verifier.RequiredChecks.Length
            || !verifier.RequiredChecks.ToHashSet(StringComparer.Ordinal).SetEquals(receipt.Checks.Select(c => c.Id)))
            return "Verification receipt must contain each required check exactly once.";
        return null;
    }
}
