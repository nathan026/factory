using System.Text.Json;

namespace Factory;

public record SlackNotice(Guid Id, Guid TaskId, string Key, string Text, DecisionOption[] Choices, string[] Users);
public record SlackClick(Guid Notice, string Action, string Value, string Actor, string MessageTs, bool WitnessedMessage);

public sealed partial class TaskStore
{
    public List<Guid> NotificationCandidates() => database.Read(s =>
    {
        using var cmd = s.Command("SELECT id FROM tasks WHERE activity IN ('Waiting','Blocked') ORDER BY turn,id");
        using var rows = cmd.ExecuteReader();
        var ids = new List<Guid>();
        while (rows.Read()) ids.Add(Guid.Parse(rows.GetString(0)));
        return ids;
    });

    public SlackNotice? ReserveNotice(Guid taskId, SlackProfile profile) => database.Write<SlackNotice?>(s =>
    {
        var task = ReadTask(s, taskId);
        string key, message;
        DecisionOption[] choices = [];
        if (task.Activity == Activity.Waiting && task.Question is { Reserved: true } q)
        {
            key = QuestionKey(task);
            message = $"Product decision requested. {q.Text}";
            if (profile.DecisionUsers.Length > 0) choices = lifecycle.Choices(task).ToArray();
        }
        else if (task.Activity == Activity.Blocked)
        {
            key = GateInput.Hash(new { task.Id, Gate = ReadGates(s, taskId).LastOrDefault(), task.Activity });
            message = "Task blocked. Open the task desk to inspect its latest gate and recovery options.";
        }
        else return null;

        var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        if (s.Text("SELECT id FROM slack_notices WHERE destination=@dest AND retry_at>@now LIMIT 1",
            ("dest",profile.Destination), ("now",now)) is not null) return null;
        var prior = s.Text("SELECT notice FROM slack_notices WHERE task_id=@task AND event_key=@key",
            ("task",taskId.ToString()), ("key",key));
        if (prior is not null)
        {
            var notice = JsonSerializer.Deserialize<SlackNotice>(prior, Wire.Json)!;
            var eligibleUsers = notice.Users.Intersect(profile.DecisionUsers).ToArray();
            notice = notice with { Users = eligibleUsers, Choices = eligibleUsers.Length == 0 ? [] : notice.Choices };
            var acquired = s.Execute("""
                UPDATE slack_notices SET state='Unconfirmed',attempts=attempts+1,retry_at=NULL,notice=@notice
                WHERE id=@id AND destination=@dest AND state='RateLimited' AND attempts<4 AND retry_at<=@now
                """, ("id",notice.Id.ToString()), ("dest",profile.Destination), ("now",now), ("notice",Encode(notice)));
            return acquired == 1 ? notice : null;
        }
        var fresh = new SlackNotice(Guid.NewGuid(), taskId, key, message, choices, profile.DecisionUsers);
        s.Execute("""
            INSERT INTO slack_notices(id,task_id,event_key,destination,notice,state,attempts)
            VALUES(@id,@task,@key,@dest,@notice,'Unconfirmed',1)
            """, ("id",fresh.Id.ToString()), ("task",taskId.ToString()), ("key",key),
            ("dest",profile.Destination), ("notice",Encode(fresh)));
        return fresh;
    });

    public void FinishNotice(Guid id, string state, string? ts = null, long? retry = null) => database.Write(s =>
        s.Execute("""
            UPDATE slack_notices SET state=CASE WHEN @state='RateLimited' AND attempts>=4 THEN 'Rejected' ELSE @state END,
                message_ts=@ts,retry_at=@retry WHERE id=@id AND state='Unconfirmed'
            """, ("id",id.ToString()), ("state",state), ("ts",(object?)ts ?? DBNull.Value),
            ("retry",(object?)retry ?? DBNull.Value)));

    public object NoticeHistory(Guid task) => database.Read(s =>
    {
        _ = ReadTask(s,task);
        using var cmd = s.Command("SELECT id,state,attempts,message_ts,actor,choice FROM slack_notices WHERE task_id=@task ORDER BY event_key",
            ("task",task.ToString()));
        using var rows = cmd.ExecuteReader();
        var list = new List<object>();
        while (rows.Read()) list.Add(new { id = rows.GetString(0), state = rows.GetString(1), attempts = rows.GetInt32(2),
            messageTs = rows.IsDBNull(3) ? null : rows.GetString(3), actor = rows.IsDBNull(4) ? null : rows.GetString(4),
            choice = rows.IsDBNull(5) ? null : rows.GetString(5) });
        return list;
    });

    public void ApplySlackDecision(SlackProfile profile, SlackClick click) => database.Write(s =>
    {
        var id = click.Notice.ToString();
        var raw = s.Text("""
            SELECT notice FROM slack_notices WHERE id=@id AND destination=@dest AND
              ((state='Sent' AND message_ts=@ts) OR (state='Unconfirmed' AND message_ts IS NULL AND @witness=1))
            """, ("id",id), ("dest",profile.Destination), ("ts",click.MessageTs), ("witness",click.WitnessedMessage ? 1 : 0))
            ?? throw new CommandConflict("This message has no matching delivery receipt.");
        var notice = JsonSerializer.Deserialize<SlackNotice>(raw, Wire.Json)!;
        if (!notice.Users.Contains(click.Actor) || !profile.DecisionUsers.Contains(click.Actor))
            throw new BadHttpRequestException("Actor is outside the decision scope.", StatusCodes.Status403Forbidden);
        if (!notice.Choices.Select((option,i) => option.Value == click.Value && click.Action == $"choice_{i}").Any(matches => matches))
            throw new BadHttpRequestException("Choice was not offered on this notice.");
        var previous = s.Text("SELECT actor FROM slack_notices WHERE id=@id", ("id",id));
        if (previous is not null)
        {
            if (previous == click.Actor && s.Text("SELECT choice FROM slack_notices WHERE id=@id", ("id",id)) == click.Value) return true;
            throw new CommandConflict("This notice already has a decision.");
        }
        var task = ReadTask(s,notice.TaskId);
        task.Gates = ReadGates(s,task.Id);
        if (task.Activity != Activity.Waiting || QuestionKey(task) != notice.Key ||
            !lifecycle.Choices(task).Any(o => o.Value == click.Value))
            throw new CommandConflict("The original question is no longer awaiting this choice.");
        lifecycle.Decide(task, click.Value, $"Slack {profile.TeamId}/{click.Actor}");
        Save(s,task);
        s.Execute("UPDATE slack_notices SET state='Sent',message_ts=@ts,actor=@actor,choice=@choice WHERE id=@id",
            ("id",id), ("ts",click.MessageTs), ("actor",click.Actor), ("choice",click.Value));
        return true;
    });
}
