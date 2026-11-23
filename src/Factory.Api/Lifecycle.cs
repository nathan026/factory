using System.Text.Json;

namespace Factory;

public sealed class Lifecycle(IControlledProject project, CiProfile? ciProfile = null, string? expertRuntime = null, string? ownerRuntime = null)
{
    public DecisionOption[] Choices(WorkItem task) => task.Advice?.Consultation?.Options ?? project.Choices(task.Source);

    public WorkItem Start(Guid id, string scenario, string? openingMessage = null, SourcePacket? source = null, WorkItem? prior = null, RoundApproval? followUp = null)
    {
        if (!project.Policy.Valid()) throw new CommandConflict("Project verification policy is invalid.");
        if (!project.ExpertProfile.Valid()) throw new CommandConflict("Invalid expert profile.");
        var task = new WorkItem { Id = id, OwnerId = prior?.OwnerId ?? Guid.NewGuid(), FollowUp = followUp, OpenedAt = prior?.OpenedAt ?? DateTimeOffset.UtcNow, OpeningMessage = openingMessage, Scenario = scenario, Goal = followUp?.Scope ?? project.Goal, Source = source ?? project.Snapshot(),
            ContractVersion = ContractGate.Version, GateRule = GateEvaluation.Rule, Policy = project.Policy, ExpertRuntime = expertRuntime, OwnerRuntime = ownerRuntime, ExpertProfile = project.ExpertProfile };
        var brief = followUp is not null && project is HostedRepositoryProject hosted ? hosted.FollowUpBrief(task.Source, followUp.Scope) : project.Describe(task.Source);
        task.Memory = OwnerMemory.Initialize(task, brief, legacy: false);
        if (prior is not null)
        {
            task.History.AddRange(prior.History);
            task.Gates.AddRange(prior.Gates);
            var memory = task.Memory;
            var revised = memory.Briefs[0] with { Version = prior.Memory!.Briefs.Count + 1 };
            memory.Briefs.Clear();
            memory.Briefs.AddRange(prior.Memory.Briefs);
            memory.Briefs.Add(revised);
            GateEvaluation.Record(task, GateInput.Capture(task), GateOutcome.Resume, "Operator approved follow-up work on this task.");
        }
        task.Record("Operator", followUp?.Scope ?? openingMessage ?? task.Goal);
        if (openingMessage is not null)
            task.Record("Lifecycle owner", $"Accepted goal for this task: {task.Goal}");
        return task;
    }

    public OwnerMemory Memory(WorkItem task) =>
        task.Memory ??= OwnerMemory.Initialize(task, project.Describe(task.Source), legacy: true);

    public void Advance(WorkItem task)
    {
        var wasQueued = task.Activity == Activity.Queued;
        var order = Prepare(task);
        if (!wasQueued && order is not null) Apply(task, order, order.Execute(project), Guid.NewGuid());
    }

    public WorkOrder? Prepare(WorkItem task)
    {
        if (task.Activity is not (Activity.Queued or Activity.Running)) return null;
        var input = GateInput.Capture(task);
        var order = PrepareCore(task);
        if (task.Activity == Activity.Blocked)
            GateEvaluation.Record(task, input, GateOutcome.Block, task.History[^1].Text);
        return order;
    }

    private WorkOrder? PrepareCore(WorkItem task)
    {
        if (task.Phase is not (Phase.Planning or Phase.Implementation or Phase.Verification or Phase.Review)
            || (task.Activity == Activity.Queued && task.Run is not null))
        {
            Block(task, "Invalid phase and activity combination for dispatch.");
            return null;
        }
        if (ciProfile is not null && GateInput.Hash(task.Policy) != GateInput.Hash(ciProfile.Rules.Policy))
        {
            Block(task, "Stored task policy differs from the admitted CI project.");
            return null;
        }
        var legacyContract = task.ContractVersion == 0 && task.Policy is null;
        if (task.GateRule is null || legacyContract)
        {
            if (task.Gates.Count > 0)
            {
                Block(task, "Missing rule version for a task with recorded gate evaluations.");
                return null;
            }
            AdoptHistoricalTask(task);
            // A pre-gate receipt has no durable evaluation. Requalify the sealed candidate.
            if (task.Phase == Phase.Review && task.Activity == Activity.Queued && !legacyContract)
            {
                task.Record("Controller", "Requalifying the historical candidate under the current verification gate.");
                Queue(task, Phase.Verification);
            }
        }
        if (task.GateRule != GateEvaluation.Rule)
        {
            Block(task, "Unsupported phase gate rule.");
            return null;
        }
        // Legacy checkpoints contain no policy or contract fields. Preserve their history and reissue
        // active fixture work. Review must obtain a newly executed, fully bound verifier receipt.
        if (task.ContractVersion == 0 && task.Policy is null && task.Results.Count == 0
            && task.Assignments.All(a => a.SchemaVersion == 0) && task.Receipts.All(r => r.SchemaVersion == 0))
        {
            task.ContractVersion = ContractGate.Version;
            task.Policy = project.Policy;
            task.Record("Controller", "Upgraded local checkpoint. Reissued pending work under the pinned verification policy.");
            Queue(task, task.Phase == Phase.Review ? Phase.Verification : task.Phase);
        }
        if (task.ContractVersion != ContractGate.Version || task.Policy?.Valid() != true)
        {
            Block(task, "Task has an unsupported contract or invalid pinned policy.");
            return null;
        }
        var memory = Memory(task);
        if (!memory.Matches(task) || ciProfile?.Rules.Blueprint is not null &&
            !memory.Documents.SequenceEqual(project.Describe(task.Source).Documents))
        {
            Block(task, "Owner brief or evidence differs from the retained task record.");
            return null;
        }
        var prerequisite = ContractGate.PrerequisiteError(task);
        if (prerequisite is null && task.Phase is Phase.Verification or Phase.Review)
            prerequisite = project.CandidateError(task.Candidate!);
        if (prerequisite is null && task.Phase is Phase.Verification or Phase.Review && ciProfile?.Rules.Blueprint is { } blueprint)
        {
            var writerGate = task.Gates.LastOrDefault(g => g.Input.RunId == task.Writer && g.Input.Phase == Phase.Implementation);
            var required = blueprint.EarlyChecks.Select(id => "local/" + id).Prepend("local/candidate-shape");
            if (writerGate?.Outcome != GateOutcome.Pass || !required.All(id => writerGate.Criteria.Any(c => c.Id == id && c.Passed)))
                prerequisite = "Mandatory local blueprint evidence is missing from the admitted writer gate.";
        }
        if (prerequisite is null && task.Phase == Phase.Review)
            prerequisite = task.Run is { } old && task.LegacyOperation == WorkOrder.OperationId(old, WorkAction.Review)
                ? ContractGate.ReceiptError(task, task.Receipts.LastOrDefault())
                    ?? (task.Receipts.LastOrDefault()?.Passed == true ? null : "Required verification checks failed.")
                : ReviewAdmissionError(task);
        if (prerequisite is not null)
        {
            Block(task, prerequisite);
            return null;
        }
        if (task.Gates.LastOrDefault() is { } prior &&
            (prior.NextPhase != task.Phase || prior.NextActivity is Activity.Blocked or Activity.Waiting or Activity.Delivered
             || (prior.NextActivity == Activity.Running && task.Activity != Activity.Running)))
        {
            Block(task, "The last gate did not authorize this phase and activity.");
            return null;
        }
        var input = task.Candidate is null ? task.Source.Fingerprint() : Wire.Digest(task.Candidate);
        if (task.Activity == Activity.Queued)
        {
            string objective = task.Phase switch
            {
                Phase.Planning => "Inspect the source and acceptance examples. Specify a bounded change.",
                Phase.Implementation when task.Repairs > 0 => "Repair the failed check using the accepted decision.",
                Phase.Implementation => "Implement the accepted plan within scope.",
                Phase.Verification => "Run every pinned acceptance example against the candidate.",
                Phase.Review when project.Ci?.RequiresCodex == true =>
                    "Inspect the sealed candidate against the accepted decision in a fresh Codex session.",
                Phase.Review => "Inspect the sealed candidate against the accepted decision in a fresh fixture session.",
                _ => throw new InvalidOperationException("Unexpected assignment")
            };
            task.Run = new(Guid.NewGuid(), task.Phase, objective, input, ContractGate.Version,
                task.Id, task.Source.Fingerprint(), task.Policy.Version, task.Policy.RequiredChecks.ToArray());
            task.Assignments.Add(task.Run);
            task.Activity = Activity.Running;
            task.Record("Lifecycle owner", objective);
        }

        var run = task.Run;
        var admitted = task.Assignments.LastOrDefault();
        if (run is null || admitted is null || run.Phase != task.Phase || !ContractGate.MatchesTask(run, task, input)
            || !ContractGate.MatchesTask(admitted, task, input) || run.Reference() != admitted.Reference()
            || run.Objective != admitted.Objective)
        {
            Block(task, "Active assignment does not match the admitted run and pinned task contract.");
            return null;
        }
        var action = ActionFor(task);
        if (action == WorkAction.AnswerWorker)
        {
            var origin = task.AskingRun!.Value;
            var q = task.Question;
            if (q is null || !CanDiscussWorkerQuestion(task, q, project.AdmittedQuestion(task.Scenario))
                || task.OwnerRuntime != "codex-subscription" || ownerRuntime != task.OwnerRuntime
                || !task.Results.Any(r => r.Assignment.RunId == origin && r.Status == ResultStatus.NeedsInput && r.OutputHash == GateInput.Hash(q))
                || !task.Gates.Any(g => g.Input.OperationId == $"{origin:N}.Ask" && g.Outcome == GateOutcome.Pass
                    && g.RuleVersion == GateEvaluation.Rule && g.Input.SessionId is not null && g.Input.TaskId == task.Id
                    && g.Input.SourceHash == run.SourceHash && g.Input.ProfileHash == GateInput.Hash(task.Policy)
                    && g.NextBriefHash == GateInput.Hash(memory.Briefs[^1]))
                || OwnerQuestion.Capture(task, origin, q.Text) is not { } request)
            {
                Block(task, "Worker follow-up lacks the original question, accepted decision or admitted owner runtime.");
                return null;
            }
            return new(run, action, new("", "", ""), task.Scenario, null, null, null, Discussion: request with { Recipient = run.Reference() });
        }
        if (action == WorkAction.Consult)
        {
            var request = task.ExpertProfile is { } profile ? ExpertRequest.Prepare(task, profile, memory.Briefs[^1]) : null;
            if (request is null || task.Question != project.AdmittedQuestion(task.Scenario)
                || !task.Gates.Any(g => g.Input.OperationId == $"{task.ExpertQuestionRunId:N}.Ask"
                    && g.Outcome == GateOutcome.Pass && ContractGate.CurrentBindings(task, g))
                || !task.Results.Any(r => r.Assignment.RunId == task.ExpertQuestionRunId
                    && r.Status == ResultStatus.NeedsInput && r.OutputHash == GateInput.Hash(task.Question)))
            {
                Block(task, "Expert request lacks its admitted question, current brief or pinned sources.");
                return null;
            }
            return new(run, action, new("", "", ""), task.Scenario, null, null, null, Consultation: request, ExpertRuntime: task.ExpertRuntime);
        }
        var feedback = action is WorkAction.Review || (action is WorkAction.Implement && task.Repairs > 0)
            ? task.Receipts.LastOrDefault() : null;
        var packet = memory.Packet(task, action) with { FollowUp = task.FollowUp };
        if (action == WorkAction.Implement && task.AskingRun is { } asking)
        {
            var reply = task.OwnerReply;
            var answerGate = task.Gates.LastOrDefault(g => g.Input.OperationId == $"{reply?.RecipientRun:N}.AnswerWorker");
            if (reply is null || answerGate is not { Outcome: GateOutcome.Pass, Input.SessionId: not null, Input.OutputHash: not null }
                || !ContractGate.CurrentBindings(task, answerGate) || reply.AskingRun != asking || reply.Question != task.Question?.Text
                || memory.Decisions.LastOrDefault() is not { } accepted || reply.Answer != OwnerQuestion.DecisionFact(accepted)
                || OwnerQuestion.Capture(task, asking, reply.Question)?.Evidence.Digest != reply.EvidenceDigest)
            {
                Block(task, "Implementation lacks the verified reply to its original question.");
                return null;
            }
            packet = packet with { OwnerReply = reply };
        }
        if (ciProfile?.Rules.Blueprint is { } registry)
            packet = registry.Hydrate(task, packet, ciProfile.Hash);
        return new(run, action, task.Source, task.Scenario, task.Answer, task.Candidate, feedback, packet);
    }

    private static void AdoptHistoricalTask(WorkItem task)
    {
        task.GateRule = GateEvaluation.Rule;
        task.LegacyWriter = task.Writer;
        task.LegacyPlanner = task.Assignments.LastOrDefault(a => a.Phase == Phase.Planning)?.RunId;
        task.LegacyOperation = task.Activity == Activity.Running && task.Run is { } legacyRun
            ? WorkOrder.OperationId(legacyRun, ActionFor(task)) : null;
    }

    public static WorkAction ActionFor(WorkItem task) => task.Phase switch
    {
        Phase.Planning => WorkAction.Plan,
        Phase.Implementation when task.Answer is null && task.ExpertQuestionRunId is not null => WorkAction.Consult,
        Phase.Implementation when task.Answer is null => WorkAction.Ask,
        Phase.Implementation when task.AskingRun is not null && task.OwnerReply is null => WorkAction.AnswerWorker,
        Phase.Implementation => WorkAction.Implement,
        Phase.Verification => WorkAction.Verify,
        Phase.Review => WorkAction.Review,
        _ => throw new InvalidDataException("Unexpected dispatched phase")
    };

    private string? ReviewAdmissionError(WorkItem task)
    {
        var receipt = task.Receipts.LastOrDefault();
        var reason = ContractGate.ReceiptError(task, receipt);
        if (reason is null && !receipt!.Passed) reason = "Required verification checks failed.";
        if (reason is null)
        {
            var pass = task.Gates.LastOrDefault(g => g.Input.RunId == receipt!.RunId && g.Input.Phase == Phase.Verification);
            if (pass?.Outcome != GateOutcome.Pass || pass.Input.SessionId is null
                || !ContractGate.CurrentBindings(task, pass) || pass.ReceiptHash != GateInput.Hash(receipt))
                reason = "No trusted verification gate covers this receipt and current task brief.";
        }
        if (reason is not null && receipt is not null) task.RejectedReceipt = new(task.Receipts.Count - 1, reason);
        return reason;
    }

    public void Apply(WorkItem task, WorkOrder order, JsonElement output, Guid? sessionId, bool providerVerified = false)
    {
        // Late or repeated callbacks cannot consume the current assignment.
        if (task.Activity != Activity.Running || task.Run?.Reference() != order.Assignment.Reference()
            || ActionFor(task) != order.Action) throw new CommandConflict("Result is for an inactive operation.");
        var input = GateInput.Capture(task, order, sessionId, output);
        var current = PrepareCore(task);
        if (current is null)
        {
            GateEvaluation.Record(task, input, GateOutcome.Block, task.History[^1].Text);
            return;
        }
        if (GateInput.Hash(order) != GateInput.Hash(current))
            throw new CommandConflict("Result packet differs from the current admitted operation.");
        var repairs = task.Repairs;
        var receiptCount = task.Receipts.Count;
        var resultCount = task.Results.Count;
        var criteria = new List<GateCriterion>();
        bool requalify = task.LegacyOperation == order.Id
            && (order.Action == WorkAction.Review || (order.Action == WorkAction.Verify && sessionId is null));
        if (requalify)
        {
            task.Record("Controller", "Requalifying historical evidence with fresh verification and review.");
            Queue(task, Phase.Verification);
        }
        else if ((sessionId is null || sessionId == Guid.Empty) && task.LegacyOperation != order.Id)
            Block(task, "Execution session is missing.");
        else if (ciProfile is not null && order.Action == WorkAction.Verify && !providerVerified)
            Block(task, "Verification requires matching source-bound CI provider evidence.");
        else if (sessionId is not null && task.Gates.Any(g => g.Input.SessionId == sessionId))
            Block(task, "Execution session was already used for another operation.");
        else
        {
            try
            {
                if (output.ValueKind != JsonValueKind.Object) throw new JsonException();
                ApplyResult(task, order, output, criteria);
                if (sessionId is null && order.Action == WorkAction.Plan && task.Activity != Activity.Blocked)
                    task.LegacyPlanner = order.Assignment.RunId;
                if (sessionId is null && order.Action == WorkAction.Implement && task.Writer == order.Assignment.RunId)
                    task.LegacyWriter = task.Writer;
            }
            catch (JsonException) { Block(task, "Worker output does not match the result contract."); }
        }
        var outcome = task.Activity switch
        {
            Activity.Blocked => GateOutcome.Block,
            Activity.Waiting => GateOutcome.Escalate,
            _ when task.Repairs > repairs || requalify => GateOutcome.Revise,
            _ => GateOutcome.Pass
        };
        var receipt = task.Receipts.Count > receiptCount ? task.Receipts[^1] : null;
        if (receipt is not null)
        {
            var valid = task.RejectedReceipt?.Index != task.Receipts.Count - 1;
            foreach (var id in task.Policy!.RequiredChecks)
            {
                var check = receipt.Checks?.FirstOrDefault(c => c?.Id == id);
                criteria.Add(new(id, valid && check?.Passed == true, valid ? check!.Assertion : "Receipt rejected: " + task.RejectedReceipt!.Reason));
            }
        }
        if (task.Results.Count > resultCount)
        {
            criteria.Add(new("assigned-result", true, "Result matches the admitted run, input, source and policy."));
            if (order.Action == WorkAction.Plan && task.PlanningEvidence is { } plan)
                criteria.AddRange(plan.RequiredChecks.Select(id => new GateCriterion(id, true, "Covered by the scoped plan.")));
            if (order.Action == WorkAction.Review && task.Review?.Evidence is { } review)
            {
                criteria.Add(new("independent-session", true, "A fresh reviewer inspected the admitted candidate and verification evidence."));
                criteria.AddRange(review.Findings.Select(f => new GateCriterion(f.Criterion, f.Severity != FindingSeverity.Blocking,
                    f.Evidence + " " + f.Recommendation)));
            }
        }
        GateEvaluation.Record(task, input, outcome, task.History[^1].Text, criteria, receipt);
    }

    private void ApplyResult(WorkItem task, WorkOrder order, JsonElement output, List<GateCriterion> criteria)
    {
        var run = order.Assignment;
        switch (order.Action)
        {
            case WorkAction.Plan:
                var plan = output.Deserialize<WorkerResult<string>>(Wire.ResultJson)!;
                var legacyPlan = task.LegacyOperation == order.Id || task.Memory?.LegacyOperationId == order.Id;
                if (!Accept(task, run, plan, ResultStatus.Completed,
                    _ => legacyPlan && plan.PlanEvidence is null ? null : ContractGate.PlanError(task, plan.PlanEvidence))) return;
                task.PlanningEvidence = plan.PlanEvidence;
                task.Plan = plan.Output;
                Memory(task).RecordPlan(task.Plan);
                task.Record("Planner", task.Plan);
                Queue(task, Phase.Implementation);
                break;
            case WorkAction.Ask:
                var question = output.Deserialize<WorkerResult<Question>>(Wire.ResultJson)!;
                if (!Accept(task, run, question, ResultStatus.NeedsInput)) return;
                ResolveQuestion(task, question.Output);
                break;
            case WorkAction.AnswerWorker:
                var request = order.Discussion!;
                var facts = JsonSerializer.Deserialize<string[]>(request.Evidence.Content)!;
                var decision = Memory(task).Decisions.LastOrDefault();
                if (!output.TryGetProperty("requestHash", out var hash) || hash.GetString() != GateInput.Hash(request)
                    || !output.TryGetProperty("fact", out var selected) || !selected.TryGetInt32(out var index)
                    || index < 0 || index >= facts.Length || decision is null || decision.Decision != task.Answer
                    || facts[index] != OwnerQuestion.DecisionFact(decision))
                {
                    Block(task, "The owner did not establish that the accepted decision answers the worker question.");
                    break;
                }
                task.OwnerReply = new(request.MessageId, run.RunId, request.Text, facts[index], request.Evidence.Digest);
                task.Record("Lifecycle owner", $"Answered implementation assignment {run.RunId}: {facts[index]}");
                break;
            case WorkAction.Consult:
                ApplyOpinion(task, order.Consultation!, output.Deserialize<ExpertOpinion>(Wire.ResultJson));
                break;
            case WorkAction.Implement:
                var implementation = output.Deserialize<WorkerResult<string>>(Wire.ResultJson)!;
                if (!Accept(task, run, implementation, ResultStatus.Completed, candidate =>
                {
                    var error = project.CandidateError(candidate);
                    if (ciProfile?.Rules.Blueprint is not null)
                        criteria.Add(new("local/candidate-shape", error is null, error ?? "Candidate conforms to the installed project format."));
                    return error;
                })) return;
                task.Candidate = implementation.Output;
                task.Writer = run.RunId;
                task.Record("Implementer", $"Submitted {Wire.Digest(task.Candidate)} and reported success.");
                if (ciProfile?.Rules.Blueprint is { } blueprint)
                {
                    var localChecks = project.LocalChecks(task.Candidate, task.Answer!, task.Source, blueprint.EarlyChecks);
                    if (localChecks.Count != blueprint.EarlyChecks.Length ||
                        !localChecks.Select(c => c.Id).ToHashSet().SetEquals(blueprint.EarlyChecks))
                    {
                        Block(task, "The local check results do not match the required project selectors.");
                        return;
                    }
                    foreach (var check in localChecks)
                        criteria.Add(new("local/" + check.Id, check.Passed, check.Assertion));
                    var failed = criteria.Where(c => !c.Passed).ToArray();
                    if (failed.Length > 0)
                    {
                        var repeated = task.Gates.Any(g => g.Outcome == GateOutcome.Revise && g.Input.Phase == Phase.Implementation
                            && g.ArtifactHash == Wire.Digest(task.Candidate) && ContractGate.CurrentBindings(task, g)
                            && failed.All(c => g.Criteria.Contains(c)));
                        if (repeated || task.Repairs >= task.Policy!.MaxRepairs)
                            Block(task, "Local checks repeated an unchanged failure or exhausted the project repair budget.");
                        else
                        {
                            task.Record("Controller", "Local checks require repair before CI. " + string.Join(" ", failed.Select(c => c.Finding)));
                            task.Repairs++;
                            Queue(task, Phase.Implementation);
                        }
                        break;
                    }
                }
                Queue(task, Phase.Verification);
                break;
            case WorkAction.Verify:
                Verify(task, output.Deserialize<Receipt>(Wire.ResultJson)!);
                break;
            case WorkAction.Review:
                var result = output.Deserialize<WorkerResult<Review>>(Wire.ResultJson)!;
                if (!Accept(task, run, result, ResultStatus.Completed, review => ContractGate.ReviewError(task, review))) return;
                var review = result.Output;
                task.Review = review;
                task.Record(project.Ci?.RequiresCodex == true ? "Codex reviewer" : "Fixture reviewer", review.Finding);
                if (review.RunId == task.Writer || !review.Approved)
                {
                    if (review.RunId != task.Writer && task.Repairs < task.Policy!.MaxRepairs
                        && review.Evidence!.Findings.Any(f => f.Severity == FindingSeverity.Blocking))
                    {
                        task.Repairs++;
                        Queue(task, Phase.Implementation);
                    }
                    else Block(task, "Independent review has not approved this candidate within the repair budget.");
                    break;
                }
                task.Phase = Phase.Delivery;
                task.Activity = Activity.Delivered;
                task.Run = null;
                task.Record("Controller", "Local evidence package ready for inspection.");
                break;
        }
    }

    private static bool Accept<T>(WorkItem task, Assignment run, WorkerResult<T> result, ResultStatus expected,
        Func<T, string?>? evidenceError = null)
    {
        bool hasOutput = result.Output switch
        {
            string text => !string.IsNullOrWhiteSpace(text),
            Question q => !string.IsNullOrWhiteSpace(q.Subject) && !string.IsNullOrWhiteSpace(q.Text),
            Review r => r.RunId == run.RunId && r.CandidateHash == run.InputHash && !string.IsNullOrWhiteSpace(r.Finding),
            _ => false
        };
        if (result.Assignment != run.Reference() || result.Status != expected || !hasOutput)
        {
            Block(task, "Worker result does not match the admitted assignment or expected outcome.");
            return false;
        }
        if (evidenceError?.Invoke(result.Output) is { } reason)
        {
            Block(task, reason);
            return false;
        }
        task.Results.Add(new(run.Reference(), result.Status, Wire.Digest(JsonSerializer.Serialize(result.Output, Wire.Json))));
        return true;
    }

    private static bool CanDiscussWorkerQuestion(WorkItem task, Question question, Question admitted) =>
        task.OwnerRuntime == "codex-subscription" && !admitted.Reserved && !question.Reserved
        && question.Subject == admitted.Subject && !string.IsNullOrWhiteSpace(question.Text) && question.Text.Length <= 4000;

    private void ResolveQuestion(WorkItem task, Question question)
    {
        task.Question = question;
        task.Record("Implementer", question.Text);
        var memory = Memory(task);
        var brief = memory.Briefs[^1];
        // In the controlled project the question contract is known at admission. Worker prose
        // and the worker's requested authority cannot select a different route.
        var admitted = project.AdmittedQuestion(task.Scenario);
        var answer = task.FollowUp is { } approval && project is HostedRepositoryProject hosted
            ? new Decision("repository-scope", admitted.Subject, approval.Scope, hosted.Ci.Rules.RepositoryLayout!.Source)
            : project.Investigate(admitted, task.Source, brief);
        if (question != admitted && (answer is null || !CanDiscussWorkerQuestion(task, question, admitted)))
        {
            Block(task, "Question differs from the admitted subject or authority.");
            return;
        }
        if (answer is not null)
        {
            task.Answer = answer;
            var sources = string.Join(", ", memory.Documents.Select(d => d.Path));
            var rationale = $"{answer.Id} resolves brief v{brief.Version} using the pinned source and acceptance examples in {sources}.";
            task.Record("Lifecycle owner", $"{rationale} {answer.Text}");
            memory.RecordDecision(task, rationale, DateTimeOffset.UtcNow);
            if (!question.Reserved && task.OwnerRuntime == "codex-subscription")
                task.AskingRun = task.Run!.RunId;
            return;
        }
        var profile = task.ExpertProfile ??= project.ExpertProfile;
        var request = ExpertRequest.Prepare(task, profile, brief);
        if (request is null)
        {
            Block(task, "Expert consultation exceeds its admitted document scope or byte limit.");
            return;
        }
        task.ExpertQuestionRunId = task.Run!.RunId;
        task.Record("Lifecycle owner", $"Requested scoped advice from {profile.Version} for brief v{brief.Version}.");
    }

    private void ApplyOpinion(WorkItem task, ExpertRequest request, ExpertOpinion? opinion)
    {
        var question = request.Question;
        var memory = Memory(task);
        var brief = memory.Briefs[^1];
        var profile = task.ExpertProfile!;
        if (opinion is null || !opinion.Supports(request))
        {
            Block(task, "Expert advice lacks grounded evidence for this consultation.");
            return;
        }
        var citations = request.Sources.Where(s => opinion.Citations.Contains(s.Path))
            .Select(s => new ExpertCitation(s.Path, s.Digest)).ToArray();
        var options = question.Reserved ? project.Choices(task.Source) : [];
        if (question.Reserved && (options.Length == 0 || options.Any(c => string.IsNullOrWhiteSpace(c.Value)
            || string.IsNullOrWhiteSpace(c.Label)) || options.Select(c => c.Value).Distinct().Count() != options.Length))
        {
            Block(task, "The project supplied no unambiguous decision options for this question.");
            return;
        }
        var consultation = new Consultation(task.Id, request.RunId, question, brief.Version, request.BriefHash,
            request.Revision, profile.Version, profile.Domain, question.Reserved ? "Operator" : "Lifecycle owner",
            opinion.RequestHash, opinion.Conclusion, opinion.Assumption, opinion.Uncertainty, citations, opinion.SuggestedOption, options);
        task.Advice = new(string.Join(", ", opinion.Citations), opinion.Recommendation, opinion.Alternative,
            opinion.Consequence, consultation);
        task.Record("Scoped expert", $"{profile.Version}: {opinion.Conclusion} {opinion.Recommendation}");
        if (!question.Reserved &&
            project.ResolveWithinScope(question, opinion.SuggestedOption, task.Source, brief) is { } resolved)
        {
            task.Answer = resolved;
            var rationale = $"The owner checked {profile.Version}'s recommendation against brief v{brief.Version} and pinned project evidence.";
            task.Record("Lifecycle owner", $"{rationale} {resolved.Text}");
            memory.RecordDecision(task, rationale, DateTimeOffset.UtcNow, authority: "Delegated implementation decision");
            return;
        }
        task.Run = null;
        task.Activity = question.Reserved ? Activity.Waiting : Activity.Blocked;
        task.Record("Lifecycle owner", question.Reserved ? "Awaiting your product decision. Coding slot released."
            : "Expert evidence did not establish an authorized resolution. A different investigation is required.");
    }

    public void Decide(WorkItem task, string choice, string actor = "Operator")
    {
        if (task.HumanChoice == choice) return;
        if (task.Activity != Activity.Waiting || task.Question?.Reserved != true)
            throw new CommandConflict("This task has no pending product decision.");
        bool historical = task.GateRule is null && task.Gates.Count == 0;
        if (historical) AdoptHistoricalTask(task);
        var memory = Memory(task);
        if (task.GateRule != GateEvaluation.Rule || !memory.Matches(task)
            || (!historical && (task.Gates.LastOrDefault() is not { Outcome: GateOutcome.Escalate, NextActivity: Activity.Waiting } last
                || !ContractGate.CurrentBindings(task, last)
                || !last.MatchesConsultation(task)
                || !task.Results.Any(r => r.Assignment.RunId == (task.ExpertQuestionRunId ?? last.Input.RunId)
                    && r.Status == ResultStatus.NeedsInput && r.OutputHash == GateInput.Hash(task.Question)))))
            throw new CommandConflict("No gate has authorized this operator decision.");
        var input = GateInput.Capture(task);
        var resolution = project.ApplyChoice(choice, task.Source);
        task.Answer = resolution.Decision;
        task.HumanChoice = choice;
        task.Record(actor, $"Decision recorded. {task.Answer.Text}");
        memory.RecordDecision(task, $"Operator selected '{choice}' for the reserved question '{task.Question.Text}'.",
            DateTimeOffset.UtcNow, resolution.Supersedes, actor: actor);
        Queue(task, Phase.Implementation);
        GateEvaluation.Record(task, input, GateOutcome.Pass, "Operator decision authorized resumption.");
    }

    private void Verify(WorkItem task, Receipt receipt)
    {
        task.Receipts.Add(receipt);
        var error = ContractGate.ReceiptError(task, receipt) ??
            (ciProfile is null ? project.VerificationError(receipt, task.Answer!, task.Source) : null);
        if (error is not null)
        {
            task.RejectedReceipt = new(task.Receipts.Count - 1, error);
            Block(task, error);
            return;
        }
        if (receipt.Passed)
        {
            task.Record("Verifier", "All required checks passed against the submitted candidate.");
            Queue(task, Phase.Review);
        }
        else if (task.Receipts.Count >= 2 && task.Receipts[^2] is { Passed: false } previous
            && previous.CandidateHash == receipt.CandidateHash && previous.PolicyVersion == receipt.PolicyVersion
            && task.Gates.Any(g => g.Outcome == GateOutcome.Revise && g.ReceiptHash == GateInput.Hash(previous)
                && g.Input.BriefHash == GateInput.Hash(task.Memory!.Briefs[^1]))
            && previous.Checks.Where(c => !c.Passed).Select(c => c.Id).ToHashSet()
                .SetEquals(receipt.Checks.Where(c => !c.Passed).Select(c => c.Id)))
        {
            Block(task, "Repair repeated the same candidate and failed checks. Investigate a different correction before continuing.");
        }
        else if (task.Repairs < task.Policy!.MaxRepairs)
        {
            task.Record("Verifier", "Gate failed. " + string.Join(", ", receipt.Checks.Where(c => !c.Passed).Select(c => c.Assertion)));
            task.Repairs++;
            Queue(task, Phase.Implementation);
        }
        else Block(task, "Verification failed after the permitted repair.");
    }

    private static void Queue(WorkItem task, Phase phase)
    {
        task.Phase = phase;
        task.Activity = Activity.Queued;
        task.Run = null;
    }

    public static void Block(WorkItem task, string reason)
    {
        task.Record("Controller", reason);
        task.Activity = Activity.Blocked;
        task.Run = null;
    }
}
