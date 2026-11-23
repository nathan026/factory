using System.Text.Json;
using System.Text.Json.Serialization;

namespace Factory;

public enum WorkAction { Plan, Ask, Implement, Verify, Review, Consult, Discuss, AnswerWorker }

public record WorkOrder(Assignment Assignment, WorkAction Action, SourcePacket Source, string Scenario,
    Decision? Answer, string? Candidate, Receipt? Feedback,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    PhaseContext? Context = null,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] ExpertRequest? Consultation = null,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? ExpertRuntime = null,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] OwnerQuestion? Discussion = null)
{
    // A resolved question can resume implementation in the same assigned run.
    public string Id => OperationId(Assignment, Action);
    public static string OperationId(Assignment assignment, WorkAction action) => $"{assignment.RunId:N}.{action}";

    public void Validate(IControlledProject project)
    {
        if (Action is WorkAction.Discuss or WorkAction.AnswerWorker)
        {
            if (Discussion is null || !Discussion.Valid(Assignment) || (Discussion.Recipient is null) != (Action == WorkAction.Discuss)
                || ExpertRuntime is not null || Consultation is not null
                || Context is not null || Answer is not null || Candidate is not null || Feedback is not null
                || Source != new SourcePacket("", "", "") || Assignment.PolicyVersion != project.Policy.Version)
                throw new InvalidDataException("Owner discussion differs from its admitted evidence.");
            return;
        }
        if (Discussion is not null) throw new InvalidDataException("Unexpected discussion packet.");
        if (Action == WorkAction.Consult)
        {
            if (ExpertRuntime is not (null or "codex-subscription") || Consultation is not { } request || Context is not null || Source != new SourcePacket("", "", "")
                || Answer is not null || Candidate is not null || Feedback is not null
                || request.TaskId != Assignment.TaskId || request.RunId != Assignment.RunId
                || request.Revision != Assignment.SourceHash || request.Question != project.AdmittedQuestion(Scenario)
                || !request.Within(project.ExpertProfile))
                throw new InvalidDataException("Consultation differs from the admitted read-only scope.");
            return;
        }
        if (Consultation is not null || ExpertRuntime is not null) throw new InvalidDataException("Unexpected expert packet.");
        if (Context is { } packet &&
            (packet.SchemaVersion != 1 || packet.OwnerId == Guid.Empty || packet.Brief.Version < 1
             || packet.Assignment != Assignment.Reference() || packet.Action != Action
             || packet.Decisions.Any(d => d.Scope != Assignment.TaskId)
             || packet.Brief.AcceptedDecision != Answer || packet.Policy.Version != Assignment.PolicyVersion
             || !packet.Policy.RequiredChecks.SequenceEqual(Assignment.RequiredChecks)
             || Source.Fingerprint() != Assignment.SourceHash
             || packet.Documents is not { Length: > 0 } || packet.Documents.Any(d => !d.Matches(Source, Assignment.SourceHash))))
            throw new InvalidDataException("Phase context does not match the admitted work order.");
        if (Context?.FollowUp is { } approved)
            approved.Validate(this, project);
        else if (project.Ci?.Rules.RepositoryLayout is not null && (Source != project.Snapshot() ||
            project is HostedRepositoryProject && Context?.Brief.Goal != project.Goal))
            throw new InvalidDataException("Repository input differs from the profile pinned to Git.");
        BlueprintPolicy.CheckGrant(project, this);
    }

    public JsonElement Execute(IControlledProject project)
    {
        Validate(project);
        if (Action is WorkAction.Discuss or WorkAction.AnswerWorker || ExpertRuntime is not null || project.Ci?.RequiresCodex == true && Action is WorkAction.Implement or WorkAction.Review)
            throw new InvalidOperationException("This policy requires a native Codex worker.");
        object result = Action switch
        {
            WorkAction.Consult => project.Consult(Consultation!) ?? throw new InvalidDataException("No scoped expert response."),
            WorkAction.Plan => project.Plan(Assignment, Context),
            WorkAction.Ask => project.Ask(Assignment, Scenario, Context),
            WorkAction.Implement => project.Implement(Assignment, Source, Answer!, Feedback, Context),
            WorkAction.Verify => project.Verify(Assignment, Candidate!, Answer!, Source, Context),
            WorkAction.Review => project.Assess(Assignment, Candidate!, Answer!, Context),
            _ => throw new InvalidDataException("Unsupported fixture operation")
        };
        return JsonSerializer.SerializeToElement(result, Wire.Json);
    }
}
