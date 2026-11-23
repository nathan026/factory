namespace Factory;

public record RestoreRecord(string Id, string BackupId, string Revision, string RestoredUtc, string? ReleasedUtc, string? Reconciliation);
public record RestoreBlocker(string Kind, string Id);
public record RestoreRetirement(Guid RestoreId, string OperationId, string Note, bool SourceFenced);
public record RestoreRelease(Guid RestoreId, string Note, bool SourceFenced);

public sealed partial class TaskStore
{
    private static RestoreRecord? LatestRestore(Session s)
    {
        using var cmd = s.Command("""
            SELECT restore_id, backup_id, revision, restored_utc, released_utc, reconciliation
            FROM restore_history ORDER BY seq DESC LIMIT 1
            """);
        using var row = cmd.ExecuteReader();
        return row.Read() ? new(row.GetString(0), row.GetString(1), row.GetString(2), row.GetString(3),
            row.IsDBNull(4) ? null : row.GetString(4), row.IsDBNull(5) ? null : row.GetString(5)) : null;
    }

    public RestoreRecord? LatestRestore() => database.Read(LatestRestore);
    public List<RestoreBlocker> RestoreBlockers() => database.Read(RestoreBlockers);

    private static List<RestoreBlocker> RestoreBlockers(Session s)
    {
        // A scheduler submission can have reached Hatchet before its run ID was saved.
        using var cmd = s.Command("""
            SELECT 'Dispatch', o.id FROM dispatch_outbox o
            LEFT JOIN dispatch_inbox i ON i.id=o.id
            WHERE (o.first_submission IS NOT NULL OR o.scheduler_id IS NOT NULL)
              AND i.output IS NULL AND NOT EXISTS (SELECT 1 FROM dispatch_holds h WHERE h.id=o.id)
              AND NOT EXISTS (SELECT 1 FROM discussion_jobs j WHERE j.operation=o.id AND j.pending=0)
            UNION ALL
            SELECT 'CI', c.id FROM ci_runs c JOIN dispatch_inbox i ON i.id=c.id
            WHERE c.evidence IS NULL AND i.output IS NULL AND NOT EXISTS (SELECT 1 FROM dispatch_holds h WHERE h.id=c.id)
            UNION ALL
            SELECT 'Publication', task_id FROM github_publications
            WHERE receipt IS NULL AND (ref_attempted<>0 OR pr_attempted<>0)
            UNION ALL
            SELECT 'Funding', request_id FROM funding_attempts WHERE state IN ('InFlight','Unconfirmed','CancelPending')
            ORDER BY 1,2
            """);
        using var row = cmd.ExecuteReader();
        var blockers = new List<RestoreBlocker>();
        while (row.Read()) blockers.Add(new(row.GetString(0), row.GetString(1)));
        return blockers;
    }

    public bool RetireRestoredOperation(RestoreRetirement command) => database.Write(s =>
    {
        var record = LatestRestore(s);
        if (record is null || record.ReleasedUtc is not null || record.Id != command.RestoreId.ToString())
            throw new CommandConflict("The command needs the current paused restore.");
        var note = ReconciliationNote(command.SourceFenced, command.Note);
        var id = command.OperationId;
        if (string.IsNullOrWhiteSpace(id)) throw new BadHttpRequestException("Identify the interrupted operation.");
        var prior = s.Text("SELECT evidence FROM restore_retirements WHERE restore_id=@restore AND operation_id=@id",
            ("restore",record.Id), ("id",id));
        if (prior is not null) return prior == note ? true : throw new CommandConflict("A different retirement was recorded.");
        if (!RestoreBlockers(s).Any(b => b.Id == id && b.Kind is "Dispatch" or "CI"))
            throw new CommandConflict("Select an unresolved worker or CI operation.");
        var raw = s.Text("SELECT t.document FROM dispatch_outbox o JOIN tasks t ON t.id=o.task_id WHERE o.id=@id", ("id",id))!;
        var task = Decode(raw);
        task.Gates = ReadGates(s,task.Id);
        var stored = s.Text("SELECT request FROM dispatch_outbox WHERE id=@id", ("id",id))!;
        if (IsDiscussion(id)) FinishDiscussion(s, id, "Retired", "The operator fenced the prior owner discussion during restore.");
        else if (lifecycle.Prepare(task) is not { } order || order.Id != id || !MatchesIntent(task, order, Encode(order), stored))
            throw new CommandConflict("The operation does not match this task's active checkpoint.");
        else Park(s, task, order, "Restore reconciliation", "Operator fenced the previous execution during restore. " + note);
        s.Execute("INSERT INTO restore_retirements(restore_id,operation_id,evidence) VALUES(@restore,@id,@note)",
            ("restore",record.Id), ("id",id), ("note",note));
        return true;
    });

    private static string ReconciliationNote(bool fenced, string? evidence)
    {
        var note = evidence?.Trim();
        if (!fenced || string.IsNullOrEmpty(note) || note.Length > 2000)
            throw new BadHttpRequestException("Confirm the source and its leases are fenced and record the reconciliation evidence.");
        return note;
    }

    public RestoreRecord ReleaseRestore(RestoreRelease command) => database.Write(s =>
    {
        var record = LatestRestore(s);
        if (record is null || record.Id != command.RestoreId.ToString())
            throw new CommandConflict("The command does not identify this restore.");
        var note = ReconciliationNote(command.SourceFenced, command.Note);
        if (record.ReleasedUtc is not null)
            return record.Reconciliation == note ? record : throw new CommandConflict("This restore already has a different release.");
        if (RestoreBlockers(s).Count != 0) throw new CommandConflict("Unresolved external operations remain. Inspect the source and obtain a reconciled checkpoint.");
        s.Execute("UPDATE restore_history SET released_utc=@now,reconciliation=@note WHERE restore_id=@id",
            ("now",Stamp()), ("note",note), ("id",record.Id));
        return LatestRestore(s)!;
    });
}
