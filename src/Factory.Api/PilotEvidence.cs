using System.IO.Compression;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Factory;

public sealed partial class TaskStore
{
    // Portable evidence uses structured fields. Operator notes, prompts, raw worker output and credentials stay local.
    private static string? DigestField(string? value) =>
        value is { Length: 64 } && value.All(char.IsAsciiHexDigit) ? value : null;

    private sealed record WaitInterval(int Gate, Activity Kind, DateTimeOffset Start, DateTimeOffset? End, double ObservedSeconds);

    private static List<WaitInterval> Waits(List<GateEvaluation> gates, DateTimeOffset captured)
    {
        var result = new List<WaitInterval>();
        GateEvaluation? start = null;
        foreach (var gate in gates)
        {
            if (start is not null && gate.NextActivity != start.NextActivity)
            {
                result.Add(new(start.Sequence, start.NextActivity, start.EvaluatedAt, gate.EvaluatedAt,
                    (gate.EvaluatedAt - start.EvaluatedAt).TotalSeconds));
                start = null;
            }
            if (gate.NextActivity is Activity.Blocked or Activity.Waiting) start ??= gate;
        }
        if (start is not null) result.Add(new(start.Sequence, start.NextActivity, start.EvaluatedAt, null,
            (captured - start.EvaluatedAt).TotalSeconds));
        return result;
    }

    public byte[] PilotEvidence(Guid id)
    {
        var files = database.Read(s =>
        {
            var task = ReadTask(s, id);
            var gates = ReadGates(s, id);
            var captured = DateTimeOffset.UtcNow;
            var project = newFixtureSession();
            var known = project.Snapshot();
            var archive = new Dictionary<string,string>();
            var workerProfile = project.Ci?.RequiresCodex == true
                ? "Codex-required profile" : task.ExpertRuntime is not null ? "Controlled coding with subscription expert" : "Controlled fixture";
            var markdown = new StringBuilder($"# Task {id:D}\n\n{workerProfile} · {task.Phase} · {task.Activity}\n\n");
            var sources = Enum.GetValues<SourcePart>().Select(part =>
            {
                var content = task.Source.Content(part);
                var path = content == known.Content(part) ? $"sources/{part}.json" : null;
                if (path is not null) archive.Add(path, content);
                return new { part, digest = Wire.Digest(content), path };
            }).ToArray();
            var allowedChecks = project.Policy.RequiredChecks.ToHashSet();
            var receipts = task.Receipts.Select((r, i) =>
            {
                var path = $"receipts/{i+1}.json";
                var status = task.RejectedReceipt?.Index == i ? "Rejected" : r.Passed ? "Passed" : "Failed";
                var evidence = new { r.TaskId, r.RunId, candidateHash = DigestField(r.CandidateHash),
                    sourceHash = DigestField(r.SourceHash), status, checks = r.Checks?.Where(c => c is not null).Select(c =>
                        new { id = allowedChecks.Contains(c.Id) ? c.Id : null, c.Passed }) };
                archive.Add(path, JsonSerializer.Serialize(evidence, Wire.Json));
                markdown.AppendLine($"- Verification {i+1} · [{status}]({path}) · run {r.RunId:D}");
                return new { index = i+1, status, path, digest = Wire.Digest(archive[path]), evidence.candidateHash };
            }).ToArray();
            var reconstructed = task.Memory is null;
            var memory = lifecycle.Memory(task);
            var decisions = memory.Decisions.Select(d =>
            {
                var actor = d.Actor is "Operator" or "Lifecycle owner" || Regex.IsMatch(d.Actor, @"^Slack T[A-Z0-9]+/[UW][A-Z0-9]+\z")
                    ? d.Actor : "Unidentified";
                var authority = d.Authority is "Reserved product decision" or "Accepted project decision" or "Delegated implementation decision"
                    ? d.Authority : "Unidentified";
                // Only publish decision prose already present in the trusted installed project catalog.
                var question = project.Supports(task.Scenario) ? project.AdmittedQuestion(task.Scenario) : null;
                var original = question is null ? null : project.Investigate(question, known);
                var recognized = original == d.Decision || project.Choices(known).Any(c => project.ApplyChoice(c.Value, known).Decision == d.Decision);
                var description = recognized ? d.Decision.Text : null;
                markdown.AppendLine($"- Decision · {actor} · {authority} · {description ?? "See decision fingerprint in task.json"}");
                return new { actor, authority, description, d.Scope, d.RecordedAt, d.HistoryEntry, decisionHash = GateInput.Hash(d.Decision) };
            }).ToArray();
            var waits = Waits(gates, captured);
            var gateSummary = gates.Select(g => new { g.Sequence, g.EvaluatedAt, g.Outcome, g.NextActivity, g.Input.Phase,
                g.Input.RunId, sourceHash = DigestField(g.Input.SourceHash), candidateHash = DigestField(g.Input.CandidateHash),
                receiptHash = DigestField(g.ReceiptHash), g.Repairs,
                submittedCandidateHash = g.Criteria.Any(c => c.Id == "local/candidate-shape" && c.Passed) ? DigestField(g.ArtifactHash) : null,
                localChecks = g.Criteria.Where(c => ciProfile?.Rules.Blueprint is { } policy &&
                    (c.Id == "local/candidate-shape" || policy.EarlyChecks.Any(id => c.Id == "local/" + id)))
                    .Select(c => new { c.Id, c.Passed }).ToArray() }).ToArray();
            var repo = RepositoryDelivery(s,id);
            var costs = Used(s,null,id);
            var host = funding?.InfrastructureCost;
            var candidate = task.Candidate is null ? null : Wire.Digest(task.Candidate);
            var questions = task.Results.Where(r => r.Status == ResultStatus.NeedsInput).ToArray();
            var end = task.Activity == Activity.Delivered ? gates.LastOrDefault()?.EvaluatedAt : captured;
            double? elapsed = end is { } last && task.OpenedAt is { } opened && last >= opened ? (last-opened).TotalSeconds : null;
            var ownerDiscussion = task.AskingRun is not null ||
                s.Text("SELECT operation FROM discussion_jobs WHERE task_id=@id LIMIT 1", ("id",id.ToString())) is not null;
            var evidenceClass = project.Ci?.RequiresCodex == true ? "codex-required" : task.ExpertRuntime is not null ? "subscription-expert"
                : ownerDiscussion ? "subscription-owner" : "controlled";
            var claims = ClaimCounts(s,id);
            var snapshot = new { format = "factory-task-evidence/v1", evidenceClass, taskId = id, capturedUtc = captured,
                controlledWorkerClaims = claims.Controlled, subscriptionWorkerClaims = claims.Subscription, ciClaims = claims.Ci,
                task.OpenedAt, observedElapsedSeconds = elapsed, installedProject = project.Policy.Version,
                scope = task.Goal == project.Goal ? project.Goal : "Private task goal", goalHash = Wire.Digest(task.Goal), task.Phase, task.Activity,
                task.ContractVersion, recordedPolicyVersion = task.Policy?.Version == project.Policy.Version ? task.Policy.Version : null,
                recordedPolicyHash = task.Policy is null ? null : GateInput.Hash(task.Policy),
                sourceHash = task.Source.Fingerprint(), sources, candidateHash = candidate, task.Repairs, decisions, decisionMemoryReconstructed = reconstructed,
                interventions = new { questionOutputs = questions.Length,
                    repeatedQuestionOutputs = questions.GroupBy(q => q.OutputHash).Sum(group => group.Count()-1),
                    reservedEscalations = gates.Count(g => g.Outcome == GateOutcome.Escalate && g.NextActivity == Activity.Waiting),
                    recordedOperatorDecisions = decisions.Count(d => d.authority == "Reserved product decision"),
                    checkpointRecoveries = gates.Count(g => g.Outcome == GateOutcome.Resume) },
                gates = gateSummary, waits, receipts, ciBudget = CiBudget(s,id),
                ci = CiEvidence(s,id).Select(c => new { c.Binding.ExecutionId, c.Binding.RunId, candidateHash = DigestField(c.Binding.CandidateHash),
                    checks = c.Checks.Select(k => new { id = allowedChecks.Contains(k.Id) ? k.Id : null, k.ExitCode, outputHash = DigestField(k.OutputHash) }) }),
                review = task.Review is { } v ? new { v.RunId, v.Approved, candidateHash = DigestField(v.CandidateHash) } : null,
                publication = repo is null ? null : new { issueNumber = repo.Number, draftNumber = repo.Draft?.Number,
                    transport = new Uri(repo.IssueUrl).IsLoopback ? "loopback" : "hosted",
                    baseSha = GitSha(repo.BaseSha), headSha = GitSha(repo.Draft?.HeadSha), repo.AwaitingConfirmation },
                costs = new { simulatedEstimatedUsdMicros = costs.EstimatedMicros, simulatedHeldUsdMicros = costs.HeldMicros,
                    taskCostUsdMicros = (long?)null, infrastructureObservation = host is null ? null : new { host.UsdMicros, host.AsOf, scope = "host" } },
                activeOperatorMinutes = (double?)null, independentHumanReview = (bool?)null };
            if (evidenceClass != "controlled")
                archive.Add("codex-attempts.json",JsonSerializer.Serialize(new { fundingMode="subscription",
                    attempts=CodexEvidence.Attempts(id) },Wire.Json));
            archive.Add("task.json", JsonSerializer.Serialize(snapshot, Wire.Json));
            markdown.AppendLine($"\nSubstantive repairs recorded: {task.Repairs}. Wait intervals observed in the gate journal: {waits.Count}.");
            foreach (var wait in waits)
                markdown.AppendLine($"- Gate {wait.Gate} · {wait.Kind} · {wait.Start:O} to {wait.End?.ToString("O") ?? "still open at export"}");
            markdown.AppendLine($"\nSimulated API usage: {costs.EstimatedMicros} USD micros. Task-attributable costs and active operator minutes are unmeasured.");
            markdown.AppendLine($"\nSource fingerprint: `{task.Source.Fingerprint()}`. Candidate: `{candidate ?? "unavailable"}`.");
            markdown.AppendLine("\n[Structured decisions, gates, checks and delivery evidence](task.json)");
            archive.Add("summary.md", markdown.ToString());
            return archive;
        });
        using var bytes = new MemoryStream();
        using (var zip = new ZipArchive(bytes, ZipArchiveMode.Create, leaveOpen:true))
            foreach (var (path, content) in files)
            {
                using var stream = zip.CreateEntry(path, CompressionLevel.Fastest).Open();
                stream.Write(Encoding.UTF8.GetBytes(content));
            }
        return bytes.ToArray();
    }

    private static string? GitSha(string? sha) => sha is { Length: 40 } && sha.All(char.IsAsciiHexDigit) ? sha : null;
}
