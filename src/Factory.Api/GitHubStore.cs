using System.Text.Json;

namespace Factory;

public record IssueObservation(long Id, string Title, string Body, string UpdatedAt, string Reason);
public record IssueOrigin(string ApiUrl, long RepoId, string Repository, int Number, long IssueId, string Base, string BaseSha,
    string BaseTree, string SourceHash, string Path, string Scenario, string ScopeHash, string? PolicyHash = null, Guid? LocalJournal = null);
public record DraftIntent(Guid TaskId, string Branch, string Marker, string Candidate, string CandidateHash,
    Guid Verifier, Guid Reviewer, string GateHash, DateTimeOffset OpenedAt,
    [property: System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
    CiPublication? Ci = null);
public record CiPublication(Guid ExecutionId, string PolicyHash, string Environment, string ReceiptHash);
public record DraftReceipt(int Number, string Url, string HeadSha);
public record RepositoryDeliveryInfo(string Repository, int Number, string IssueUrl, string BaseSha, string Eligibility, bool AwaitingConfirmation, DraftReceipt? Draft, string? Problem, bool RequiresReconciliation);
public record GitHubStatus(long Version, Guid? TaskId, IssueObservation? Observation, IssueOrigin? Origin, DraftReceipt? Draft);

public sealed partial class TaskStore
{
    public GitHubStatus GitHubStatusFor(long repo, int number) => database.Read(s => IssueStatus(s, repo, number));

    private static GitHubStatus IssueStatus(Session s, long repo, int number)
    {
        using var cmd = s.Command("""
            SELECT i.version,i.task_id,i.observation,i.origin,p.receipt FROM github_intake i
            LEFT JOIN github_publications p ON p.task_id=i.task_id WHERE i.repo_id=@repo AND i.issue_number=@number
            """, ("repo", repo), ("number", number));
        using var row = cmd.ExecuteReader();
        if (!row.Read()) return new(0, null, null, null, null);
        return new(row.GetInt64(0), row.IsDBNull(1) ? null : Guid.Parse(row.GetString(1)),
            JsonSerializer.Deserialize<IssueObservation>(row.GetString(2), Wire.Json),
            row.IsDBNull(3) ? null : JsonSerializer.Deserialize<IssueOrigin>(row.GetString(3), Wire.Json),
            row.IsDBNull(4) ? null : JsonSerializer.Deserialize<DraftReceipt>(row.GetString(4), Wire.Json));
    }

    public GitHubStatus ObserveIssue(long repo, int number, long version, IssueObservation observation,
        IssueOrigin? origin, SourcePacket? source) => database.Write(s =>
    {
        var prior = IssueStatus(s, repo, number);
        if (prior.Version != version) throw new CommandConflict("Concurrent issue poll. Read the current issue again.");
        if (!DateTimeOffset.TryParse(observation.UpdatedAt, out var updated) ||
            (prior.Observation is { } older && updated < DateTimeOffset.Parse(older.UpdatedAt)))
            throw new CommandConflict("Stale issue observation. Poll the canonical issue again.");
        var taskId = prior.TaskId;
        if (taskId is null && origin is not null && source is not null && observation.Reason == "Eligible")
        {
            var task = lifecycle.Start(Guid.NewGuid(), origin.Scenario, observation.Title + "\n\n" + observation.Body, source);
            task.Record("Controller", $"Admitted {origin.Repository}#{number} at {origin.BaseSha}.");
            Save(s, task);
            taskId = task.Id;
        }
        if (prior.Observation != observation || taskId != prior.TaskId)
        {
            s.Execute("""
                INSERT INTO github_intake(repo_id,issue_number,version,observation,eligible,task_id,origin)
                VALUES(@repo,@number,1,@observation,@eligible,@task,@origin)
                ON CONFLICT(repo_id,issue_number) DO UPDATE SET version=github_intake.version+1,
                observation=excluded.observation,eligible=excluded.eligible,task_id=excluded.task_id,origin=excluded.origin
                """, ("repo", repo), ("number", number), ("observation", Encode(observation)), ("eligible", observation.Reason == "Eligible" ? 1 : 0),
                ("task", (object?)taskId?.ToString() ?? DBNull.Value),
                ("origin", (prior.Origin ?? origin) is { } admitted ? Encode(admitted) : (object)DBNull.Value));
            if (prior.TaskId is { } admittedId)
                s.Execute("UPDATE dashboard_queue SET revision=revision+1 WHERE task_id=@id", ("id", admittedId.ToString()));
        }
        return IssueStatus(s, repo, number);
    });

    public DraftIntent? PrepareDraft(GitHubStatus status) => database.Write(s =>
    {
        var origin = status.Origin!;
        var latest = IssueStatus(s, origin.RepoId, origin.Number);
        if (latest.Version != status.Version || latest.Observation?.Reason != "Eligible" || latest.TaskId != status.TaskId)
            throw new CommandConflict("Issue admission changed.");
        var task = ReadTask(s, status.TaskId!.Value);
        if (task.Activity != Activity.Delivered) return null;
        task.Gates = ReadGates(s, task.Id);
        var intent = QualifyCandidate(s, task, origin);
        if (ciProfile is not null && (origin.PolicyHash != ciProfile.Hash ||
            !CiMatches(s, task, intent.Verifier, task.Receipts[^1])))
            throw new CommandConflict("Publication requires the provider receipt for this revision and candidate.");
        if (ciProfile is not null)
        {
            var evidence = CiEvidence(s, task.Id).Single(e => e.Binding.RunId == intent.Verifier);
            intent = intent with { Ci = new(evidence.Binding.ExecutionId, ciProfile.Hash, evidence.Binding.Environment, GateInput.Hash(evidence)) };
        }
        s.Execute("INSERT INTO github_publications(task_id,intent) VALUES(@id,@intent) ON CONFLICT(task_id) DO NOTHING",
            ("id", task.Id.ToString()), ("intent", Encode(intent)));
        if (s.Text("SELECT intent FROM github_publications WHERE task_id=@id", ("id", task.Id.ToString())) != Encode(intent))
            throw new CommandConflict("The sealed publication intent differs from the task.");
        return intent;
    });

    private static DraftIntent QualifyCandidate(Session s, WorkItem task, IssueOrigin origin)
    {
        if (task.Phase != Phase.Delivery || task.Candidate is null || task.Memory?.Matches(task) != true ||
            task.Source.Fingerprint() != origin.SourceHash || ContractGate.PrerequisiteError(task) is not null)
            throw new CommandConflict("The delivered candidate differs from its admitted brief, source or writer gate.");

        var verification = task.Gates.LastOrDefault(g => g.Input.Phase == Phase.Verification);
        if (task.Receipts.LastOrDefault() is not { Passed: true } receipt || ContractGate.ReceiptError(task, receipt) is not null ||
            verification is not { Outcome: GateOutcome.Pass, Input.SessionId: not null } ||
            verification.Input.RunId != receipt.RunId || verification.ReceiptHash != GateInput.Hash(receipt) ||
            !ContractGate.CurrentBindings(task, verification))
            throw new CommandConflict("The passing receipt and verifier gate must cover the delivered candidate.");

        var review = task.Gates.LastOrDefault();
        var assignment = task.Assignments.LastOrDefault(a => a.Phase == Phase.Review);
        var writer = task.Gates.LastOrDefault(g => g.Input.RunId == task.Writer && g.Input.Phase == Phase.Implementation);
        if (task.Review is not { Approved: true } reviewed || ContractGate.ReviewError(task, reviewed) is not null ||
            assignment is null || reviewed.RunId != assignment.RunId || reviewed.RunId == task.Writer ||
            review is not { Outcome: GateOutcome.Pass, Input.Phase: Phase.Review, Input.SessionId: not null, NextActivity: Activity.Delivered } ||
            review.Input.RunId != reviewed.RunId || !ContractGate.CurrentBindings(task, review) ||
            review.Input.CandidateHash != receipt.CandidateHash || writer?.Input.SessionId is null ||
            new[] { writer.Input.SessionId, verification.Input.SessionId, review.Input.SessionId }.Distinct().Count() != 3 ||
            !task.Results.Any(r => r.Assignment == assignment.Reference() && r.OutputHash == GateInput.Hash(reviewed)) ||
            !MatchesReviewSubmission(s, assignment, reviewed, review))
            throw new CommandConflict("Publication requires the original independent review of the verified candidate.");

        return new(task.Id, $"factory/{task.Id:N}", $"<!-- factory-publication:{task.Id:N} -->",
            task.Candidate, receipt.CandidateHash, receipt.RunId, reviewed.RunId, GateInput.Hash(review), task.OpenedAt!.Value);
    }

    private static bool MatchesReviewSubmission(Session s, Assignment assignment, Review review, GateEvaluation gate)
    {
        var expected = new WorkerResult<Review>(assignment.Reference(), ResultStatus.Completed, review);
        var canonical = GateInput.Hash(expected);
        if (gate.Input.OutputHash == canonical) return true;
        // Scheduler transports may reorder JSON keys. Bind both the original envelope and its typed contents.
        var raw = s.Text("SELECT output FROM dispatch_inbox WHERE id=@id AND applied=1", ("id", gate.Input.OperationId!));
        if (raw is null) return false;
        using var doc = JsonDocument.Parse(raw);
        return gate.Input.OutputHash == GateInput.Hash(doc.RootElement) &&
            canonical == GateInput.Hash(doc.RootElement.Deserialize<WorkerResult<Review>>(Wire.ResultJson));
    }

    public (DraftIntent Intent, string Head)? UnconfirmedDraft(Guid id) => database.Read<(DraftIntent, string)?>(s =>
    {
        using var command = s.Command("SELECT intent,head_sha FROM github_publications WHERE task_id=@id AND pr_attempted=1 AND receipt IS NULL", ("id", id.ToString()));
        using var row = command.ExecuteReader();
        return row.Read() ? (JsonSerializer.Deserialize<DraftIntent>(row.GetString(0), Wire.Json)!, row.GetString(1)) : null;
    });

    private static RepositoryDeliveryInfo? RepositoryDelivery(Session s, Guid id)
    {
        using var cmd = s.Command("""
            SELECT i.origin,i.observation,p.pr_attempted,p.receipt,COALESCE(p.problem,h.reason),h.reason FROM github_intake i
            LEFT JOIN github_publications p ON i.task_id=p.task_id
            LEFT JOIN github_publication_holds h ON h.task_id=p.task_id AND h.head_sha=p.head_sha WHERE i.task_id=@id
            """, ("id", id.ToString()));
        using var row = cmd.ExecuteReader();
        if (!row.Read()) return null;
        var origin = JsonSerializer.Deserialize<IssueOrigin>(row.GetString(0), Wire.Json)!;
        var webRoot = new Uri(origin.ApiUrl).IsLoopback ? origin.ApiUrl.TrimEnd('/') : "https://github.com";
        return new(origin.Repository, origin.Number, $"{webRoot}/{origin.Repository}/issues/{origin.Number}", origin.BaseSha,
            JsonSerializer.Deserialize<IssueObservation>(row.GetString(1), Wire.Json)!.Reason,
            !row.IsDBNull(2) && row.GetInt32(2) == 1 && row.IsDBNull(3) && row.IsDBNull(5),
            row.IsDBNull(3) ? null : JsonSerializer.Deserialize<DraftReceipt>(row.GetString(3), Wire.Json),
            row.IsDBNull(4) ? null : row.GetString(4), !row.IsDBNull(5) && row.IsDBNull(3));
    }

    public bool PublicationHeld(Guid id) => database.Read(s =>
        s.Text("SELECT h.task_id FROM github_publication_holds h JOIN github_publications p ON h.task_id=p.task_id AND h.head_sha=p.head_sha WHERE h.task_id=@id AND p.receipt IS NULL",
               ("id", id.ToString())) is not null);

    public void HoldPublication(Guid id, string reason) => database.Write(s =>
    {
        if (s.Execute("""
            INSERT INTO github_publication_holds(task_id,head_sha,reason,observed_utc)
            SELECT task_id,head_sha,@reason,@now FROM github_publications WHERE task_id=@id
              AND head_sha IS NOT NULL AND receipt IS NULL AND (pr_attempted=1 OR ref_attempted=1)
            ON CONFLICT(task_id) DO NOTHING
            """, ("reason", reason), ("now", DateTimeOffset.UtcNow.ToString("O")), ("id", id.ToString())) == 1)
            s.Execute("UPDATE dashboard_queue SET revision=revision+1 WHERE task_id=@id", ("id", id.ToString()));
        return true;
    });

    public bool OwnsBranch(Guid id) => database.Read(s =>
        s.Text("SELECT task_id FROM github_publications WHERE task_id=@id AND ref_attempted=1", ("id", id.ToString())) is not null);

    public string? DraftHead(Guid id) => database.Read(s => s.Text("SELECT head_sha FROM github_publications WHERE task_id=@id", ("id", id.ToString())));
    public string SealHead(Guid id, string sha) => database.Write(s =>
    {
        s.Execute("UPDATE github_publications SET head_sha=@sha WHERE task_id=@id AND head_sha IS NULL", ("id", id.ToString()), ("sha", sha));
        return s.Text("SELECT head_sha FROM github_publications WHERE task_id=@id", ("id", id.ToString()))!;
    });

    public bool ReservePublicationWrite(IssueOrigin origin, Guid id, long version, bool pullRequest) => database.Write(s =>
    {
        var current = IssueStatus(s, origin.RepoId, origin.Number);
        if (current.Version != version || current.Observation?.Reason != "Eligible" || current.TaskId != id)
            throw new CommandConflict("Issue eligibility changed before publication.");
        if (origin.LocalJournal is not null && pullRequest)
        {
            if (s.Execute("UPDATE github_publications SET pr_attempted=1,ref_attempted=1 WHERE task_id=@id AND pr_attempted=0",
                ("id", id.ToString())) == 1)
                s.Execute("UPDATE dashboard_queue SET revision=revision+1 WHERE task_id=@id", ("id", id.ToString()));
            return true; // The local Git transaction itself is create-only, including across simultaneous publishers.
        }
        var sql = pullRequest ? "UPDATE github_publications SET pr_attempted=1 WHERE task_id=@id AND pr_attempted=0"
            : "UPDATE github_publications SET ref_attempted=1 WHERE task_id=@id AND ref_attempted=0";
        var reserved = s.Execute(sql, ("id", id.ToString())) == 1;
        if (reserved) s.Execute("UPDATE dashboard_queue SET revision=revision+1 WHERE task_id=@id", ("id", id.ToString()));
        return reserved;
    });
    public void PublicationProblem(Guid id, string problem) => database.Write(s =>
    {
        if (s.Execute("UPDATE github_publications SET problem=@p WHERE task_id=@id AND receipt IS NULL AND (problem IS NULL OR problem<>@p)",
            ("p", problem), ("id", id.ToString())) == 1)
            s.Execute("UPDATE dashboard_queue SET revision=revision+1 WHERE task_id=@id", ("id", id.ToString()));
        return true;
    });

    public void RecordDraft(Guid id, DraftReceipt receipt) => database.Write(s =>
    {
        if (s.Execute("UPDATE github_publications SET receipt=@receipt,problem=NULL WHERE task_id=@id AND head_sha=@head AND pr_attempted=1 AND receipt IS NULL",
            ("receipt", Encode(receipt)), ("id", id.ToString()), ("head", receipt.HeadSha)) == 1)
        {
            var task = ReadTask(s, id);
            task.Record("GitHub publisher", $"Draft PR #{receipt.Number} at {receipt.HeadSha}: {receipt.Url}. Hosted checks pending inspection.");
            Save(s, task);
        }
        else if (s.Text("SELECT receipt FROM github_publications WHERE task_id=@id", ("id", id.ToString())) != Encode(receipt))
            throw new CommandConflict("Publication acknowledgement conflicts with the sealed head or recorded Draft.");
        return true;
    });
}
