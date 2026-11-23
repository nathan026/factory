namespace Factory;

// Only these installed operations can be selected by a trusted project manifest.
public sealed record BlueprintPolicy(string[] EarlyChecks, int MaxCiRounds, int MaxCiSeconds,
    Dictionary<string, string[]> Tools)
{
    private static string Operation(WorkAction action) => action switch
    {
        WorkAction.Plan => "scope.plan",
        WorkAction.Ask => "question.raise",
        WorkAction.Implement => "candidate.write",
        WorkAction.Verify => "checks.run",
        WorkAction.Review => "candidate.review",
        _ => throw new InvalidDataException("Unknown blueprint node.")
    };

    private static string ReadTool(SourcePart part) => part switch
    {
        SourcePart.Source => "source.read",
        SourcePart.Decisions => "decisions.read",
        SourcePart.Cases => "cases.read",
        _ => throw new InvalidDataException("Unknown source reference.")
    };

    // Consultations have their own read-only expert profile and never receive blueprint grants.
    private static readonly WorkAction[] ProjectActions =
        [WorkAction.Plan, WorkAction.Ask, WorkAction.Implement, WorkAction.Verify, WorkAction.Review];

    public bool Valid(ProjectPolicy policy)
    {
        if (EarlyChecks is not { Length: > 0 } || EarlyChecks.Distinct().Count() != EarlyChecks.Length ||
            EarlyChecks.Except(policy.RequiredChecks).Any() || MaxCiRounds < 1 || MaxCiSeconds is < 1 or > 120 ||
            Tools?.Count != ProjectActions.Length) return false;
        var reads = Enum.GetValues<SourcePart>().Select(ReadTool).ToArray();
        return ProjectActions.All(action => Tools.TryGetValue(action.ToString(), out var tools)
            && tools is { Length: > 1 } && tools.Contains(Operation(action)) && tools.Intersect(reads).Any()
            && (action != WorkAction.Verify || reads.All(tools.Contains))
            && (action != WorkAction.Implement || tools.Contains("source.read"))
            && tools.Distinct().Count() == tools.Length && !tools.Except(reads.Append(Operation(action))).Any());
    }

    public SourceDocument[] Resolve(WorkAction action, IEnumerable<SourceDocument> canonical) =>
        canonical.Where(d => Tools[action.ToString()].Contains(ReadTool(d.Part))).ToArray();

    public PhaseContext Hydrate(WorkItem task, PhaseContext packet, string manifestHash)
    {
        var lastRevision = task.Gates.LastOrDefault(g => g.Outcome == GateOutcome.Revise);
        var feedback = packet.Action == WorkAction.Implement && lastRevision?.Input.Phase == Phase.Implementation
            && lastRevision.ArtifactHash == (task.Candidate is null ? null : Wire.Digest(task.Candidate))
            ? lastRevision.Criteria.Where(c => c.Id.StartsWith("local/", StringComparison.Ordinal) && !c.Passed).ToArray() : [];
        var documents = Resolve(packet.Action, packet.Documents);
        // Only local Markdown destinations already present in the admitted source catalog resolve.
        // External locations and issue prose never select tools or trigger network access.
        var opening = task.OpeningMessage ?? "";
        var references = documents.Where(d => opening.Contains("](" + d.Path + ")", StringComparison.Ordinal) ||
            opening.Contains("](./" + d.Path + ")", StringComparison.Ordinal)).Select(d => d.Path).ToArray();
        return packet with { Documents = documents,
            Blueprint = new(manifestHash, Tools[packet.Action.ToString()], references, feedback) };
    }

    public static void CheckGrant(IControlledProject project, WorkOrder order)
    {
        var grant = order.Context?.Blueprint;
        var ci = project.Ci;
        if (ci?.Rules.Blueprint is not { } policy)
        {
            if (grant is not null) throw new InvalidDataException("Unregistered project tools.");
            return;
        }
        if (grant is null || grant.ManifestHash != ci.Hash || grant.Tools is null ||
            !grant.Tools.SequenceEqual(policy.Tools[order.Action.ToString()]) || grant.OpeningReferences is null ||
            grant.LocalFeedback is null || grant.LocalFeedback.Any(c => c.Passed || order.Action != WorkAction.Implement ||
                !policy.EarlyChecks.Any(id => c.Id == "local/" + id)) ||
            grant.OpeningReferences.Except(order.Context!.Documents.Select(d => d.Path)).Any() ||
            !order.Context!.Documents.SequenceEqual(policy.Resolve(order.Action, project.Describe(order.Source).Documents)))
            throw new InvalidDataException("Worker tools or source references differ from the trusted blueprint.");
    }
}

public record BlueprintGrant(string ManifestHash, string[] Tools, string[] OpeningReferences, GateCriterion[] LocalFeedback);
