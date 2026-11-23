using System.Text;
using System.Text.Json;

namespace Factory;

public record WorkerReply(Guid AskingRun, Guid RecipientRun, string Question, string Answer, string EvidenceDigest);

public record OwnerQuestion(Guid TaskId, Guid OwnerId, Guid MessageId, string Text, string SourceRevision,
    int BriefVersion, string BriefHash, ExpertSource Evidence,
    [property: System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
    RunReference? Recipient = null,
    [property: System.Text.Json.Serialization.JsonIgnore(Condition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull)]
    ScopePlanning? ScopePlanning = null)
{
    public const string Path = "owner/accepted.json";

    public bool Valid(Assignment assignment) => TaskId == assignment.TaskId && OwnerId != Guid.Empty
        && MessageId != Guid.Empty && Text.Length is > 0 and <= 4000 && BriefVersion > 0
        && SourceRevision == assignment.SourceHash && (Recipient is null ? BriefHash == assignment.InputHash : Recipient == assignment.Reference())
        && Evidence.Path == Path && Evidence.Digest == Wire.Digest(Evidence.Content)
        && (ScopePlanning is { } planning
            ? Recipient is null && planning.BaseSha == SourceRevision && planning.Valid() && Encoding.UTF8.GetByteCount(Evidence.Content) <= 49152
            : Encoding.UTF8.GetByteCount(Evidence.Content) <= 16384);

    public static string DecisionFact(DecisionRecord d) =>
        $"{d.Decision.Id} ({d.Authority}). {d.Decision.Text} Evidence: {string.Join(", ", d.Sources)}.";

    public static OwnerQuestion? Capture(WorkItem task, Guid message, string text)
    {
        if (task.Memory is not { } memory || !memory.Matches(task) || task.Policy is null) return null;
        var brief = memory.Briefs[^1];
        var facts = memory.Decisions.Where(d => d.Scope == task.Id &&
            !memory.Decisions.Any(next => next.Supersedes == d.Decision.Id))
            .Select(DecisionFact)
            .Concat(brief.Requirements.Criteria.Select(c => $"{c.Id} (brief v{brief.Version}). {c.Text}")).ToArray();
        var content = JsonSerializer.Serialize(facts, Wire.Json);
        if (facts.Length == 0 || Encoding.UTF8.GetByteCount(content) > 16384) return null;
        return new(task.Id, task.OwnerId, message, text, task.Source.Fingerprint(), brief.Version, GateInput.Hash(brief),
            new(Path, Wire.Digest(content), content));
    }
}

public sealed partial class TaskStore
{
    private static bool IsDiscussion(string id) => id.EndsWith(".Discuss", StringComparison.Ordinal);

    private Discussion QueueDiscussion(Session s, WorkItem task, Discussion exchange)
    {
        var question = OwnerQuestion.Capture(task, exchange.Id, exchange.Text);
        if (question is null)
            return exchange with { Status = "Unavailable", Reply = "The owner could not establish a bounded, pinned evidence record for this question." };

        // Identical questions on the same checkpoint can use the verified answer even with a new message ID.
        // Include the utterance, brief, owner and source revision in the comparison.
        using (var cmd = s.Command("""
            SELECT o.request,d.exchange FROM discussion_jobs j JOIN dispatch_outbox o ON o.id=j.operation
            JOIN task_discussion d ON d.task_id=j.task_id AND d.message_id=j.message_id
            WHERE j.task_id=@task AND j.pending=0 ORDER BY d.seq DESC
            """, ("task",task.Id.ToString())))
        using (var rows = cmd.ExecuteReader())
        {
            while (rows.Read())
            {
                var prior = JsonSerializer.Deserialize<Discussion>(rows.GetString(1), Wire.Json)!;
                if (prior.Text != exchange.Text || prior.Status != "Answered") continue;
                var admitted = JsonSerializer.Deserialize<WorkOrder>(rows.GetString(0), Wire.Json)!.Discussion;
                if (admitted == (question with { MessageId = prior.Id }))
                    return exchange with { Reply = prior.Reply, Status = prior.Status,
                        Operation = prior.Operation, EvidenceHash = prior.EvidenceHash };
            }
        }
        return QueueOwnerTurn(s, task, exchange, question);
    }

    private Discussion QueueScopeDiscussion(Session s, Guid taskId, Guid ownerId, string baseSha, Guid messageId,
        string text, string checkpoint, string contextJson, ExpertSource[] files)
    {
        var task = ReadTask(s, taskId);
        var exchange = new Discussion(messageId, task.History.Count, text, "", DateTimeOffset.UtcNow);
        if (!liveOwner)
            return exchange with { Status = "Unavailable", Reply = "Subscription-backed owner discussion is disabled on this deployment." };
        var question = new OwnerQuestion(taskId,ownerId,messageId,text,baseSha,1,checkpoint,
            new(OwnerQuestion.Path, Wire.Digest(contextJson), contextJson), ScopePlanning: new(baseSha,files,RequireAssessment: true));
        if (ownerId != task.OwnerId || ciProfile?.Rules.RepositoryLayout is not { } layout ||
            !files.All(f => layout.Paths.Contains(f.Path)))
            throw new CommandConflict("The planning owner or its read permissions changed.");
        return QueueOwnerTurn(s, task, exchange, question);
    }

    private Discussion QueueOwnerTurn(Session s, WorkItem task, Discussion exchange, OwnerQuestion question)
    {
        if (s.Text("SELECT operation FROM discussion_jobs WHERE task_id=@task AND pending=1", ("task", task.Id.ToString())) is not null)
            throw new CommandConflict("The owner is already answering a question on this task.");

        var assignment = new Assignment(Guid.NewGuid(), task.Phase, question.ScopePlanning is null
            ? "Answer a question from accepted task evidence" : "Discuss the requested work and propose a scope",
            question.BriefHash, 1, task.Id, question.SourceRevision,
            question.ScopePlanning is null ? task.Policy!.Version : ciProfile!.Rules.Policy.Version, []);
        if (!question.Valid(assignment)) throw new CommandConflict("The owner discussion exceeds the admitted evidence bounds.");
        var order = new WorkOrder(assignment, WorkAction.Discuss, new("", "", ""), task.Scenario, null, null, null, Discussion: question);
        s.Execute("INSERT INTO dispatch_outbox(id,task_id,run_id,request) VALUES(@id,@task,@run,@request)",
            ("id",order.Id), ("task",task.Id.ToString()), ("run",assignment.RunId.ToString()), ("request",Encode(order)));
        s.Execute("INSERT INTO discussion_jobs(operation,task_id,message_id,deadline,pending) VALUES(@id,@task,@msg,@deadline,1)",
            ("id",order.Id), ("task",task.Id.ToString()), ("msg",exchange.Id.ToString()),
            ("deadline",Math.Min(Deadline(), DateTimeOffset.UtcNow.AddHours(23).ToUnixTimeSeconds())));
        return exchange with { Status = "Pending", Operation = order.Id, Reply = question.ScopePlanning is null
            ? "The owner is checking the accepted task evidence." : "The owner is reading the issue and repository files." };
    }

    private bool CurrentDiscussion(Session s, WorkItem task, OwnerQuestion question) =>
        question.ScopePlanning is not null ? task.OwnerId == question.OwnerId && ScopeDiscussionCurrent(s,question) :
        !(task.Activity == Activity.Waiting && task.Question is { Reserved: true }) &&
        OwnerQuestion.Capture(task, question.MessageId, question.Text) == question;

    private static void FinishDiscussion(Session s, string id, string status, string reply, string? evidence = null, ScopeProposal? proposal = null, ScopeAssessment? assessment = null)
    {
        var raw = s.Text("""
            SELECT d.exchange FROM task_discussion d JOIN discussion_jobs j ON j.task_id=d.task_id AND j.message_id=d.message_id
            WHERE j.operation=@id AND j.pending=1
            """, ("id",id));
        if (raw is null) return;
        var prior = JsonSerializer.Deserialize<Discussion>(raw, Wire.Json)!;
        var saved = prior with { Status = status, Reply = reply, EvidenceHash = evidence, Proposal = proposal, Assessment = assessment };
        s.Execute("UPDATE task_discussion SET exchange=@exchange WHERE message_id=@msg AND task_id=(SELECT task_id FROM discussion_jobs WHERE operation=@id)",
            ("exchange",Encode(saved)), ("msg",prior.Id.ToString()), ("id",id));
        s.Execute("UPDATE discussion_jobs SET pending=0 WHERE operation=@id", ("id",id));
        s.Execute("UPDATE dashboard_queue SET revision=revision+1 WHERE task_id=(SELECT task_id FROM discussion_jobs WHERE operation=@id)", ("id",id));
    }

    private static void ExpireDiscussions(Session s)
    {
        using var cmd = s.Command("SELECT operation FROM discussion_jobs WHERE pending=1 AND deadline<=@now",
            ("now",DateTimeOffset.UtcNow.ToUnixTimeSeconds()));
        var ids = new List<string>();
        using (var rows = cmd.ExecuteReader()) while (rows.Read()) ids.Add(rows.GetString(0));
        foreach (var id in ids) FinishDiscussion(s, id, "Expired", "The owner discussion expired before a verified answer was received.");
    }

    private DispatchTicket? DiscussionTicket(Session s)
    {
        var id = s.Text("SELECT operation FROM discussion_jobs WHERE pending=1 AND deadline>@now ORDER BY deadline,operation LIMIT 1",
            ("now",DateTimeOffset.UtcNow.ToUnixTimeSeconds()));
        if (id is null) return null;
        if (!liveOwner)
        {
            FinishDiscussion(s, id, "Unavailable", "The admitted owner runtime is disabled on this deployment.");
            return new(true);
        }
        s.Execute("UPDATE dispatch_outbox SET first_submission=COALESCE(first_submission,@now) WHERE id=@id",
            ("now",DateTimeOffset.UtcNow.ToUnixTimeSeconds()), ("id",id));
        return new(false,id,s.Text("SELECT scheduler_id FROM dispatch_outbox WHERE id=@id", ("id",id)));
    }

    private static bool OpenDiscussion(Session s, string id) =>
        s.Text("SELECT operation FROM discussion_jobs WHERE operation=@id AND pending=1 AND deadline>@now",
            ("id",id), ("now",DateTimeOffset.UtcNow.ToUnixTimeSeconds())) is not null;

    private bool ApplyDiscussion(Session s, WorkOrder order, string token, JsonElement output, string session)
    {
        if (!OpenDiscussion(s, order.Id))
        {
            if (s.Text("SELECT id FROM dispatch_inbox WHERE id=@id AND claim=@claim AND applied=1 AND output=@output",
                    ("id",order.Id), ("claim",token), ("output",Encode(output))) is not null) return true;
            throw new CommandConflict("This discussion is closed or expired.");
        }
        if (!liveOwner) throw new CommandConflict("The admitted owner runtime is disabled.");
        CodexEvidence.Require(order, output, null, session);
        Receive(s, order.Id, token, output);
        var question = order.Discussion!;
        var current = ReadTask(s, question.TaskId);
        if (!CurrentDiscussion(s, current, question))
            FinishDiscussion(s, order.Id, "Stale", "The task evidence changed before the owner answered.");
        else if (question.ScopePlanning is not null)
        {
            var result = output.Deserialize<ScopeReply>(Wire.ResultJson)!;
            var proposal = result.Proposal;
            var assessment = result.ReadAssessment();
            var requestHash = GateInput.Hash(question);
            if (assessment is null && (question.ScopePlanning.RequireAssessment || result.Assessment.ValueKind != JsonValueKind.Undefined))
                FinishDiscussion(s, order.Id, "NeedsAssessment", result.DiscussionText(), requestHash);
            else if (assessment?.HasOpenItems() == true)
                FinishDiscussion(s, order.Id, "Answered", result.DiscussionText(), requestHash, assessment: assessment);
            else if (proposal is not null && !proposal.Valid())
                FinishDiscussion(s, order.Id, "NeedsScope", result.DiscussionText(), requestHash, assessment: assessment);
            else
            {
                if (proposal is not null) SaveOwnerProposal(s, question, proposal);
                FinishDiscussion(s, order.Id, "Answered", result.Reply, requestHash, proposal, assessment);
            }
        }
        else
        {
            var index = output.GetProperty("fact").GetInt32();
            var facts = JsonSerializer.Deserialize<string[]>(question.Evidence.Content)!;
            FinishDiscussion(s, order.Id, index < 0 ? "Unanswered" : "Answered",
                index < 0 ? "The accepted task record does not establish an answer." : facts[index], question.Evidence.Digest);
        }
        s.Execute("UPDATE dispatch_inbox SET applied=1 WHERE id=@id", ("id",order.Id));
        return true;
    }
}
