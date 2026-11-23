using System.Text.Json;
using System.Text.RegularExpressions;

namespace Factory;

public record ScenarioOption(string Id, string Label);
public record DecisionOption(string Value, string Label);
public record Discussion(Guid Id, int AfterEntry, string Text, string Reply, DateTimeOffset SentAt,
    string? Status = null, string? Operation = null, string? EvidenceHash = null);
public record TaskSummary(Guid Id, string Scenario, string Goal, Phase Phase, Activity Activity,
    DateTimeOffset? OpenedAt, string? WaitReason, string? LastActivity, int? DraftNumber = null);
public record QueueRow(TaskSummary Task, long Revision, long StateRevision);

public sealed partial class TaskStore
{
    public void InitializeDashboard() => database.Write(s =>
    {
        using var cmd = s.Command("""
            SELECT id FROM tasks t WHERE NOT EXISTS (SELECT 1 FROM dashboard_queue q WHERE q.task_id=t.id)
            """);
        var missing = new List<Guid>();
        using (var rows = cmd.ExecuteReader())
            while (rows.Read()) missing.Add(Guid.Parse(rows.GetString(0)));
        foreach (var id in missing) SaveSummary(s, ReadTask(s, id));
        return missing.Count;
    });

    private static void SaveSummary(Session s, WorkItem task)
    {
        string? Preview(string? text) => text is { Length: > 140 } ? text[..(char.IsHighSurrogate(text[139]) ? 139 : 140)] + "…" : text;
        var summary = new TaskSummary(task.Id, task.Scenario, Preview(task.Goal)!, task.Phase, task.Activity, task.OpenedAt,
            task.Activity == Activity.Waiting ? Preview(task.Question?.Text) : null, Preview(task.History.LastOrDefault()?.Text),
            task.Activity == Activity.Delivered ? RepositoryDelivery(s, task.Id)?.Draft?.Number : null);
        s.Execute("""
            INSERT INTO dashboard_queue(task_id,summary,revision) VALUES(@id,@summary,1)
            ON CONFLICT(task_id) DO UPDATE SET summary=excluded.summary,revision=dashboard_queue.revision+1
            """, ("id", task.Id.ToString()), ("summary", Encode(summary)));
    }

    public List<QueueRow> Queue() => database.Read(s =>
    {
        using var cmd = s.Command("""
            SELECT q.summary,q.revision,t.turn FROM dashboard_queue q JOIN tasks t ON t.id=q.task_id ORDER BY t.turn DESC
            """);
        using var rows = cmd.ExecuteReader();
        var queue = new List<QueueRow>();
        while (rows.Read()) queue.Add(new(JsonSerializer.Deserialize<TaskSummary>(rows.GetString(0), Wire.Json)!,
            rows.GetInt64(1), rows.GetInt64(2)));
        return queue;
    });

    private static WorkItem ReadTask(Session s, Guid id) =>
        Decode(s.Text("SELECT document FROM tasks WHERE id=@id", ("id", id.ToString())) ?? throw new KeyNotFoundException());

    private static string QuestionKey(WorkItem task) => GateInput.Hash(new { task.Id, task.Question,
        Run = task.Results.LastOrDefault(r => r.Status == ResultStatus.NeedsInput)?.Assignment.RunId });

    public object Workspace(Guid id) => database.Read(s =>
    {
        var task = ReadTask(s, id);
        var memory = lifecycle.Memory(task);
        var owner = memory.View(task);
        var recovery = Recovery(s, task);
        var repository = RepositoryDelivery(s, id);
        using var cmd = s.Command("SELECT exchange FROM task_discussion WHERE task_id=@id ORDER BY seq", ("id", id.ToString()));
        using var rows = cmd.ExecuteReader();
        var messages = new List<Discussion>();
        while (rows.Read()) messages.Add(JsonSerializer.Deserialize<Discussion>(rows.GetString(0), Wire.Json)!);
        return new { task.Id, task.Goal, task.OwnerId, task.Phase, task.Activity, task.Scenario, task.OpenedAt,
            nextAction = repository?.Draft is null ? owner.NextAction : "Inspect the Draft PR and hosted checks",
            draftNumber = repository?.Draft?.Number, run = task.Run is { } run ? new { run.RunId, run.Phase } : null, task.History, messages, brief = memory.Briefs[^1],
            recovery, repository, decisions = memory.Decisions, task.Question, task.Advice, task.HumanChoice,
            questionKey = QuestionKey(task), choices = task.Activity == Activity.Waiting ? lifecycle.Choices(task) : [] };
    });

    public object Evidence(Guid id) => database.Read(s =>
    {
        var task = ReadTask(s, id);
        return new { task.Id, task.Receipts, task.RejectedReceipt, task.Candidate, task.Review,
            ciPolicy = ciProfile?.Rules, ciBudget = CiBudget(s, id), ciExecutions = CiEvidence(s, id),
            documents = lifecycle.Memory(task).Documents.Select(d => new { d.Part, d.Path, d.Digest, d.Revision }), gates = ReadGates(s, id) };
    });

    public Discussion Discuss(Guid id, Guid messageId, string text) => database.Write(s =>
    {
        var key = id.ToString();
        var previous = s.Text("SELECT exchange FROM task_discussion WHERE task_id=@id AND message_id=@message",
            ("id", key), ("message", messageId.ToString()));
        if (previous is not null)
        {
            var exchange = JsonSerializer.Deserialize<Discussion>(previous, Wire.Json)!;
            if (exchange.Text != text) throw new CommandConflict("Message ID already has different text.");
            return exchange;
        }
        var task = ReadTask(s, id);
        task.Gates = ReadGates(s, id);
        var owner = lifecycle.Memory(task).View(task);
        var reply = Reply(task, owner, text);
        var added = new Discussion(messageId, task.History.Count, text, reply, DateTimeOffset.UtcNow);
        if (liveOwner && reply == SavedNote && text.EndsWith('?'))
        {
            if (task.Activity == Activity.Waiting && task.Question is { Reserved: true } question)
                added = added with { Reply = question.Text + " This choice is reserved for the operator. Use a decision button to record it." };
            else
                added = QueueDiscussion(s, task, added);
        }
        s.Execute("""
            INSERT INTO task_discussion(task_id,message_id,seq,exchange)
            VALUES(@id,@message,(SELECT COALESCE(MAX(seq),0)+1 FROM task_discussion WHERE task_id=@id),@exchange)
            """, ("id", key), ("message", messageId.ToString()), ("exchange", Encode(added)));
        // Discussion advances the browser cursor without moving the scheduler's turn or changing the brief.
        s.Execute("UPDATE dashboard_queue SET revision=revision+1 WHERE task_id=@id", ("id", key));
        return added;
    });

    private const string SavedNote = "Your note is saved in this conversation. I can report the accepted brief, status, verification checks or decision options from this task’s record.";

    private static string Reply(WorkItem task, OwnerView owner, string text)
    {
        // Resolve an explicit project reference from this task's retained brief before classifying general topics.
        bool RefersTo(string id) => Regex.IsMatch(text,
            @"(?<![\p{L}\p{N}_/-])" + Regex.Escape(id) + @"(?![\p{L}\p{N}_/-])",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        var briefVersion = owner.Briefs[^1];
        var answers = new List<string>();
        foreach (var decision in owner.Decisions.Where(d => d.Scope == task.Id && RefersTo(d.Decision.Id)))
        {
            var successor = owner.Decisions.LastOrDefault(d => d.Scope == task.Id && d.Supersedes == decision.Decision.Id);
            var status = successor is null ? decision.Authority : $"Superseded by {successor.Decision.Id}";
            answers.Add($"{decision.Decision.Id} ({status}). {decision.Decision.Text} Evidence: {string.Join(", ", decision.Sources)}.");
        }
        foreach (var criterion in briefVersion.Requirements.Criteria.Where(c => RefersTo(c.Id)))
            answers.Add($"{criterion.Id} (brief v{briefVersion.Version}). {criterion.Text}");
        if (answers.Count > 0) return string.Join("\n", answers);

        var words = Regex.Matches(text.ToLowerInvariant(), @"[\p{L}\p{N}]+").Select(m => m.Value).ToHashSet();
        bool Mentions(params string[] terms) => terms.Any(words.Contains);
        if (Mentions("expert", "consultation", "uncertainty", "assumptions", "advisor") &&
            task.Advice?.Consultation is { } consultation)
            return $"{consultation.ProfileVersion}: {consultation.Conclusion} {task.Advice.Recommendation} "
                + $"{consultation.Assumption} {consultation.Uncertainty} Sources: {task.Advice.Source}. "
                + $"Decision authority: {consultation.RequiredAuthority}.";
        if (Mentions("failed", "fail", "failure", "checks", "evidence", "verification", "repair", "test", "tests"))
        {
            var localFailure = task.Gates.LastOrDefault(g => g.Input.Phase == Phase.Implementation &&
                g.Criteria.Any(c => c.Id.StartsWith("local/", StringComparison.Ordinal) && !c.Passed));
            if (localFailure is not null && (Mentions("local", "early") || !task.Gates.Any(g =>
                    g.Sequence > localFailure.Sequence && g.Input.Phase == Phase.Verification)))
                return $"Local gate {localFailure.Sequence} recorded {localFailure.Outcome}. {localFailure.Reason} The current phase is {task.Phase}.";
            var verification = task.Gates.Where(g => g.Input.Phase == Phase.Verification).ToList();
            var gate = verification.LastOrDefault();
            if (Mentions("failed", "fail", "failure", "repair") &&
                verification.LastOrDefault(g => g.Outcome is GateOutcome.Revise or GateOutcome.Block) is { } failed)
                return $"Verification gate {failed.Sequence} recorded {failed.Outcome}. {failed.Reason} The current phase is {task.Phase}.";
            return gate is null ? "Verification has not recorded a gate result yet. " + owner.NextAction
                : $"Verification gate {gate.Sequence} recorded {gate.Outcome}. {gate.Reason}";
        }
        if (Mentions("decision", "decide", "recommend", "options", "approve", "keep", "consequence", "risk", "waiting", "recommendation", "consequences", "alternatives", "choices"))
        {
            if (task.Activity == Activity.Waiting && task.Advice is { } advice)
                return $"{task.Question?.Text} {advice.Recommendation} {advice.Alternative} {advice.Consequence} Source: {advice.Source}. Use a decision button to record your choice.";
            if (owner.Decisions.LastOrDefault() is { } record) return $"{record.Actor}: {record.Decision.Text} {record.Rationale}";
            return "There is no pending product decision. " + owner.NextAction;
        }
        if (Mentions("brief", "goal", "criteria", "requirements", "scope", "plan"))
        {
            var brief = owner.Briefs[^1];
            return $"Brief v{brief.Version}: {brief.Goal} " + string.Join(" ", brief.Requirements.Criteria.Select(c => c.Text))
                + (brief.Plan is null ? "" : " Plan: " + brief.Plan);
        }
        if (Mentions("status", "next", "progress", "phase", "worker", "blocked", "happening"))
            return $"{task.Phase} · {task.Activity}. {owner.NextAction.TrimEnd('.')}. " + task.History.LastOrDefault()?.Text;
        return SavedNote;
    }
}
