using System.Text.Json;
using System.Text.RegularExpressions;

namespace Factory;

public record ScenarioOption(string Id, string Label);
public record DecisionOption(string Value, string Label);
public record Discussion(Guid Id, int AfterEntry, string Text, string Reply, DateTimeOffset SentAt,
    string? Status = null, string? Operation = null, string? EvidenceHash = null);
public record TaskSummary(Guid Id, string Scenario, string Goal, Phase Phase, Activity Activity,
    DateTimeOffset? OpenedAt, string? WaitReason, string? LastActivity, int? DraftNumber = null, bool IssueFinished = false);
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
        var draft = task.Activity == Activity.Delivered ? RepositoryDelivery(s,task.Id)?.Draft : null;
        var summary = new TaskSummary(task.Id, task.Scenario, Preview(task.Goal)!, task.Phase, task.Activity, task.OpenedAt,
            task.Activity == Activity.Waiting ? Preview(task.Question?.Text) : null, Preview(task.History.LastOrDefault()?.Text),
            draft?.Number, draft is not null && ActionFor(s,task.Id,draft.HeadSha)?.State.Stage=="finished");
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
        var issueAction = repository?.Draft is { } published ? ActionFor(s,id,published.HeadSha)?.State : null;
        var previousDrafts = task.FollowUp is null ? [] : PreviousDrafts(s,id);
        using var cmd = s.Command("SELECT exchange FROM task_discussion WHERE task_id=@id ORDER BY seq", ("id", id.ToString()));
        using var rows = cmd.ExecuteReader();
        var messages = new List<Discussion>();
        while (rows.Read()) messages.Add(JsonSerializer.Deserialize<Discussion>(rows.GetString(0), Wire.Json)!);
        return new { task.Id, task.Goal, task.OwnerId, task.Phase, task.Activity, task.Scenario, task.OpenedAt,
            issueFinished = issueAction?.Stage=="finished", nextAction = issueAction?.Stage=="finished"
                ? "Issue closed. Read the final summary on GitHub." : previousProfile is not null && ProfileFor(task) == previousProfile && task.Activity == Activity.Blocked ? "Select Revise and resume to review the new files and checks." : repository?.Draft is null ? owner.NextAction : "Inspect the Draft PR and hosted checks",
            draftNumber = repository?.Draft?.Number, run = task.Run is { } run ? new { run.RunId, run.Phase } : null, task.History, messages, brief = memory.Briefs[^1],
            summary = DeliverySummary.From(task, memory.Documents.First(d => d.Part == SourcePart.Source).Path,
                ProfileFor(task)?.Rules.RepositoryLayout, repository is not null),
            revisionAvailable = previousProfile is not null && task.Policy?.Version == previousProfile.Rules.Policy.Version && task.Activity == Activity.Blocked && task.Phase == Phase.Verification,
            issueAction, previousDrafts,
            recovery, repository, decisions = memory.Decisions, task.Question, task.Advice, task.HumanChoice,
            questionKey = QuestionKey(task), choices = task.Activity == Activity.Waiting ? lifecycle.Choices(task) : [] };
    });

    public object Evidence(Guid id) => database.Read(s =>
    {
        var task = ReadTask(s, id);
        return new { task.Id, task.Receipts, task.RejectedReceipt, task.Candidate, task.Review,
            earlierVerification = ArchivedVerification(s, id), ciPolicy = ProfileFor(task)?.Rules, ciBudget = CiBudget(s, id), ciExecutions = CiEvidence(s, id),
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
        if (previousProfile is not null && ProfileFor(task) == previousProfile && task.Activity == Activity.Blocked && reply.StartsWith("Verification stopped.", StringComparison.Ordinal))
            reply += "\nSelect Revise and resume to review the new files and rules.";
        var added = new Discussion(messageId, task.History.Count, text, reply, DateTimeOffset.UtcNow);
        if (liveOwner && ProfileFor(task) == ciProfile && reply == SavedNote && text.EndsWith('?'))
        {
            if (task.Activity == Activity.Waiting && task.Question is { Reserved: true } question)
                added = added with { Reply = question.Text + " This choice is reserved for the operator. Use a decision button to record it." };
            else
                added = QueueDiscussion(s, task, added);
        }
        else if (reply == SavedNote && text.EndsWith('?'))
            added = added with { Reply = "I could not answer that question from the task record. Ask about the current status or name a check or decision from the brief." };
        s.Execute("""
            INSERT INTO task_discussion(task_id,message_id,seq,exchange)
            VALUES(@id,@message,(SELECT COALESCE(MAX(seq),0)+1 FROM task_discussion WHERE task_id=@id),@exchange)
            """, ("id", key), ("message", messageId.ToString()), ("exchange", Encode(added)));
        // Discussion advances the browser cursor without moving the scheduler's turn or changing the brief.
        s.Execute("UPDATE dashboard_queue SET revision=revision+1 WHERE task_id=@id", ("id", key));
        return added;
    });

    private const string SavedNote = "Your note is saved in this conversation. I can report the accepted brief, status, verification checks or decision options from this task’s record.";

    private static string? VerificationStop(WorkItem task, OwnerView owner)
    {
        var gate = task.Gates.LastOrDefault();
        var receipt = task.Receipts.LastOrDefault();
        if (task.Activity != Activity.Blocked || task.Phase != Phase.Verification ||
            gate is not { Outcome: GateOutcome.Block, Input.Phase: Phase.Verification } ||
            receipt is not { Passed: false } || task.RejectedReceipt?.Index == task.Receipts.Count - 1 ||
            gate.ReceiptHash != GateInput.Hash(receipt) || gate.Input.RunId != receipt.RunId ||
            ContractGate.ReceiptError(task, receipt) is not null)
            return null;

        var failed = receipt.Checks.Where(c => !c.Passed).ToArray();
        var lines = new List<string> { "Verification stopped." };
        var previous = task.Receipts.Count > 1 ? task.Receipts[^2] : null;
        var previousChecked = previous is { Passed: false } && task.Gates.Any(g =>
            g.Input.Phase == Phase.Verification && g.Outcome == GateOutcome.Revise && g.ReceiptHash == GateInput.Hash(previous));
        foreach (var check in failed)
        {
            bool repeated = previousChecked && previous!.Checks.Any(c => c.Id == check.Id && !c.Passed);
            lines.Add($"The {check.Id} check failed{(repeated ? " before and after the repair" : "")}." +
                (check.Id == check.Assertion ? "" : $" {check.Assertion}"));
        }
        if (gate.Repairs == task.Policy!.MaxRepairs)
            lines.Add($"The task used {gate.Repairs} of {task.Policy.MaxRepairs} permitted repair attempts.");
        else
            lines.Add(gate.Reason);

        // The original brief records the installed checks even when the operator revises the goal.
        if (task.FollowUp is not null)
            foreach (var rule in (owner.Briefs[^1].Requirements.Criteria.Any(c => c.Text.StartsWith("Pass the installed ", StringComparison.Ordinal)) ? owner.Briefs[0] : owner.Briefs[^1]).Requirements.Criteria.Where(c => failed.Any(f => f.Id == c.Id)))
                lines.Add($"The installed requirement for {rule.Id} says “{rule.Text}”");

        lines.Add("This revision cannot go to review. Open Evidence to inspect the failed checks before deciding what to change.");
        return string.Join("\n", lines);
    }

    internal static string Reply(WorkItem task, OwnerView owner, string text)
    {
        // Resolve an explicit project reference from this task's retained brief before classifying general topics.
        bool RefersTo(string id) => Regex.IsMatch(text,
            @"(?<![\p{L}\p{N}_/-])" + Regex.Escape(id) + @"(?![\p{L}\p{N}_/-])",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        var words = Regex.Matches(text.ToLowerInvariant(), @"[\p{L}\p{N}]+").Select(m => m.Value).ToHashSet();
        bool Mentions(params string[] terms) => terms.Any(words.Contains);
        var statusQuestion = Mentions("status", "next", "progress", "phase", "worker", "blocked", "stopped",
            "happening", "happened", "update", "outcome") || text.Contains("going on", StringComparison.OrdinalIgnoreCase)
            || text.Contains("went wrong", StringComparison.OrdinalIgnoreCase);
        var checksQuestion = Mentions("failed", "fail", "failure", "checks", "evidence", "verification", "repair", "test", "tests");
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
        if ((statusQuestion || checksQuestion) && (answers.Count == 0 ||
            task.Receipts.LastOrDefault()?.Checks?.Any(c => c is { Passed: false, Id: not null } && RefersTo(c.Id)) == true) &&
            VerificationStop(task, owner) is { } stopped)
            return stopped;
        if (answers.Count > 0) return string.Join("\n", answers);

        if (Mentions("expert", "consultation", "uncertainty", "assumptions", "advisor") &&
            task.Advice?.Consultation is { } consultation)
            return $"{consultation.ProfileVersion}: {consultation.Conclusion} {task.Advice.Recommendation} "
                + $"{consultation.Assumption} {consultation.Uncertainty} Sources: {task.Advice.Source}. "
                + $"Decision authority: {consultation.RequiredAuthority}.";
        if (checksQuestion)
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
        if (statusQuestion)
            return task.Activity switch
            {
                Activity.Blocked => $"The task stopped during {task.Phase.ToString().ToLowerInvariant()}. {owner.NextAction}",
                Activity.Waiting => $"The task is waiting for your decision. {task.Question?.Text}",
                Activity.Delivered => "The task reached delivery. " + owner.NextAction.TrimEnd('.') + ".",
                _ => $"{task.Phase} is {task.Activity.ToString().ToLowerInvariant()}. {owner.NextAction.TrimEnd('.')}."
            };
        return SavedNote;
    }
}
