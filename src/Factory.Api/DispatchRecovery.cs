using System.Text.Json;

namespace Factory;

public record RuntimeWait(string Token, string Kind, string FundingMode = "Controlled");
public record RecoveryCommand(string OperationId, int GateSequence, string Note);
public record RecoveryView(string OperationId, int GateSequence, string Kind, string Reason, DateTimeOffset Since,
    string? SchedulerId, Guid? SessionId, string? RetainedOutputHash);

public sealed partial class TaskStore
{
    private const string MissingAcknowledgement = "Scheduler acknowledgement is unresolved. Reconcile the original operation before reissuing it.";
    private const string FailedRun = "Scheduler run ended without a recoverable result. Inspect its execution before reissuing work.";
    private const string ExpiredClaim = "Worker claim expired without a recorded result. Its execution requires reconciliation.";
    private long Deadline() => DateTimeOffset.UtcNow.AddSeconds(stallSeconds).ToUnixTimeSeconds();

    private void AdvanceDeadline(Session s, string id) =>
        s.Execute("UPDATE dispatch_watch SET deadline=@deadline WHERE id=@id", ("deadline", Deadline()), ("id", id));

    private static bool IsHeld(Session s, string id) =>
        s.Text("SELECT id FROM dispatch_holds WHERE id=@id", ("id", id)) is not null;

    private static void Park(Session s, WorkItem task, WorkOrder order, string kind, string reason)
    {
        BlockDispatch(s, task, order, reason);
        s.Execute("INSERT INTO dispatch_holds(id,kind,gate_seq) VALUES(@id,@kind,@seq)",
            ("id", order.Id), ("kind", kind), ("seq", task.Gates[^1].Sequence));
    }

    // Run independently of scheduler connectivity. A committed result always wins the race
    // with inspection, and only the currently admitted operation can park its task.
    public void ReconcileStalls() => database.Write(s =>
    {
        ExpireDiscussions(s);
        using var cmd = s.Command("""
            SELECT t.document,w.id FROM dispatch_watch w JOIN dispatch_outbox o ON o.id=w.id
            JOIN tasks t ON t.id=o.task_id LEFT JOIN dispatch_inbox i ON i.id=w.id
            WHERE t.activity='Running' AND i.output IS NULL
              AND (w.deadline<=@now OR i.deadline<=@now)
              AND NOT EXISTS (SELECT 1 FROM dispatch_holds h WHERE h.id=w.id)
            """, ("now", DateTimeOffset.UtcNow.ToUnixTimeSeconds()));
        var expired = new List<(WorkItem Task, string Operation)>();
        using (var rows = cmd.ExecuteReader())
            while (rows.Read()) expired.Add((Decode(rows.GetString(0)), rows.GetString(1)));
        foreach (var (task, id) in expired)
        {
            if (task.Run is null || WorkOrder.OperationId(task.Run, Lifecycle.ActionFor(task)) != id) continue;
            task.Gates = ReadGates(s, task.Id);
            if (lifecycle.Prepare(task) is not { } order)
            {
                if (task.Activity == Activity.Blocked) Save(s, task);
                continue;
            }
            if (RecordIntent(s, task, order, Encode(order))) InspectDeadline(s, task, order);
        }
        return expired.Count;
    });

    private static bool InspectDeadline(Session s, WorkItem task, WorkOrder order)
    {
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        if (s.Text("SELECT claim FROM dispatch_inbox WHERE id=@id AND output IS NULL AND deadline<=@now",
            ("id", order.Id), ("now", now)) is not null)
            Park(s, task, order, "Expired claim", ExpiredClaim);
        else if (s.Text("SELECT id FROM dispatch_watch WHERE id=@id AND deadline<=@now",
            ("id", order.Id), ("now", now)) is not null)
            Park(s, task, order, "No progress", "No submission, claim or result advanced this operation before its progress deadline.");
        else return false;
        return true;
    }

    // Only the three dispatch-reconciliation blockers emitted before FCT010 are recoverable.
    // Failed project gates and inconsistent envelopes must remain blocked.
    public void RestoreDispatchHolds() => database.Write(s =>
    {
        s.Execute("""
            INSERT INTO dispatch_watch(id,deadline)
            SELECT o.id,@deadline FROM dispatch_outbox o JOIN tasks t ON t.id=o.task_id WHERE t.activity='Running'
            ON CONFLICT(id) DO NOTHING
            """, ("deadline", Deadline()));
        using var cmd = s.Command("SELECT id FROM tasks WHERE activity='Blocked'");
        var ids = new List<Guid>();
        using (var rows = cmd.ExecuteReader())
            while (rows.Read()) ids.Add(Guid.Parse(rows.GetString(0)));
        foreach (var id in ids)
        {
            var gate = ReadGates(s, id).LastOrDefault();
            if (gate is not { Outcome: GateOutcome.Block, Input.OperationId: { } operation } ||
                gate.Input.OutputHash is not null) continue;
            var kind = gate.Reason switch
            {
                MissingAcknowledgement or FailedRun => "Outcome unknown",
                "Local worker stopped before recording a result. Its execution requires reconciliation." => "Expired claim",
                _ => null
            };
            if (kind is not null)
                s.Execute("""
                    INSERT INTO dispatch_holds(id,kind,gate_seq)
                    SELECT id,@kind,@seq FROM dispatch_outbox WHERE id=@id AND task_id=@task
                    ON CONFLICT(id) DO NOTHING
                    """, ("id", operation), ("task", id.ToString()), ("kind", kind), ("seq", gate.Sequence));
        }
        return ids.Count;
    });

    private static RecoveryView? Recovery(Session s, WorkItem task)
    {
        if (task.Activity != Activity.Blocked) return null;
        var gate = ReadGates(s, task.Id).LastOrDefault();
        if (gate is not { Outcome: GateOutcome.Block, Input.OperationId: { } id }) return null;
        using var cmd = s.Command("""
            SELECT h.kind,o.scheduler_id,i.output FROM dispatch_holds h JOIN dispatch_outbox o ON o.id=h.id
            LEFT JOIN dispatch_inbox i ON i.id=h.id
            WHERE h.id=@id AND h.gate_seq=@seq AND o.task_id=@task AND h.resolution IS NULL
            """, ("id", id), ("seq", gate.Sequence), ("task", task.Id.ToString()));
        using var row = cmd.ExecuteReader();
        return row.Read() ? new(id, gate.Sequence, row.GetString(0), gate.Reason, gate.EvaluatedAt,
            row.IsDBNull(1) ? null : row.GetString(1), gate.Input.SessionId,
            row.IsDBNull(2) ? null : Wire.Digest(row.GetString(2))) : null;
    }

    public bool ParkRuntime(string id, RuntimeWait body) => database.Write(s =>
    {
        if (string.IsNullOrWhiteSpace(body.Token)) throw new CommandConflict("The runtime wait requires a claim token.");
        if (body.FundingMode is not ("Controlled" or "Subscription"))
            throw new CommandConflict("Unsupported runtime funding mode for this wait.");
        var reason = body.Kind switch
        {
            "Authentication" => "Restore the runtime sign-in before resuming this task.",
            "Quota" => "Wait for the runtime allowance to reset before resuming this task.",
            "Budget" => "Resolve the runtime allocation before resuming this task.",
            _ => throw new BadHttpRequestException("Unsupported runtime wait.")
        };
        var raw = s.Text("""
            SELECT o.request FROM dispatch_outbox o
            JOIN dispatch_inbox i ON i.id=o.id WHERE o.id=@id AND i.claim=@token AND i.output IS NULL
            """, ("id", id), ("token", body.Token));
        if (raw is null) throw new CommandConflict("The runtime wait has no active claim.");
        var committed = JsonSerializer.Deserialize<WorkOrder>(raw, Wire.Json)!;
        var native = RequiresSubscription(committed);
        var subscription = native || body.FundingMode == "Subscription";
        if (subscription && body.Kind == "Budget")
            throw new CommandConflict("A subscription operation cannot report an API budget wait.");
        if (committed.Action == WorkAction.Discuss)
        {
            if (body.FundingMode != "Subscription" || !OpenDiscussion(s, id))
                throw new CommandConflict("The owner discussion requires an open subscription claim.");
            FinishDiscussion(s, id, body.Kind, body.Kind == "Quota"
                ? "The subscription allowance is exhausted. The owner question can be asked again after capacity returns."
                : "The owner discussion requires a valid subscription sign-in on this deployment.");
            return true;
        }
        var kind = s.Text("SELECT kind FROM dispatch_holds WHERE id=@id", ("id", id));
        if (kind == body.Kind)
        {
            var tagged = s.Text("SELECT id FROM subscription_waits WHERE id=@id", ("id",id)) is not null;
            if ((tagged || native) == subscription) return true;
            throw new CommandConflict("Runtime wait already has a different funding mode.");
        }
        if (kind is not null) throw new CommandConflict("This operation already has a different outcome.");
        var task = Decode(s.Text("SELECT t.document FROM tasks t JOIN dispatch_outbox o ON o.task_id=t.id WHERE o.id=@id", ("id",id))!);
        task.Gates = ReadGates(s, task.Id);
        if (lifecycle.Prepare(task) is not { } order || order.Id != id)
            throw new CommandConflict("The reported wait is for an inactive operation.");
        Park(s, task, order, body.Kind, reason);
        if (subscription)
            s.Execute("INSERT INTO subscription_waits(id,observed_after) VALUES(@id,@now)", ("id",id), ("now",Stamp()));
        return true;
    });

    public WorkItem Recover(Guid id, RecoveryCommand command) => database.Write(s =>
    {
        if (string.IsNullOrWhiteSpace(command.Note) || command.Note.Length > 2000 ||
            string.IsNullOrWhiteSpace(command.OperationId) || command.GateSequence < 1)
            throw new BadHttpRequestException("Recovery requires an operation, gate sequence and a short operator note.");
        command = command with { Note = command.Note.Trim() };
        var task = ReadTask(s, id);
        var operation = command.OperationId;
        var request = s.Text("SELECT request FROM dispatch_outbox WHERE id=@op AND task_id=@task",
            ("op", operation), ("task", id.ToString()));
        if (request is null) throw new CommandConflict("Operation does not belong to this task.");
        var previous = s.Text("SELECT resolution FROM dispatch_holds WHERE id=@id", ("id", operation));
        if (previous == Encode(command)) return task;
        if (previous is not null) throw new CommandConflict("This recovery already has a different resolution.");
        var hold = Recovery(s, task);
        if (hold is null || hold.OperationId != operation || hold.GateSequence != command.GateSequence)
            throw new CommandConflict("No matching dispatch wait is pending.");

        var order = JsonSerializer.Deserialize<WorkOrder>(request, Wire.Json)!;
        var since = s.Text("SELECT observed_after FROM subscription_waits WHERE id=@id", ("id",operation));
        // Older native waits may have been recorded without a funding tag. Their original gate dates the hold.
        var nativeFundingWait = RequiresSubscription(order) && hold.Kind is "Authentication" or "Quota" or "Budget";
        if (since is not null || nativeFundingWait)
        {
            var after = since is null ? hold.Since : DateTimeOffset.Parse(since);
            var observed = funding?.ReadSubscription();
            if (observed is null || observed.ObservedUtc <= after || observed.ObservedUtc < DateTimeOffset.UtcNow.AddMinutes(-5)
                || observed.ObservedUtc > DateTimeOffset.UtcNow
                || ((hold.Kind == "Quota" || nativeFundingWait) && (observed.Windows is not { Length: > 0 } || observed.Windows.Any(w => w.UsedPercent >= 100))))
                throw new CommandConflict("Refresh subscription metadata after this wait and confirm sign-in and available allowance before resuming.");
            s.Execute("""
                INSERT INTO subscription_waits(id,observed_after,clearance) VALUES(@id,@since,@receipt)
                ON CONFLICT(id) DO UPDATE SET clearance=excluded.clearance
                """, ("id",operation), ("since",after.ToString("O")), ("receipt",Encode(observed)));
        }
        task.Gates = ReadGates(s, id);
        var gate = task.Gates[^1];
        if (!ContractGate.CurrentBindings(task, gate) || !gate.MatchesConsultation(task) || gate.Repairs != task.Repairs ||
            order.Assignment.RunId != gate.Input.RunId || order.Id != operation || gate.Input.OutputHash is not null)
            throw new CommandConflict("The checkpoint differs from the parked operation.");

        // Validate the original admission and every prerequisite before authorizing a fresh fixture run.
        // The old operation stays fenced even after its successor starts.
        task.Gates.RemoveAt(task.Gates.Count - 1);
        task.Run = order.Assignment;
        task.Activity = Activity.Running;
        if (lifecycle.Prepare(task) is not { } original || !MatchesIntent(task, original, Encode(original), request))
            throw new CommandConflict("The original operation cannot be reconstructed from this checkpoint.");
        task.Gates.Add(gate);
        var input = GateInput.Capture(task, order);
        task.Record("Operator", $"Recovered {operation} from {hold.Kind}. {command.Note}");
        task.Run = null;
        task.Activity = Activity.Queued;
        GateEvaluation.Record(task, input, GateOutcome.Resume, "Operator authorized a fresh controlled attempt in the same phase.");
        s.Execute("UPDATE dispatch_holds SET resolution=@resolution WHERE id=@op",
            ("op", operation), ("resolution", Encode(command)));
        Save(s, task);
        return task;
    });
}
