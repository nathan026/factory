using System.Text.Json;

namespace Factory;

public record IssueActionCommand(Guid TaskId, string HeadSha, string BaseSha, string Scope, Guid? ActionId = null);
public record IssueActionState(Guid Id, string Kind, string Stage, string Scope, string HeadSha, string BaseSha, string? CommentId);
public record DeliveredRound(IssueOrigin Origin, DraftIntent Intent, DraftReceipt Draft, bool OwnsBranch);
internal record IssueActionRecord(IssueActionState State, string Body, DeliveredRound Delivery);

public sealed partial class TaskStore
{
    private static IssueActionRecord? ActionFor(Session s, Guid task, string? head)
    {
        using var cmd = s.Command("""
            SELECT id,kind,stage,scope,head_sha,base_sha,comment_id,body,origin,intent,draft,owns_branch FROM github_issue_actions
            WHERE task_id=@task AND head_sha=@head AND stage <> 'withdrawn'
            """, ("task", task.ToString()), ("head", head ?? ""));
        using var r = cmd.ExecuteReader();
        return !r.Read() ? null : new(new(Guid.Parse(r.GetString(0)), r.GetString(1), r.GetString(2), r.GetString(3), r.GetString(4), r.GetString(5),
            r.IsDBNull(6) ? null : r.GetString(6)), r.GetString(7), new(
            JsonSerializer.Deserialize<IssueOrigin>(r.GetString(8), Wire.Json)!,
            JsonSerializer.Deserialize<DraftIntent>(r.GetString(9), Wire.Json)!,
            JsonSerializer.Deserialize<DraftReceipt>(r.GetString(10), Wire.Json)!, r.GetInt32(11) == 1));
    }

    internal IssueActionRecord? ActionFor(Guid task, string head) => database.Read(s => ActionFor(s, task, head));

    internal DeliveredRound CurrentDelivery(long repo, int issue, IssueActionCommand command) => database.Read(s =>
    {
        var status = IssueStatus(s, repo, issue);
        if (status.TaskId != command.TaskId || status.Origin is null || status.Draft is null || status.Draft.HeadSha != command.HeadSha ||
            ReadTask(s, command.TaskId).Activity != Activity.Delivered)
            throw new CommandConflict("The delivery changed. Reload the task.");
        var raw = s.Text("SELECT intent FROM github_publications WHERE task_id=@id", ("id", command.TaskId.ToString()))!;
        return new DeliveredRound(status.Origin, JsonSerializer.Deserialize<DraftIntent>(raw, Wire.Json)!, status.Draft!,
            s.Text("SELECT task_id FROM github_publications WHERE task_id=@id AND ref_attempted=1", ("id", command.TaskId.ToString())) is not null);
    });

    internal List<DeliveredRound> PriorRounds(Guid task) => database.Read(s =>
    {
        using var cmd = s.Command("SELECT origin,intent,draft,owns_branch FROM github_issue_actions WHERE task_id=@id AND stage='started' ORDER BY sequence", ("id", task.ToString()));
        using var r = cmd.ExecuteReader();
        var list = new List<DeliveredRound>();
        while (r.Read()) list.Add(new(JsonSerializer.Deserialize<IssueOrigin>(r.GetString(0), Wire.Json)!,
            JsonSerializer.Deserialize<DraftIntent>(r.GetString(1), Wire.Json)!, JsonSerializer.Deserialize<DraftReceipt>(r.GetString(2), Wire.Json)!, r.GetInt32(3) == 1));
        return list;
    });

    private static List<DraftReceipt> PreviousDrafts(Session s, Guid taskId)
    {
        using var command = s.Command("SELECT draft FROM github_issue_actions WHERE task_id=@t AND stage='started' ORDER BY sequence",
            ("t", taskId.ToString()));
        using var rows = command.ExecuteReader();
        var drafts = new List<DraftReceipt>();
        while (rows.Read()) drafts.Add(JsonSerializer.Deserialize<DraftReceipt>(rows.GetString(0), Wire.Json)!);
        return drafts;
    }

    internal string PreviewFinish(DeliveredRound round) => database.Read(s =>
        FinishSummary(s, ReadTask(s, round.Intent.TaskId), round));

    internal IssueActionRecord ReserveIssueAction(DeliveredRound round, IssueActionCommand command, string kind) => database.Write(s =>
    {
        var task = ReadTask(s, command.TaskId);
        if (task.Activity != Activity.Delivered || RepositoryDelivery(s, task.Id)?.Draft != round.Draft)
            throw new CommandConflict("Delivery changed before the command was recorded.");
        var id = Guid.NewGuid();
        var marker = $"<!-- factory-issue-action:{id:N} -->";
        var body = kind == "follow-up"
            ? $"## Follow-up request\n\n{DeliverySummary.Literal(command.Scope)}\n\nApproved base: `{command.BaseSha}`."
            : FinishSummary(s, task, round);
        s.Execute("""
            INSERT INTO github_issue_actions(task_id,head_sha,id,kind,scope,base_sha,stage,body,archived_task,intent,origin,draft,ci_total,sequence,owns_branch)
            VALUES(@task,@head,@id,@kind,@scope,@base,'ready',@body,@doc,@intent,@origin,@draft,(SELECT COUNT(*) FROM ci_runs WHERE task_id=@task), (SELECT COUNT(*) FROM github_issue_actions WHERE task_id=@task),@owns)
            ON CONFLICT DO NOTHING
            """, ("task", task.Id.ToString()), ("head", round.Draft.HeadSha), ("id", id.ToString()), ("owns", round.OwnsBranch ? 1 : 0), ("kind", kind), ("scope", command.Scope),
            ("base", command.BaseSha), ("body", body + "\n\n" + marker), ("doc", Encode(task)), ("intent", Encode(round.Intent)), ("origin", Encode(round.Origin)), ("draft", Encode(round.Draft)));
        var action = ActionFor(s, task.Id, command.HeadSha)!;
        if (action.State.Kind != kind || action.State.Scope != command.Scope || action.State.BaseSha != command.BaseSha)
            throw new CommandConflict("A different operator action already covers this delivery.");
        return action;
    });

    private string FinishSummary(Session s, WorkItem current, DeliveredRound round)
    {
        var entries = new List<string>();
        void Add(WorkItem task, string path, DraftReceipt pr)
        {
            var summary = DeliverySummary.From(task, path, ProfileFor(task)?.Rules.RepositoryLayout, true);
            entries.Add($"### PR #{pr.Number}\n\n{DeliverySummary.Literal(task.FollowUp?.Scope ?? summary.Request ?? summary.Issue)}\n\n" + string.Join("\n", summary.Work.Concat(summary.Checks).Select(v => "- " + DeliverySummary.Literal(v))));
        }
        using (var cmd = s.Command("SELECT archived_task,origin,draft FROM github_issue_actions WHERE task_id=@t AND stage='started' ORDER BY sequence", ("t", current.Id.ToString())))
        using (var r = cmd.ExecuteReader())
            while (r.Read()) Add(Decode(r.GetString(0)), JsonSerializer.Deserialize<IssueOrigin>(r.GetString(1), Wire.Json)!.Path,
                JsonSerializer.Deserialize<DraftReceipt>(r.GetString(2), Wire.Json)!);
        Add(current, round.Origin.Path, round.Draft);
        var title = current.OpeningMessage?.Split('\n', 2)[0].Trim() ?? current.Goal;
        var body = "## Work complete\n\n" + DeliverySummary.Literal(title) + "\n\nThe operator accepted the work for this issue.\n\n" + string.Join("\n\n", entries);
        if (body.Length > 64000) throw new CommandConflict("The delivery summary exceeds the GitHub comment size limit.");
        return body;
    }

    internal bool StepIssueAction(Guid task, Guid id, string from, string to, string? comment = null) => database.Write(s =>
    {
        var changed = s.Execute("UPDATE github_issue_actions SET stage=@to,comment_id=COALESCE(@c,comment_id) WHERE id=@id AND task_id=@t AND stage=@from",
            ("id", id.ToString()), ("t", task.ToString()), ("from", from), ("to", to), ("c", (object?)comment ?? DBNull.Value)) == 1;
        if (changed) s.Execute("UPDATE dashboard_queue SET revision=revision+1 WHERE task_id=@t", ("t", task.ToString()));
        return changed;
    });

    internal bool ReserveBranchCleanup(Guid id, string branch) => database.Write(s =>
        s.Execute("INSERT INTO github_branch_cleanup(action_id,branch) VALUES(@id,@b) ON CONFLICT DO NOTHING",
            ("id", id.ToString()), ("b", branch)) == 1);

    internal string? CleanupResult(Guid id, string branch) => database.Read(s =>
        s.Text("SELECT result FROM github_branch_cleanup WHERE action_id=@id AND branch=@b", ("id", id.ToString()), ("b", branch)));

    internal void RecordCleanup(Guid id, string branch, string result) => database.Write(s =>
        s.Execute("""
            INSERT INTO github_branch_cleanup(action_id,branch,result) VALUES(@id,@b,@r)
            ON CONFLICT(action_id,branch) DO UPDATE SET result=excluded.result WHERE github_branch_cleanup.result IS NULL
            """, ("id", id.ToString()), ("b", branch), ("r", result)));

    internal void FinishIssueAction(Guid taskId, Guid id, List<string> branches) => database.Write(s =>
    {
        if (s.Execute("UPDATE github_issue_actions SET stage='finished' WHERE task_id=@t AND id=@id AND stage='closed'",
            ("t", taskId.ToString()), ("id", id.ToString())) == 1)
        {
            var task = ReadTask(s, taskId);
            task.Record("Operator", "Finished the GitHub issue. " + string.Join(" ", branches));
            Save(s, task);
        }
        return true;
    });

    internal void StartFollowUp(IssueActionRecord action, IssueOrigin origin, SourcePacket source, bool carryCandidate) => database.Write(s =>
    {
        var a = action.State;
        var tid = action.Delivery.Intent.TaskId;
        var prior = ReadTask(s, tid);
        var current = ActionFor(s, tid, a.HeadSha)?.State;
        if (current?.Id != a.Id) throw new CommandConflict("The operator replaced this follow-up.");
        if (current.Stage == "started") return true;
        if (prior.Activity != Activity.Delivered || RepositoryDelivery(s, tid)?.Draft != action.Delivery.Draft ||
            s.Execute("UPDATE github_issue_actions SET stage='started' WHERE id=@id AND stage='commented'", ("id", a.Id.ToString())) != 1)
            throw new CommandConflict("The follow-up admission changed.");
        prior.Gates = ReadGates(s, tid);
        var grant = new RoundApproval(a.Id, tid, a.Scope, origin.BaseSha, source.Fingerprint(), origin.PolicyHash ?? "");
        grant.Persist();
        var next = lifecycle.Start(tid, prior.Scenario, prior.OpeningMessage, source, prior, grant);
        if (carryCandidate) next.Candidate = prior.Candidate;
        next.Record("Operator", $"Approved follow-up at {origin.BaseSha} for issue #{origin.Number}. Previous delivery: PR #{action.Delivery.Draft.Number}.");
        s.Execute("DELETE FROM github_publication_holds WHERE task_id=@t", ("t", tid.ToString()));
        s.Execute("DELETE FROM github_publications WHERE task_id=@t", ("t", tid.ToString()));
        s.Execute("UPDATE github_intake SET origin=@o,version=version+1 WHERE task_id=@t", ("o", Encode(origin)), ("t", tid.ToString()));
        Save(s, next);
        return true;
    });
}
