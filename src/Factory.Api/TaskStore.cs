using System.Text.Json;

namespace Factory;

public record DispatchTicket(bool Advanced, string? Id = null, string? SchedulerId = null);
public record DispatchClaim(string Token, WorkOrder Order, string WorkerMode, int? CodingSeconds = null);

public sealed partial class TaskStore(FactoryDatabase database, Lifecycle lifecycle, Func<IControlledProject> newFixtureSession, int stallSeconds = 120, CiProfile? ciProfile = null, FundingProfile? funding = null, bool liveOwner = false, CiProfile? previousProfile = null, int codingSeconds = 60)
{
    public List<WorkItem> List()
    {
        using var db = database.Open();
        using var cmd = db.CreateCommand();
        cmd.CommandText = "SELECT document FROM tasks WHERE NOT EXISTS (SELECT 1 FROM github_scope_discussions d WHERE d.task_id=tasks.id) ORDER BY " + (db is Microsoft.Data.Sqlite.SqliteConnection ? "rowid" : "turn") + " DESC";
        using var rows = cmd.ExecuteReader();
        var result = new List<WorkItem>();
        while (rows.Read()) result.Add(Decode(rows.GetString(0)));
        return result;
    }

    private WorkItem Load(Guid id)
    {
        WorkItem task;
        using (var db = database.Open())
        {
            using var cmd = db.CreateCommand();
            cmd.CommandText = "SELECT document FROM tasks WHERE id=@id";
            var parameter = cmd.CreateParameter();
            parameter.ParameterName = "id";
            parameter.Value = id.ToString();
            cmd.Parameters.Add(parameter);
            if (cmd.ExecuteScalar() is not string raw) throw new KeyNotFoundException();
            task = Decode(raw);
        }
        if (task.Memory is not null) return task;
        // Pin the reconstructed brief on first access, including parked and delivered legacy tasks.
        return database.Write(s =>
        {
            var current = Decode(s.Text("SELECT document FROM tasks WHERE id=@id", ("id", id.ToString()))!);
            if (current.Memory is null)
            {
                lifecycle.Memory(current);
                s.Execute("UPDATE tasks SET document=@doc WHERE id=@id", ("doc", Encode(current)), ("id", id.ToString()));
            }
            return current;
        });
    }

    public List<GateEvaluation> Gates(Guid id)
    {
        using var db = database.Open();
        var session = new Session(db, null);
        if (session.Text("SELECT id FROM tasks WHERE id=@id", ("id", id.ToString())) is null)
            throw new KeyNotFoundException();
        return ReadGates(session, id);
    }

    public OwnerView Owner(Guid id)
    {
        var task = Load(id);
        return lifecycle.Memory(task).View(task);
    }

    public object Document(Guid id, SourcePart part, string? path = null)
    {
        var task = Load(id);
        var document = lifecycle.Memory(task).Documents.FirstOrDefault(d => d.Part == part && (path is null || path == d.Path)) ?? throw new KeyNotFoundException();
        var content = task.Source.Content(part, document.Path);
        if (document.Digest != Wire.Digest(content) || document.Revision != task.Source.Fingerprint())
            throw new CommandConflict("The stored document does not match its pinned reference.");
        return new { document, content };
    }

    public WorkItem Create(Guid id, string scenario, string? openingMessage = null) => Update(id, existing =>
    {
        if (existing is null) return lifecycle.Start(id, scenario, openingMessage);
        if (existing.Scenario != scenario || existing.OpeningMessage != openingMessage) throw new CommandConflict("Task ID already has a different request.");
        return existing;
    });

    public WorkItem Decide(Guid id, string choice, string? questionKey = null) => Update(id, existing =>
    {
        if (existing is null) throw new KeyNotFoundException();
        if (!lifecycle.Choices(existing).Any(c => c.Value == choice)) throw new BadHttpRequestException("Unknown decision option.");
        if (questionKey is not null && QuestionKey(existing) != questionKey)
            throw new CommandConflict("The decision question has changed. Refresh the task.");
        lifecycle.Decide(existing, choice);
        return existing;
    });

    public bool Tick()
    {
        var ticket = Next();
        if (ticket.Id is null || ticket.Advanced) return ticket.Advanced;
        var worker = Claim(ticket.Id, "local." + ticket.Id, Guid.NewGuid());
        if (worker is null) return false;
        var output = worker.Order.Execute(newFixtureSession());
        try { Receive(ticket.Id, worker.Token, output); }
        catch (CommandConflict) when (database.Read(s => IsHeld(s, ticket.Id))) { return false; }
        return true;
    }

    public DispatchTicket Next(bool remote = false) => database.Write<DispatchTicket>(s =>
    {
        var raw = s.Text("""
            SELECT document FROM tasks WHERE activity='Running' OR (activity='Queued' AND NOT EXISTS (
                SELECT 1 FROM github_intake i WHERE i.task_id=tasks.id AND i.eligible=0))
            ORDER BY CASE activity WHEN 'Running' THEN 0 ELSE 1 END, turn, id LIMIT 1
            """);
        var task = raw is null ? null : Decode(raw);
        if (remote && task?.Activity != Activity.Running && DiscussionTicket(s) is { } discussion) return discussion;
        if (task is null) return new(false);
        task.Gates = ReadGates(s, task.Id);
        var order = lifecycle.Prepare(task);
        var changed = Encode(task) != raw;
        if (changed) Save(s, task);
        if (order is null) return new(changed);

        var request = Encode(order);
        if (!RecordIntent(s, task, order, request)) return new(true);
        var output = s.Text("SELECT output FROM dispatch_inbox WHERE id=@id AND applied=0", ("id", order.Id));
        if (output is not null)
        {
            using var document = JsonDocument.Parse(output);
            var session = s.Text("SELECT session_id FROM dispatch_sessions WHERE operation_id=@id", ("id", order.Id));
            var providerVerified = ciProfile is not null && order.Action == WorkAction.Verify &&
                CiMatches(s, task, order.Assignment.RunId, document.RootElement.Deserialize<Receipt>(Wire.Json)!);
            lifecycle.Apply(task, order, document.RootElement, session is null ? null : Guid.Parse(session), providerVerified);
            s.Execute("UPDATE dispatch_inbox SET applied=1 WHERE id=@id", ("id", order.Id));
            // A resolved question can make a second action eligible within the same assignment.
            var continuation = task.Activity == Activity.Running ? lifecycle.Prepare(task) : null;
            Save(s, task);
            if (continuation is not null) RecordIntent(s, task, continuation, Encode(continuation));
            return new(true);
        }

        if (ciProfile is not null && order.Action == WorkAction.Verify)
            return new(!CheckCiBudget(s, task, order) || InspectDeadline(s, task, order) || changed);

        var scheduler = s.Text("SELECT scheduler_id FROM dispatch_outbox WHERE id=@id", ("id", order.Id));
        if (remote && scheduler is null)
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            s.Execute("UPDATE dispatch_outbox SET first_submission=@now WHERE id=@id AND first_submission IS NULL",
                ("now", now), ("id", order.Id));
            // Leave a margin inside the scheduler's 24-hour deduplication window.
            if (s.Text("SELECT id FROM dispatch_outbox WHERE id=@id AND first_submission<=@cutoff",
                ("id", order.Id), ("cutoff", now - 23 * 3600)) is not null)
            {
                Park(s, task, order, "Outcome unknown", MissingAcknowledgement);
                return new(true);
            }
        }
        if (InspectDeadline(s, task, order)) return new(true);
        return new(changed, order.Id, scheduler);
    });

    private static void BlockDispatch(Session s, WorkItem task, WorkOrder order, string reason)
    {
        var session = s.Text("SELECT session_id FROM dispatch_sessions WHERE operation_id=@id", ("id", order.Id));
        var input = GateInput.Capture(task, order, session is null ? null : Guid.Parse(session));
        Lifecycle.Block(task, reason);
        GateEvaluation.Record(task, input, GateOutcome.Block, reason);
        Save(s, task);
    }

    private bool RecordIntent(Session s, WorkItem task, WorkOrder order, string request)
    {
        s.Execute("""
            INSERT INTO dispatch_outbox(id,task_id,run_id,request) VALUES(@id,@task,@run,@request)
            ON CONFLICT(id) DO NOTHING
            """, ("id", order.Id), ("task", task.Id.ToString()), ("run", order.Assignment.RunId.ToString()), ("request", request));
        var committed = s.Text("SELECT request FROM dispatch_outbox WHERE id=@id", ("id", order.Id));
        if (MatchesIntent(task, order, request, committed))
        {
            s.Execute("INSERT INTO dispatch_watch(id,deadline) VALUES(@id,@deadline) ON CONFLICT(id) DO NOTHING",
                ("id", order.Id), ("deadline", Deadline()));
            return true;
        }
        BlockDispatch(s, task, order, "Current inputs differ from the committed dispatch.");
        return false;
    }

    // A pre-FCT006 operation may already have been submitted or produced a result.
    // Only its original run can complete from the exact earlier envelope.
    private static bool MatchesIntent(WorkItem task, WorkOrder order, string expected, string? committed) =>
        committed == expected || (task.Memory?.LegacyOperationId == order.Id
            && committed == Encode(order with { Context = null, Feedback = task.Receipts.LastOrDefault() }));

    public bool Submitted(string id, string schedulerId) => database.Write(s => Bind(s, id, schedulerId));

    private bool Bind(Session s, string id, string schedulerId)
    {
        if (string.IsNullOrWhiteSpace(schedulerId)) throw new CommandConflict("A scheduler run ID is required.");
        if (IsDiscussion(id) && !OpenDiscussion(s, id)) throw new CommandConflict("This discussion is closed.");
        if (IsHeld(s, id)) throw new CommandConflict("This operation has been retired.");
        var previous = s.Text("SELECT scheduler_id FROM dispatch_outbox WHERE id=@id", ("id", id));
        if (s.Execute("""
            UPDATE dispatch_outbox SET scheduler_id=@scheduler WHERE id=@id
              AND (scheduler_id IS NULL OR scheduler_id=@scheduler)
            """, ("id", id), ("scheduler", schedulerId)) != 1)
            throw new CommandConflict("Unknown dispatch or conflicting scheduler run.");
        if (previous is null) AdvanceDeadline(s, id);
        return true;
    }

    public DispatchClaim? Claim(string id, string schedulerId, Guid? sessionId) => database.Write(s => Claim(s, id, schedulerId, sessionId, ci: false));

    private DispatchClaim? Claim(Session s, string id, string schedulerId, Guid? sessionId, bool ci)
    {
        var request = s.Text("""
            SELECT o.request FROM dispatch_outbox o JOIN tasks t ON t.id=o.task_id
            WHERE o.id=@id AND (t.activity='Running' OR EXISTS (SELECT 1 FROM discussion_jobs j WHERE j.operation=o.id AND j.pending=1))
              AND NOT EXISTS (SELECT 1 FROM dispatch_holds h WHERE h.id=o.id)
            """, ("id", id));
        if (request is null) return null;
        var order = JsonSerializer.Deserialize<WorkOrder>(request, Wire.Json)!;
        if (ci != (ciProfile is not null && order.Action == WorkAction.Verify)) return null;
        var task = Decode(s.Text("SELECT document FROM tasks WHERE id=@id", ("id", order.Assignment.TaskId.ToString()))!);
        task.Gates = ReadGates(s, task.Id);
        if (order.Action == WorkAction.Discuss && order.Discussion is { } question)
        {
            if (!OpenDiscussion(s, id)) return null;
            if (!liveOwner || !CurrentDiscussion(s, task, question))
            {
                FinishDiscussion(s, id, "Unavailable", "The owner runtime or the pinned evidence changed before execution.");
                return null;
            }
        }
        else if (lifecycle.Prepare(task) is not { } active || !MatchesIntent(task, active, Encode(active), request)) return null;
        if (ci && !CheckCiBudget(s, task, order)) return null;
        Bind(s, id, schedulerId);
        int? coding = !ci && RequiresSubscription(order) && (order.Action is WorkAction.Implement or WorkAction.Review)
            ? codingSeconds : null;
        // The model and its workspace stop inside the coding window. Leave time for the
        // dispatcher to deliver the terminal result before the controller fences the claim.
        int receiptSeconds = coding.HasValue ? Math.Max(120, coding.Value + 15)
            : ci ? ciProfile!.Rules.Blueprint?.MaxCiSeconds ?? 120 : 120;
        var token = Guid.NewGuid().ToString("N");
        int inserted = s.Execute("""
            INSERT INTO dispatch_inbox(id,claim,deadline) VALUES(@id,@claim,@deadline) ON CONFLICT(id) DO NOTHING
            """, ("id", id), ("claim", token), ("deadline", DateTimeOffset.UtcNow.AddSeconds(receiptSeconds).ToUnixTimeSeconds()));
        if (inserted == 0) return null;
        if (sessionId is null || sessionId == Guid.Empty) throw new CommandConflict("A fresh execution session is required.");
        if (s.Execute("""
            INSERT INTO dispatch_sessions(operation_id,session_id) VALUES(@id,@session)
            ON CONFLICT(session_id) DO NOTHING
            """, ("id", id), ("session", sessionId.Value.ToString())) == 0)
            throw new CommandConflict("Execution session already belongs to another operation.");
        AdvanceDeadline(s, id, coding is > 60 ? Math.Max(stallSeconds, receiptSeconds) : stallSeconds);
        return new(token, order, ci ? "ci" : RequiresSubscription(order) ? "codex-subscription" : "controlled", coding);
    }

    private bool RequiresSubscription(WorkOrder order) => order.Action is WorkAction.Discuss or WorkAction.AnswerWorker || order.ExpertRuntime == "codex-subscription"
        || ciProfile?.RequiresCodex == true && order.Action is WorkAction.Implement or WorkAction.Review;

    public bool Receive(string id, string token, JsonElement output) => database.Write(s =>
    {
        if (IsHeld(s, id)) throw new CommandConflict("This operation has been retired.");
        if (s.Text("SELECT id FROM ci_runs WHERE id=@id", ("id", id)) is not null)
            throw new CommandConflict("Only the CI provider can submit this verification.");
        if (ciProfile?.RequiresCodex == true || id.EndsWith(".Consult", StringComparison.Ordinal) || IsDiscussion(id) || id.EndsWith(".AnswerWorker", StringComparison.Ordinal))
        {
            var request = s.Text("SELECT request FROM dispatch_outbox WHERE id=@id", ("id",id));
            var order = request is null ? null : JsonSerializer.Deserialize<WorkOrder>(request, Wire.Json);
            if (order is null) throw new CommandConflict("Unknown live operation.");
            if (order.Action == WorkAction.Discuss)
            {
                var session = s.Text("SELECT session_id FROM dispatch_sessions WHERE operation_id=@id", ("id",id));
                return ApplyDiscussion(s, order, token, output, session ?? throw new CommandConflict("Unclaimed owner discussion."));
            }
            if (order.ExpertRuntime == "codex-subscription" || order.Action == WorkAction.AnswerWorker)
            {
                var execution = s.Text("SELECT session_id FROM dispatch_sessions WHERE operation_id=@id", ("id",id));
                if (execution is null) throw new CommandConflict("Unclaimed expert consultation.");
                CodexEvidence.Require(order, output, null, execution);
            }
            else if (order.Action is WorkAction.Implement or WorkAction.Review)
                CodexEvidence.Require(order, output, ciProfile!.Rules.RepositoryLayout);
        }
        return Receive(s, id, token, output);
    });

    private static bool Receive(Session s, string id, string token, JsonElement output)
    {
        if (IsHeld(s, id)) throw new CommandConflict("This operation has been retired.");
        var encoded = Encode(output);
        if (s.Execute("""
            UPDATE dispatch_inbox SET output=@output WHERE id=@id AND claim=@claim
              AND ((output IS NULL AND deadline>@now) OR output=@output)
            """, ("id", id), ("claim", token), ("output", encoded), ("now", DateTimeOffset.UtcNow.ToUnixTimeSeconds())) != 1)
            throw new CommandConflict("Unknown claim or conflicting result.");
        return true;
    }

    public bool Failed(string id, string schedulerId) => database.Write(s =>
    {
        if (IsDiscussion(id))
        {
            if (s.Text("SELECT id FROM dispatch_outbox WHERE id=@id AND scheduler_id=@scheduler", ("id",id), ("scheduler",schedulerId)) is null) return false;
            FinishDiscussion(s, id, "Failed", "The scheduler ended this discussion without a verified answer.");
            return true;
        }
        var raw = s.Text("""
            SELECT t.document FROM tasks t JOIN dispatch_outbox o ON t.id=o.task_id
            WHERE o.id=@id AND o.scheduler_id=@scheduler AND t.activity='Running'
            """, ("id", id), ("scheduler", schedulerId));
        if (raw is null) return false;
        var task = Decode(raw);
        task.Gates = ReadGates(s, task.Id);
        if (lifecycle.Prepare(task) is not { } order || order.Id != id
            || s.Text("SELECT output FROM dispatch_inbox WHERE id=@id", ("id", id)) is not null) return false;
        Park(s, task, order, "Outcome unknown", FailedRun);
        return true;
    });

    private WorkItem Update(Guid id, Func<WorkItem?, WorkItem> change) => database.Write(s =>
    {
        var raw = s.Text("SELECT document FROM tasks WHERE id=@id", ("id", id.ToString()));
        var existing = raw is null ? null : Decode(raw);
        if (existing is not null) existing.Gates = ReadGates(s, existing.Id);
        var task = change(existing);
        if (raw != Encode(task)) Save(s, task);
        return task;
    });

    private static string Encode<T>(T value) => JsonSerializer.Serialize(value, Wire.Json);
    private static WorkItem Decode(string raw) => JsonSerializer.Deserialize<WorkItem>(raw, Wire.Json)!;

    private static List<GateEvaluation> ReadGates(Session s, Guid id)
    {
        using var cmd = s.Command("SELECT evaluation FROM gate_evaluations WHERE task_id=@id ORDER BY seq", ("id", id.ToString()));
        using var rows = cmd.ExecuteReader();
        var gates = new List<GateEvaluation>();
        while (rows.Read()) gates.Add(JsonSerializer.Deserialize<GateEvaluation>(rows.GetString(0), Wire.Json)!);
        return gates;
    }

    private static void Save(Session s, WorkItem task)
    {
        var id = task.Id.ToString();
        s.Execute("""
            INSERT INTO tasks(id,activity,turn,document)
            VALUES(@id,@activity,(SELECT COALESCE(MAX(turn),0)+1 FROM tasks),@doc)
            ON CONFLICT(id) DO UPDATE SET activity=excluded.activity, turn=excluded.turn, document=excluded.document
            """, ("id", id), ("activity", task.Activity.ToString()), ("doc", Encode(task)));
        SaveSummary(s, task);
        using var gateCount = s.Command("SELECT COALESCE(MAX(seq),0) FROM gate_evaluations WHERE task_id=@id", ("id", id));
        foreach (var gate in task.Gates.Skip(Convert.ToInt32(gateCount.ExecuteScalar())))
            s.Execute("INSERT INTO gate_evaluations(task_id,seq,evaluation) VALUES(@id,@seq,@entry)",
                ("id", id), ("seq", gate.Sequence), ("entry", Encode(gate)));
        using var last = s.Command("SELECT COALESCE(MAX(seq),0) FROM task_events WHERE task_id=@id", ("id", id));
        var count = Convert.ToInt32(last.ExecuteScalar());
        foreach (var entry in task.History.Skip(count))
            s.Execute("INSERT INTO task_events(task_id,seq,entry) VALUES(@id,@seq,@entry)",
                ("id", id), ("seq", entry.Number), ("entry", Encode(entry)));
    }
}
