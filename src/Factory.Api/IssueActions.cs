using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Factory;

public sealed partial class GitHubRepository
{
    internal async Task<(bool Merged, bool Open)> PullState(DeliveredRound round)
    {
        var pr = (await Call($"pulls/{round.Draft.Number}"))!;
        if ((int?)pr["number"] != round.Draft.Number || (long?)pr["base"]?["repo"]?["id"] != config.RepositoryId ||
            (long?)pr["head"]?["repo"]?["id"] != config.RepositoryId || (string?)pr["base"]?["ref"] != config.BaseBranch ||
            (string?)pr["head"]?["ref"] != round.Intent.Branch || (string?)pr["head"]?["sha"] != round.Draft.HeadSha ||
            !((string?)pr["body"] ?? "").Split('\n').Any(s => s.TrimEnd('\r') == round.Intent.Marker) ||
            pr["merged"] is not JsonValue merged || !merged.TryGetValue<bool>(out var value))
            throw new CommandConflict("The pull request no longer matches the recorded delivery.");
        return (value, (string?)pr["state"] == "open");
    }

    internal async Task<long?> FindActionComment(int number, string body)
    {
        for (var page = 1; page <= 100; page++)
        {
            var comments = (await Call($"issues/{number}/comments?per_page=100&page={page}"))!.AsArray();
            foreach (var comment in comments)
                if ((string?)comment?["body"] == body && string.Equals((string?)comment["user"]?["login"], config.IssueActionBot, StringComparison.OrdinalIgnoreCase))
                    return (long?)comment["id"] ?? throw new GitHubFailure("Missing comment identity.");
            if (comments.Count < 100) return null;
        }
        throw new CommandConflict("The comment search limit was reached. Inspect GitHub before retrying.");
    }

    internal async Task<long> PostActionComment(int number, string body)
    {
        var comment = (await Call($"issues/{number}/comments", new { body }))!;
        if ((string?)comment["body"] != body || !string.Equals((string?)comment["user"]?["login"], config.IssueActionBot, StringComparison.OrdinalIgnoreCase) ||
            (long?)comment["id"] is not > 0) throw new GitHubFailure("GitHub did not confirm the operator comment.");
        return (long)comment["id"]!;
    }

    internal async Task CloseIssue(int number)
    {
        var result = (await Call($"issues/{number}", new { state = "closed", state_reason = "completed" }, method: HttpMethod.Patch))!;
        if ((int?)result["number"] != number || (string?)result["state"] != "closed" || (string?)result["state_reason"] != "completed")
            throw new GitHubFailure("GitHub did not confirm issue closure.");
    }

    internal async Task<string> CleanMergedBranch(DeliveredRound round, Func<bool> reserve)
    {
        var branch = round.Intent.Branch;
        var prefix = $"factory/{round.Intent.TaskId:N}";
        if (!Regex.IsMatch(branch, @"\A" + Regex.Escape(prefix) + @"(?:-[a-f0-9]{32})?\z") || branch == config.BaseBranch)
            throw new CommandConflict("Unexpected task branch.");
        if (!round.OwnsBranch) return $"Kept {branch} because the factory did not record creating it.";
        if (!(await PullState(round)).Merged) return $"Kept {branch} because its PR is unmerged.";
        return await DeleteApprovedBranch(new(branch, round.Draft.Number, round.Draft.HeadSha), reserve);
    }

    private async Task<string?> KeepBranch(BranchTarget target)
    {
        var branch = target.Branch;
        if (branch == config.BaseBranch) return $"Kept {branch} because it is the project base.";
        var current = await Ref(branch);
        if (current is null) return $"{branch} was already absent.";
        if (current != target.HeadSha) return $"Kept {branch} because its head changed.";
        var repo = (await Call(""))!;
        if ((long?)repo["id"] != config.RepositoryId || (string?)repo["default_branch"] is not { Length: > 0 })
            throw new GitHubFailure("Cannot confirm the repository node.");
        if ((string?)repo["default_branch"] == branch) return $"Kept {branch} because it is the default branch.";
        var info = (await Call("branches/" + Uri.EscapeDataString(branch), allowMissing: true));
        if (info is null) return $"{branch} is absent.";
        if ((bool?)info["protected"] != false) return $"Kept {branch} because GitHub protects it.";
        var owner = config.Repository.Split('/')[0];
        var open = await Call($"pulls?state=open&head={Uri.EscapeDataString(owner + ":" + branch)}&per_page=1");
        var bases = await Call($"pulls?state=open&base={Uri.EscapeDataString(branch)}&per_page=1");
        if (open!.AsArray().Count > 0 || bases!.AsArray().Count > 0) return $"Kept {branch} because an open PR uses it.";
        return null;
    }

    internal async Task<string> DeleteApprovedBranch(BranchTarget target, Func<bool> reserve)
    {
        var branch = target.Branch;
        if (await KeepBranch(target) is { } reason) return reason;
        var repository = (await Call(""))!;
        if ((long?)repository["id"] != config.RepositoryId || (string?)repository["node_id"] is not {Length: >0} node)
            throw new GitHubFailure("Cannot confirm the repository node.");
        if (!reserve()) throw new CommandConflict($"Removal of {branch} is unconfirmed. Check the original request before retrying.");
        const string query = "mutation($input:UpdateRefsInput!){updateRefs(input:$input){clientMutationId}}";
        var response = (await Call("", new
        {
            query,
            variables = new
            {
                input = new
                {
                    repositoryId = node,
                    refUpdates = new[] { new { name = "refs/heads/" + branch, beforeOid = target.HeadSha, afterOid = new string('0', 40) } }
                }
            }
        }, graphql: true))!;
        if (response["errors"] is not null || response["data"]?["updateRefs"] is null || await Ref(branch) is not null)
            throw new CommandConflict("GitHub did not confirm branch removal. Reconcile the recorded head.");
        return $"Removed merged branch {branch}.";
    }
}

public sealed partial class GitHubSync
{
    public bool IssueActionsEnabled => config.IssueActionBot is not null;

    public async Task<object> IssueActionPreview(int number, Guid taskId, string headSha, bool finish)
    {
        var approval = Approval(number);
        if (config.IssueActionBot is null) throw new CommandConflict("Issue writes are disabled for this repository.");
        var round = store.CurrentDelivery(config.RepositoryId, number, new(taskId, headSha, "", ""));
        var issue = await repo.Issue(number);
        if ((long?)issue["id"] != approval.Id || issue["pull_request"] is not null) throw new CommandConflict("Issue identity changed.");
        var pr = await repo.PullState(round);
        var action = store.ActionFor(taskId,headSha);
        var plan = action is not null ? store.CleanupPlanFor(action.State.Id) :
            finish && pr.Merged ? await repo.CleanupPlan(approval,store.PriorRounds(taskId).Append(round)) : null;
        return new
        {
            baseSha = await repo.BaseHead(),
            path = round.Origin.Path,
            paths = ci?.Rules.RepositoryLayout?.Writable ?? [round.Origin.Path],
            criteria = ci?.Rules.Revision?.Criteria,
            merged = pr.Merged,
            open = pr.Open,
            summary = store.PreviewFinish(round),
            targetBranch = config.BaseBranch,
            state = action?.State, branches = plan, cleanupHash = TaskStore.PlanHash(plan)
        };
    }

    public async Task<IssueActionState> RunIssueAction(int number, string kind, IssueActionCommand command)
    {
        if (config.IssueActionBot is null || kind is not ("follow-up" or "finish" or "withdraw") || command.TaskId == Guid.Empty || command.HeadSha is null || !Regex.IsMatch(command.HeadSha, @"\A[a-f0-9]{40}\z") || command.Scope is null ||
            command.Scope.Length > 2000 || kind == "follow-up" && string.IsNullOrWhiteSpace(command.Scope) ||
            kind == "finish" && command.Scope != "" || !Regex.IsMatch(command.BaseSha ?? "", @"\A[a-f0-9]{40}\z"))
            throw new CommandConflict("Select an enabled issue action with an approved base and scope.");
        var approval = Approval(number);
        var action = store.ActionFor(command.TaskId, command.HeadSha);
        if (command.ActionId is { } expected && action?.State.Id != expected)
            throw new CommandConflict("The operator action changed. Reload the task.");
        if (action is not null && (action.Delivery.Origin.Number != number || action.Delivery.Origin.RepoId != config.RepositoryId ||
            (kind == "withdraw" ? action.State.Kind != "follow-up" : action.State.Kind != kind) || action.State.BaseSha != command.BaseSha || action.State.Scope != command.Scope))
            throw new CommandConflict("Another operator action covers this delivery.");
        if (kind == "withdraw")
        {
            if (action is null || command.ActionId != action.State.Id || action.State.Stage is not ("ready" or "posting" or "commented"))
                throw new CommandConflict("Only a pending follow-up can be withdrawn.");
            store.CurrentDelivery(config.RepositoryId, number, command);
            if (action.State.Stage == "posting")
            {
                var confirmed = await repo.FindActionComment(number, action.Body);
                if (confirmed is null) throw new CommandConflict("The comment is unconfirmed. Inspect GitHub before withdrawing.");
                store.StepIssueAction(command.TaskId, action.State.Id, "posting", "commented", confirmed.Value.ToString());
            }
            if (!store.StepIssueAction(command.TaskId, action.State.Id, action.State.Stage == "ready" ? "ready" : "commented", "withdrawn"))
                throw new CommandConflict("The action changed. Reload the task.");
            return action.State with { Stage = "withdrawn" };
        }
        if (action?.State.Stage is "finished" or "started") return action.State;
        var round = store.CurrentDelivery(config.RepositoryId, number, command);
        var issue = await repo.Issue(number);
        if ((long?)issue["id"] != approval.Id || issue["pull_request"] is not null) throw new CommandConflict("Issue identity changed.");
        var state = (string?)issue["state"];
        if (state == "open" && repo.Observe(issue, approval, round.Origin).Reason != "Eligible")
            throw new CommandConflict("The issue is held or its approved scope changed.");
        if (state != "open" && !(kind == "finish" && state == "closed" && (string?)issue["state_reason"] == "completed"))
            throw new CommandConflict("The issue is not open for this action.");
        if (kind == "finish" && action?.State.Stage == "closed" && state != "closed")
            throw new CommandConflict("The issue was reopened. Branch cleanup is suspended.");
        var pr = await repo.PullState(round);
        if (kind == "finish" && !pr.Merged) throw new CommandConflict("Merge the delivered PR before finishing the issue.");
        if (kind == "follow-up" && (!pr.Merged && !pr.Open || await repo.BaseHead() != command.BaseSha))
            throw new CommandConflict("The base or unmerged delivery changed. Refresh the proposed scope and base.");
        if (kind == "follow-up" && !RoundApproval.Configured)
            throw new CommandConflict("Configure the shared follow-up approval directory first.");
        if (round.Origin.LocalJournal is not null) throw new CommandConflict("Issue actions require a GitHub issue provider.");
        var snapshot = kind == "follow-up" ? await repo.Snapshot(command.BaseSha!, project, ci, followUp: true) : default;
        if (round.Origin.PolicyHash != ci?.Hash || round.Origin.ApiUrl != config.ApiUrl || round.Origin.Repository != config.Repository ||
            round.Origin.Base != config.BaseBranch || round.Origin.ScopeHash != approval.ScopeHash || round.Origin.IssueId != approval.Id)
            throw new CommandConflict("The repository or installed policy differs from the recorded delivery.");
        var deliveries = store.PriorRounds(command.TaskId).Append(round).ToArray();
        var plan = action is null ? null : store.CleanupPlanFor(action.State.Id);
        if (action is null && command.CleanupHash is not null && kind == "finish")
            plan = await repo.CleanupPlan(approval,deliveries);
        if (TaskStore.PlanHash(plan) != command.CleanupHash)
            throw new CommandConflict("The branches changed. Reopen Finish issue to review the current branches.");
        action ??= store.ReserveIssueAction(round, command, kind, plan);
        var a = action.State;
        IssueActionState Current() => store.ActionFor(command.TaskId, command.HeadSha)?.State is { } current && current.Id == a.Id
            ? current : throw new CommandConflict("The pending action was replaced or withdrawn.");
        bool Step(string from, string to, string? id = null) => store.StepIssueAction(command.TaskId, a.Id, from, to, id);
        if (a.Stage is "ready" or "posting")
        {
            long? comment;
            if (a.Stage == "ready" && Step("ready", "posting"))
            {
                try { comment = await repo.PostActionComment(number, action.Body); }
                catch (GitHubFailure e) when (e.RejectedWrite) { Step("posting", "ready"); throw; }
            }
            else comment = await repo.FindActionComment(number, action.Body);
            if (comment is null) throw new CommandConflict("The comment outcome is unknown. Check the original comment before retrying.");
            Step("posting", "commented", comment.Value.ToString());
        }
        if (kind == "follow-up")
        {
            if ((await repo.ObserveIssue(number, approval, round.Origin)).Reason != "Eligible" || await repo.BaseHead() != command.BaseSha || (await repo.PullState(round)) != pr)
                throw new CommandConflict("The delivery changed before scheduling.");
            var origin = round.Origin with { BaseSha = command.BaseSha!, BaseTree = snapshot.Tree, SourceHash = snapshot.Source.Fingerprint() };
            store.StartFollowUp(action, origin, snapshot.Source, !pr.Merged && round.Origin.BaseSha == command.BaseSha);
        }
        else
        {
            if (state == "closed") Step("commented", "closed");
            if (Step("commented", "closing"))
            {
                try { await repo.CloseIssue(number); }
                catch (GitHubFailure e) when (e.RejectedWrite) { Step("closing", "commented"); throw; }
                Step("closing", "closed");
            }
            if (Current().Stage == "closing")
            {
                var observed = await repo.Issue(number);
                if ((long?)observed["id"] != approval.Id || (string?)observed["state"] != "closed" || (string?)observed["state_reason"] != "completed")
                    throw new CommandConflict("Closure is unconfirmed. Check GitHub before retrying.");
                Step("closing", "closed");
            }
            if (Current().Stage == "closed")
            {
                var branches = new List<string>();
                if (plan is not null)
                {
                    var current = await repo.CleanupPlan(approval,deliveries);
                    foreach (var entry in plan)
                    {
                        var target = entry.Target;
                        var result = store.CleanupResult(a.Id,target.Branch);
                        if (result is null)
                        {
                            var match = current.FirstOrDefault(c=>c.Target==target);
                            result = entry.Keep ?? (match is null ? $"Skipped {target.Branch}. The approved pull request or branch changed."
                                : match.Keep ?? await repo.DeleteApprovedBranch(target,()=>store.ReserveBranchCleanup(a.Id,target.Branch)));
                            store.RecordCleanup(a.Id,target.Branch,result);
                        }
                        branches.Add(result);
                    }
                }
                else foreach (var delivery in deliveries)
                {
                    var branch = delivery.Intent.Branch;
                    var outcome = store.CleanupResult(a.Id, branch);
                    if (outcome is null)
                    {
                        outcome = await repo.CleanMergedBranch(delivery, () => store.ReserveBranchCleanup(a.Id, branch));
                        store.RecordCleanup(a.Id, branch, outcome);
                    }
                    branches.Add(outcome);
                }
                store.FinishIssueAction(command.TaskId, a.Id, branches);
            }
        }
        return Current();
    }
}
