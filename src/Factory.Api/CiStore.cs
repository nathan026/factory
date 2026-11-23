using System.Text.Json;
using System.Text.RegularExpressions;

namespace Factory;

public record CiBinding(Guid TaskId, Guid RunId, Guid ExecutionId, string RevisionKind, string Revision,
    string SourceHash, string CandidateHash, string ProfileVersion, string PolicyHash, string ScriptHash,
    string Environment, string OrderHash);
public record CiJob(string Id, string Token, CiBinding Binding, WorkOrder Order, string Script,
    [property: System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
    long? DeadlineUnix = null);
public record CiCheck(string Id, string Assertion, int ExitCode, string ContainerId, string OutputHash);
public record CiDelivery(string Token, CiBinding Binding, CiCheck[] Checks);
public record CiUnavailable(string Token, Guid ExecutionId, string Reason);
public record CiExecution(CiBinding Binding, CiCheck[] Checks);

public sealed partial class TaskStore
{
    public void BindProject(string profile) => database.Write(s =>
    {
        var prior = s.Text("SELECT profile FROM controller_project WHERE id=1");
        if ((prior is not null && prior != profile) || (prior is null && profile != "shipping/v1" &&
            s.Text("SELECT id FROM tasks LIMIT 1") is not null))
            throw new InvalidOperationException("This state belongs to a different project profile.");
        s.Execute("INSERT INTO controller_project(id,profile) VALUES(1,@profile) ON CONFLICT(id) DO NOTHING", ("profile", profile));
        return true;
    });

    private CiBinding BindCi(Session s, WorkOrder order, Guid execution)
    {
        var profile = ciProfile!;
        var assignment = order.Assignment;
        var raw = s.Text("SELECT origin FROM github_intake WHERE task_id=@task", ("task", assignment.TaskId.ToString()));
        var origin = raw is null ? null : JsonSerializer.Deserialize<IssueOrigin>(raw, Wire.Json);
        if (order.Candidate is null || Wire.Digest(order.Candidate) != assignment.InputHash ||
            order.Source.Fingerprint() != assignment.SourceHash || order.Context is null ||
            GateInput.Hash(profile.Rules.Policy) != GateInput.Hash(order.Context.Policy) ||
            (origin is not null && (origin.PolicyHash != profile.Hash || origin.SourceHash != assignment.SourceHash)))
            throw new CommandConflict("CI inputs differ from the admitted source, candidate or project policy.");
        return new(assignment.TaskId, assignment.RunId, execution, origin is null ? "snapshot" : "git",
            origin?.BaseSha ?? assignment.SourceHash, assignment.SourceHash, assignment.InputHash,
            assignment.PolicyVersion, profile.Hash, profile.Rules.ScriptHash, profile.Rules.Environment, GateInput.Hash(order));
    }

    private static int AdmittedCiRounds(Session s, Guid taskId) => int.Parse(s.Text(
        "SELECT CAST(COUNT(*) AS TEXT) FROM ci_runs WHERE task_id=@task", ("task", taskId.ToString()))!);

    private object? CiBudget(Session s, Guid taskId) => ciProfile?.Rules.Blueprint is { } policy
        ? new { admitted = AdmittedCiRounds(s, taskId), limit = policy.MaxCiRounds, secondsPerRound = policy.MaxCiSeconds } : null;

    private bool CheckCiBudget(Session s, WorkItem task, WorkOrder order)
    {
        if (ciProfile?.Rules.Blueprint is not { } blueprint ||
            s.Text("SELECT id FROM dispatch_inbox WHERE id=@id", ("id", order.Id)) is not null) return true;
        var rounds = AdmittedCiRounds(s, task.Id);
        if (rounds < blueprint.MaxCiRounds) return true;
        BlockDispatch(s, task, order, $"Project CI budget exhausted after {rounds} admitted rounds.");
        return false;
    }

    public CiJob? ClaimCi() => database.Write<CiJob?>(s =>
    {
        var id = s.Text("""
            SELECT o.id FROM dispatch_outbox o JOIN tasks t ON t.id=o.task_id
            WHERE t.activity='Running' AND o.id LIKE '%.Verify'
              AND NOT EXISTS (SELECT 1 FROM dispatch_inbox i WHERE i.id=o.id)
              AND NOT EXISTS (SELECT 1 FROM dispatch_holds h WHERE h.id=o.id)
            ORDER BY t.turn, o.id LIMIT 1
            """);
        if (id is null) return null;
        var execution = Guid.NewGuid();
        var claim = Claim(s, id, "ci." + execution.ToString("N"), execution, ci: true);
        if (claim is null) return null;
        var binding = BindCi(s, claim.Order, execution);
        s.Execute("INSERT INTO ci_runs(id,task_id,binding) VALUES(@id,@task,@binding)", ("id", id),
            ("task", binding.TaskId.ToString()), ("binding", Encode(binding)));
        long? deadline = ciProfile!.Rules.Blueprint is null ? null :
            long.Parse(s.Text("SELECT CAST(deadline AS TEXT) FROM dispatch_inbox WHERE id=@id", ("id",id))!);
        return new(id, claim.Token, binding, claim.Order, ciProfile.Script, deadline);
    });

    public bool CiUnavailable(string id, CiUnavailable issue) => database.Write(s =>
    {
        if (string.IsNullOrWhiteSpace(issue.Token)) throw new CommandConflict("Missing CI claim token.");
        var raw = s.Text("""
            SELECT c.binding FROM ci_runs c JOIN dispatch_inbox i ON c.id=i.id
            WHERE c.id=@id AND i.claim=@claim AND i.output IS NULL
            """, ("id", id), ("claim", issue.Token));
        if (raw is null || string.IsNullOrWhiteSpace(issue.Reason) || issue.Reason.Length > 512)
            throw new CommandConflict("Unknown CI claim or missing infrastructure finding.");
        var binding = JsonSerializer.Deserialize<CiBinding>(raw, Wire.Json)!;
        if (binding.ExecutionId != issue.ExecutionId) throw new CommandConflict("Different CI execution.");
        if (IsHeld(s, id)) return false;
        var task = ReadTask(s, binding.TaskId);
        task.Gates = ReadGates(s, task.Id);
        if (lifecycle.Prepare(task) is not { } order || order.Id != id)
            throw new CommandConflict("CI operation is no longer active.");
        Park(s, task, order, "CI unavailable", issue.Reason);
        return true;
    });

    public bool ReceiveCi(string id, CiDelivery delivery) => database.Write(s =>
    {
        if (string.IsNullOrWhiteSpace(delivery.Token)) throw new CommandConflict("Missing CI claim token.");
        var binding = delivery.Binding;
        if (binding is null || s.Text("SELECT binding FROM ci_runs WHERE id=@id", ("id", id)) != Encode(binding))
            throw new CommandConflict("Unknown or foreign CI execution binding.");
        var raw = s.Text("SELECT request FROM dispatch_outbox WHERE id=@id", ("id", id))!;
        var order = JsonSerializer.Deserialize<WorkOrder>(raw, Wire.Json)!;
        if (binding != BindCi(s, order, binding.ExecutionId))
            throw new CommandConflict("Provider receipt differs from the admitted operation.");
        var checks = delivery.Checks;
        if (checks is null || checks.Any(c => c is null || c.ExitCode is not (0 or 1) ||
            string.IsNullOrWhiteSpace(c.Assertion) || c.Assertion.Length > 1024 ||
            !HexDigest(c.ContainerId) || !HexDigest(c.OutputHash)) ||
            !checks.Select(c => c.Id).SequenceEqual(order.Assignment.RequiredChecks) ||
            checks.Select(c => c.ContainerId).Distinct().Count() != checks.Length)
            throw new CommandConflict("Provider receipt must cover every selector with a distinct container outcome.");
        var receipt = new Receipt(binding.TaskId, binding.RunId, order.Candidate!, binding.CandidateHash, binding.SourceHash,
            checks.Select(c => new Check(c.Assertion, c.ExitCode == 0, c.Id)).ToList(), order.Assignment.SchemaVersion,
            binding.ProfileVersion, ContractGate.VerificationRule);
        var evidence = Encode(new CiExecution(binding, checks));
        if (s.Execute("UPDATE ci_runs SET evidence=@e, receipt_hash=@h WHERE id=@id AND (evidence IS NULL OR evidence=@e)",
            ("e", evidence), ("h", GateInput.Hash(receipt)), ("id", id)) != 1)
            throw new CommandConflict("This execution already has different evidence.");
        return Receive(s, id, delivery.Token, JsonSerializer.SerializeToElement(receipt, Wire.Json));
    });

    private static bool HexDigest(string? value) => value is { Length: 64 } && Regex.IsMatch(value, "^[0-9a-f]{64}$");

    private bool CiMatches(Session s, WorkItem task, Guid run, Receipt receipt)
    {
        var id = $"{run:N}.Verify";
        if (s.Text("SELECT receipt_hash FROM ci_runs WHERE id=@id", ("id", id)) != GateInput.Hash(receipt)) return false;
        var order = JsonSerializer.Deserialize<WorkOrder>(s.Text("SELECT request FROM dispatch_outbox WHERE id=@id", ("id", id))!, Wire.Json)!;
        var session = s.Text("SELECT session_id FROM dispatch_sessions WHERE operation_id=@id", ("id", id));
        return order.Assignment.TaskId == task.Id && order.Source == task.Source && order.Candidate == task.Candidate &&
            GateInput.Hash(task.Policy) == GateInput.Hash(ciProfile!.Rules.Policy) && Guid.TryParse(session, out var execution) &&
            s.Text("SELECT binding FROM ci_runs WHERE id=@id", ("id", id)) == Encode(BindCi(s, order, execution));
    }

    private static List<CiExecution> CiEvidence(Session s, Guid taskId)
    {
        using var cmd = s.Command("SELECT evidence FROM ci_runs WHERE task_id=@id AND evidence IS NOT NULL", ("id", taskId.ToString()));
        using var rows = cmd.ExecuteReader();
        var results = new List<CiExecution>();
        while (rows.Read()) results.Add(JsonSerializer.Deserialize<CiExecution>(rows.GetString(0), Wire.Json)!);
        return results;
    }
}
