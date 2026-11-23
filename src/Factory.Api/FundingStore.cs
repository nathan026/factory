using System.Globalization;
using System.Text;
using System.Text.Json;

namespace Factory;

public record AdviceRequest(Guid Id, string Question);
public record AdviceReceipt(Guid RequestId, int Attempt, string ResponseId, string Account, string Model, int InputTokens, int OutputTokens, string Answer);
public record FundingAttempt(int Attempt, string State, string Reason, long ReservedMicros, long EstimatedMicros,
    string CreatedUtc, string UpdatedUtc, AdviceReceipt? Receipt);
public record FundedAdvice(Guid Id, string Question, string Account, FundingAttempt Latest);
public record FundingWait(string Epoch, string Kind, long? RetryAt);
public record FundingUse(long HeldMicros, long EstimatedMicros);
public record AdviceTicket(FundedAdvice Request, string? Input = null);

public sealed partial class TaskStore
{
    private static string Stamp() => DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
    private static FundingAttempt Attempt(System.Data.Common.DbDataReader row, int start = 0) =>
        new(row.GetInt32(start), row.GetString(start+1), row.GetString(start+2), row.GetInt64(start+3), row.GetInt64(start+4),
            row.GetString(start+5), row.GetString(start+6), row.IsDBNull(start+7) ? null :
            JsonSerializer.Deserialize<AdviceReceipt>(row.GetString(start+7), Wire.Json));
    private const string AttemptFields = "a.attempt,a.state,a.reason,a.reserved,a.estimate,a.created_utc,a.updated_utc,a.receipt";

    private static FundedAdvice? Advice(Session s, Guid task, Guid request)
    {
        using var cmd = s.Command("SELECT r.question,r.account," + AttemptFields + """
             FROM funding_requests r JOIN funding_attempts a ON a.request_id=r.id
             WHERE r.task_id=@task AND r.id=@id ORDER BY a.attempt DESC LIMIT 1
            """, ("task", task.ToString()), ("id", request.ToString()));
        using var row = cmd.ExecuteReader();
        return row.Read() ? new(request, row.GetString(0), row.GetString(1), Attempt(row, 2)) : null;
    }

    public FundedAdvice Advice(Guid task, Guid request) =>
        database.Read(s => Advice(s, task, request) ?? throw new KeyNotFoundException());

    private static FundingUse Used(Session s, string? account, Guid? task = null)
    {
        using var cmd = s.Command("""
            SELECT COALESCE(SUM(CASE WHEN a.state IN ('InFlight','Unconfirmed','CancelPending') THEN a.reserved ELSE 0 END),0),
                   COALESCE(SUM(a.estimate),0)
            FROM funding_requests r JOIN funding_attempts a ON a.request_id=r.id WHERE 1=1
            """ + (account is null ? "" : " AND r.account=@account") + (task is null ? "" : " AND r.task_id=@task"), ("account", account ?? ""), ("task", task?.ToString() ?? ""));
        using var row = cmd.ExecuteReader();
        row.Read();
        return new(Convert.ToInt64(row.GetValue(0)), Convert.ToInt64(row.GetValue(1)));
    }

    private static FundingWait? AccountWait(Session s, string account)
    {
        using var cmd = s.Command("SELECT epoch,kind,retry_at FROM funding_waits WHERE account=@account", ("account",account));
        using var row = cmd.ExecuteReader();
        return row.Read() ? new(row.GetString(0), row.GetString(1), row.IsDBNull(2) ? null : row.GetInt64(2)) : null;
    }
    public FundingWait? AccountWait(string account) => database.Read(s => AccountWait(s, account));

    private static void CheckIdentity(Session s, Guid task, Guid id, FixtureAllocation allocation)
    {
        var identity = s.Text("SELECT identity_hash FROM funding_requests WHERE id=@id AND task_id=@task",
            ("id", id.ToString()), ("task", task.ToString())) ?? throw new KeyNotFoundException();
        if (identity != allocation.Identity) throw new CommandConflict("Restore the funding account, model and rates used for this request.");
    }
    public void CheckFundingIdentity(Guid task, Guid id, FixtureAllocation allocation) =>
        database.Read(s => { CheckIdentity(s, task, id, allocation); return true; });

    public AdviceTicket BeginAdvice(Guid task, AdviceRequest request, FixtureAllocation allocation, int? expected = null,
        string? clearedEpoch = null) => database.Write<AdviceTicket>(s =>
    {
        var id = request.Id;
        if (id == Guid.Empty || string.IsNullOrWhiteSpace(request.Question) || request.Question.Length > 2000)
            throw new BadHttpRequestException("Supply a request ID and a question of up to 2000 characters.");
        var prior = Advice(s, task, id);
        string input;
        if (prior is not null)
        {
            if (prior.Question != request.Question) throw new CommandConflict("Request ID already has a different question.");
            if (expected is null || prior.Latest.Attempt != expected || prior.Latest.State is not ("Budget" or "Quota" or "Authentication"))
                return new(prior);
            CheckIdentity(s, task, id, allocation);
            input = s.Text("SELECT context FROM funding_requests WHERE id=@id", ("id", id.ToString()))!;
        }
        else
        {
            if (expected is not null) throw new KeyNotFoundException();
            var item = ReadTask(s, task);
            if (!allocation.TaskLimitsMicros.ContainsKey(task))
                throw new CommandConflict("This task has no owner-advice allocation.");
            input = Encode(new { taskId = task, item.Goal, item.Phase, item.Activity, question = request.Question });
            if (s.Execute("""
                INSERT INTO funding_requests(task_id,id,question,context,account,identity_hash)
                VALUES(@task,@id,@q,@input,@account,@identity) ON CONFLICT(id) DO NOTHING
                """, ("task",task.ToString()), ("id",id.ToString()), ("q",request.Question), ("input",input),
                ("account",allocation.Account), ("identity",allocation.Identity)) != 1)
                throw new CommandConflict("Request identity already in use.");
        }
        if (clearedEpoch is not null)
            s.Execute("DELETE FROM funding_waits WHERE account=@account AND epoch=@epoch", ("account",allocation.Account), ("epoch",clearedEpoch));
        var wait = AccountWait(s, allocation.Account);
        var bytes = Encoding.UTF8.GetByteCount(input);
        // The loopback contract bounds input at one unit per UTF-8 byte.
        var reserve = checked((long)bytes * allocation.InputMicrosPerToken + (long)allocation.MaxOutputTokens * allocation.OutputMicrosPerToken);
        var accountUse = Used(s, allocation.Account);
        var taskUse = Used(s, allocation.Account, task);
        var limit = allocation.TaskLimitsMicros.GetValueOrDefault(task);
        var reason = bytes > allocation.MaxInputBytes || reserve > allocation.RunLimitMicros ? "Request exceeds the per-run allocation."
            : taskUse.HeldMicros + taskUse.EstimatedMicros + reserve > limit ? "Insufficient task allocation."
            : accountUse.HeldMicros + accountUse.EstimatedMicros + reserve > allocation.AccountLimitMicros ? "Insufficient account allocation."
            : null;
        var state = wait?.Kind ?? (reason is null ? "InFlight" : "Budget");
        reason = wait is not null ? "Provider clearance required." : reason ?? "Reserved before submission.";
        if (prior?.Latest.State == state && state != "InFlight" && prior.Latest.Reason == reason) return new(prior);
        var attempt = checked((prior?.Latest.Attempt ?? 0) + 1);
        s.Execute("""
            INSERT INTO funding_attempts(request_id,attempt,state,reason,reserved,input_bound,output_bound,created_utc,updated_utc)
            VALUES(@id,@a,@state,@reason,@reserve,@bytes,@output,@now,@now)
            """, ("id",id.ToString()), ("a",attempt), ("state",state), ("reason",reason),
            ("reserve",state == "InFlight" ? reserve : 0L), ("bytes",bytes), ("output",allocation.MaxOutputTokens), ("now",Stamp()));
        return new(Advice(s, task, id)!, state == "InFlight" ? input : null);
    });

    public FundedAdvice FinishAdvice(Guid task, Guid id, int attempt, FixtureAllocation allocation, AdviceReceipt receipt)
        => database.Write(s =>
    {
        CheckIdentity(s, task, id, allocation);
        var prior = Advice(s, task, id)!;
        if (prior.Latest.Attempt != attempt || prior.Latest.State is not ("InFlight" or "Unconfirmed" or "CancelPending"))
        {
            if (prior.Latest.Receipt == receipt) return prior;
            throw new CommandConflict("The provider outcome conflicts with the recorded attempt.");
        }
        var cost = checked((long)receipt.InputTokens * allocation.InputMicrosPerToken + (long)receipt.OutputTokens * allocation.OutputMicrosPerToken);
        var over = cost > prior.Latest.ReservedMicros || s.Text("""
            SELECT request_id FROM funding_attempts WHERE request_id=@id AND attempt=@a AND (input_bound<@input OR output_bound<@output)
            """, ("id",id.ToString()), ("a",attempt), ("input",receipt.InputTokens), ("output",receipt.OutputTokens)) is not null;
        s.Execute("""
            UPDATE funding_attempts SET state=@state,reason=@reason,estimate=@cost,receipt=@receipt,updated_utc=@now
            WHERE request_id=@id AND attempt=@a
            """, ("state",over ? "Overrun" : "Completed"), ("reason",over ? "Observed usage exceeded the reservation." : "Provider usage recorded."),
            ("cost",cost), ("receipt",Encode(receipt)), ("now",Stamp()), ("id",id.ToString()), ("a",attempt));
        return Advice(s, task, id)!;
    });

    public (FundedAdvice Request, bool Send) CancelAdvice(Guid task, Guid id, int attempt, FixtureAllocation? allocation)
        => database.Write<(FundedAdvice, bool)>(s =>
    {
        var prior = Advice(s, task, id) ?? throw new KeyNotFoundException();
        if (prior.Latest.Attempt != attempt) return (prior, false);
        var remote = prior.Latest.State is "InFlight" or "Unconfirmed" or "CancelPending";
        if (!remote && prior.Latest.State is not ("Budget" or "Quota" or "Authentication")) return (prior, false);
        if (remote) CheckIdentity(s, task, id, allocation ?? throw new CommandConflict("Restore the provider to cancel an outstanding request."));
        if (prior.Latest.State == "CancelPending") return (prior, true);
        s.Execute("UPDATE funding_attempts SET state=@state,reason=@reason,updated_utc=@now WHERE request_id=@id AND attempt=@a",
            ("state",remote ? "CancelPending" : "Cancelled"), ("reason",remote ? "Cancellation requested. Funds remain reserved pending provider confirmation." : "Operator cancelled the waiting request."),
            ("now",Stamp()), ("id",id.ToString()), ("a",attempt));
        return (Advice(s, task, id)!, remote);
    });

    public FundedAdvice ConfirmCancellation(Guid task, Guid id, int attempt) => database.Write(s =>
    {
        s.Execute("""
            UPDATE funding_attempts SET state='Cancelled',reason='Provider confirmed final cancellation with zero usage.',updated_utc=@now
            WHERE request_id=@id AND attempt=@a AND state='CancelPending' AND EXISTS
            (SELECT 1 FROM funding_requests r WHERE r.id=@id AND r.task_id=@task)
            """, ("now",Stamp()), ("id",id.ToString()), ("a",attempt), ("task",task.ToString()));
        return Advice(s, task, id) ?? throw new KeyNotFoundException();
    });

    public FundedAdvice DeferAdvice(Guid task, Guid id, int attempt, string state, long? retryAt = null) => database.Write(s =>
    {
        var prior = Advice(s, task, id) ?? throw new KeyNotFoundException();
        if (prior.Latest.Attempt != attempt || prior.Latest.State != "InFlight") return prior;
        var reason = state switch {
            "Quota" => "Provider rejected the request before execution. Check the allowance reset.",
            "Authentication" => "Provider rejected the request before execution. Restore provider access.",
            _ => "Provider outcome is uncertain. The reservation remains held until a receipt is found."
        };
        s.Execute("UPDATE funding_attempts SET state=@state,reason=@reason,updated_utc=@now WHERE request_id=@id AND attempt=@a",
            ("state",state), ("reason",reason), ("now",Stamp()), ("id",id.ToString()), ("a",attempt));
        if (state is "Quota" or "Authentication")
        {
            var existing = AccountWait(s, prior.Account);
            if (existing?.RetryAt is { } limit && (retryAt is null || limit > retryAt)) retryAt = limit;
            s.Execute("""
                INSERT INTO funding_waits(account,epoch,kind,retry_at) VALUES(@account,@epoch,@kind,@retry)
                ON CONFLICT(account) DO UPDATE SET epoch=excluded.epoch,kind=excluded.kind,retry_at=excluded.retry_at
                """, ("account",prior.Account), ("epoch",Guid.NewGuid().ToString()), ("kind",state), ("retry",(object?)retryAt ?? DBNull.Value));
        }
        return Advice(s, task, id)!;
    });

    private (int Controlled,int Subscription,int Ci) ClaimCounts(Session s, Guid task)
    {
        using var cmd = s.Command("""
            SELECT o.request,c.id FROM dispatch_outbox o JOIN dispatch_inbox i ON i.id=o.id
            LEFT JOIN ci_runs c ON c.id=o.id WHERE o.task_id=@task
            """, ("task",task.ToString()));
        using var row = cmd.ExecuteReader();
        int controlled = 0, subscription = 0, ci = 0;
        while (row.Read())
        {
            if (!row.IsDBNull(1)) ci++;
            else if (RequiresSubscription(JsonSerializer.Deserialize<WorkOrder>(row.GetString(0), Wire.Json)!)) subscription++;
            else controlled++;
        }
        return (controlled,subscription,ci);
    }

    public object FundingReport(Guid task, FundingProfile profile) => database.Read(s =>
    {
        _ = ReadTask(s, task);
        var api = profile.FixtureApi;
        using var cmd = s.Command("SELECT r.id,r.question,r.account," + AttemptFields + """
             FROM funding_requests r JOIN funding_attempts a ON a.request_id=r.id
             WHERE r.task_id=@task ORDER BY a.created_utc,r.id,a.attempt
            """, ("task",task.ToString()));
        var history = new List<FundedAdvice>();
        using (var rows = cmd.ExecuteReader())
            while (rows.Read()) history.Add(new(Guid.Parse(rows.GetString(0)), rows.GetString(1), rows.GetString(2), Attempt(rows,3)));
        var claims = ClaimCounts(s,task);
        return new { taskId = task, controlledWorkerClaims = claims.Controlled, subscriptionWorkerClaims = claims.Subscription,
            ciClaims = claims.Ci, subscription = profile.ReadSubscription(),
            profile.ManualApiBalance, profile.InfrastructureCost, paidApiEnabled = false,
            fixture = api is null ? null : new { api.Account, api.Model, api.AccountLimitMicros, api.RunLimitMicros,
                taskLimitMicros = api.TaskLimitsMicros.GetValueOrDefault(task), authorized = api.TaskLimitsMicros.ContainsKey(task),
                accountUse = Used(s, api.Account), taskUse = Used(s, api.Account, task), wait = AccountWait(s,api.Account) }, history };
    });
}
