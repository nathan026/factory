namespace Factory;

public sealed class GitHubSync(GitHubProject config, GitHubRepository repo, TaskStore store, IControlledProject project, CiProfile? ci = null)
{
    public async Task<GitHubStatus> Poll(int number, bool automatic = false)
    {
        var approval = config.Issues.SingleOrDefault(i => i.Number == number)
            ?? throw new CommandConflict("This issue is outside the configured admission list.");
        var status = store.GitHubStatusFor(config.RepositoryId, number);
        var origin = status.Origin;
        if (origin is not null && (origin.PolicyHash != ci?.Hash || origin.ApiUrl != config.ApiUrl || origin.Repository != config.Repository || origin.Base != config.BaseBranch))
            throw new CommandConflict("Project destination differs from the original admission.");
        var observation = await repo.ObserveIssue(number, approval, origin);
        var reason = observation.Reason;
        SourcePacket? source = null;
        if (reason == "Eligible" && status.TaskId is null)
        {
            try
            {
                var revision = await repo.BaseHead();
                var snapshot = await repo.Snapshot(revision, project, ci);
                source = snapshot.Source;
                origin = new(config.ApiUrl, config.RepositoryId, config.Repository, number, observation.Id, config.BaseBranch, revision,
                    snapshot.Tree, source.Fingerprint(), snapshot.Path, approval.Scenario, approval.ScopeHash, ci?.Hash, config.LocalAtomicDraft ? await repo.LocalJournal() : null);
            }
            catch (CommandConflict ex) { reason = ex.Message; }
        }
        observation = observation with { Reason = reason };
        status = store.ObserveIssue(config.RepositoryId, number, status.Version, observation, origin, source);
        if (status.TaskId is not { } taskId || status.Draft is not null) return status;

        if (automatic && store.PublicationHeld(taskId)) return status;
        try { return await Publish(status, approval, taskId); }
        catch (UnknownPublication ex) when (status.Origin is { LocalJournal: null } &&
            (new Uri(config.ApiUrl).IsLoopback || config.HostedIssue is not null))
        {
            store.HoldPublication(taskId, ex.Message);
            store.PublicationProblem(taskId, ex.Message);
            throw;
        }
        catch (Exception e) when (e is CommandConflict or GitHubFailure or HttpRequestException or TaskCanceledException)
        {
            store.PublicationProblem(taskId, e is CommandConflict or GitHubFailure ? e.Message : "Repository request interrupted. Reconciliation pending.");
            throw;
        }
    }

    private async Task<GitHubStatus> RefreshEligibility(GitHubStatus status, ApprovedIssue approval)
    {
        var origin = status.Origin!;
        var issue = await repo.ObserveIssue(origin.Number, approval, origin);
        if (await repo.BaseHead() != origin.BaseSha) throw new CommandConflict("Base branch moved. Requalify before publication.");
        return store.ObserveIssue(config.RepositoryId, origin.Number, status.Version, issue, origin, null);
    }

    private async Task<GitHubStatus> Publish(GitHubStatus status, ApprovedIssue approval, Guid taskId)
    {
        // Reconcile even if a hold or issue edit arrived after the remote write. This path is read-only on GitHub.
        if (store.UnconfirmedDraft(taskId) is { } pending)
        {
            var existing = status.Origin!.LocalJournal is not null
                ? await repo.LocalReceipt(status.Origin, pending.Intent, pending.Head) : await repo.FindDraft(pending.Intent, pending.Head);
            if (existing is not null)
            {
                store.RecordDraft(taskId, existing);
                return store.GitHubStatusFor(config.RepositoryId, approval.Number);
            }
            if (status.Origin.LocalJournal is null)
                throw new UnknownPublication("Draft creation outcome is unknown. Inspect the task branch and its original Draft receipt.");
        }
        if (status.Observation?.Reason == "Eligible" && store.PrepareDraft(status) is { } intent)
        {
            var origin = status.Origin!;
            if (await repo.BaseHead() != origin.BaseSha) throw new CommandConflict("Base branch moved. Requalify the candidate before publication.");
            var head = store.DraftHead(taskId) ?? store.SealHead(taskId, await repo.BuildCommit(origin, intent));
            var found = origin.LocalJournal is not null ? null : await repo.FindDraft(intent, head);
            if (found is null)
            {
                status = await RefreshEligibility(status, approval);
                if (status.Observation?.Reason != "Eligible") return status;
                if (origin.LocalJournal is null)
                {
                    await repo.EnsureBranch(intent, head, () => store.ReservePublicationWrite(origin, taskId, status.Version, false));
                    if (!store.OwnsBranch(taskId)) throw new CommandConflict("The existing branch was not created by this publication.");
                    status = await RefreshEligibility(status, approval);
                    if (status.Observation?.Reason != "Eligible") return status;
                }
                if (!store.ReservePublicationWrite(origin, taskId, status.Version, true))
                    throw new CommandConflict("A publisher already reserved creation. Poll to reconcile.");
                if (origin.LocalJournal is not null)
                    found = await repo.LocalReceipt(origin, intent, head);
                found ??= await repo.CreateDraft(origin, intent, head);
            }
            store.RecordDraft(taskId, found);
        }
        return store.GitHubStatusFor(config.RepositoryId, approval.Number);
    }
}

// Poll approved issues from canonical state. A notification can request the same poll without supplying issue data.
public sealed class GitHubPoller(GitHubSync sync, GitHubProject config, ILogger<GitHubPoller> log) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stop)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(30));
        do
        {
            foreach (var issue in config.Issues)
            {
                if (stop.IsCancellationRequested) return;
                try { await sync.Poll(issue.Number, automatic: true); }
                catch (Exception e) when (e is HttpRequestException or TaskCanceledException or GitHubFailure or CommandConflict)
                { log.LogWarning("GitHub poll for issue {Issue} deferred: {Reason}", issue.Number, e.Message); }
                catch (Exception e)
                { log.LogError(e, "GitHub poll for issue {Issue} failed. Continuing with the other approved issues.", issue.Number); }
            }
        } while (await timer.WaitForNextTickAsync(stop));
    }
}
