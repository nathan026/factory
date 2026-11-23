using System.Collections.Immutable;
using System.Text;

namespace Factory;

// The advisor receives only the admitted question and allowlisted pinned documents.
public record ExpertProfile(string Version, string Domain, string[] Documents, int MaxSourceBytes)
{
    public bool Valid() => !string.IsNullOrWhiteSpace(Version) && !string.IsNullOrWhiteSpace(Domain)
        && Documents is { Length: > 0 } && Documents.All(p => !string.IsNullOrWhiteSpace(p))
        && Documents.Distinct(StringComparer.Ordinal).Count() == Documents.Length && MaxSourceBytes > 0;
}

public record ExpertSource(string Path, string Digest, string Content);
public record ExpertRequest(Guid TaskId, Guid RunId, Question Question, int BriefVersion, string BriefHash,
    string Revision, string ProfileVersion, ImmutableArray<ExpertSource> Sources)
{
    public bool Within(ExpertProfile profile) => profile.Valid() && ProfileVersion == profile.Version
        && !Sources.IsDefault && Sources.All(s => s is not null && s.Content is not null && Wire.Digest(s.Content) == s.Digest)
        && Sources.Select(s => s.Path).SequenceEqual(profile.Documents)
        && Sources.Sum(s => (long)Encoding.UTF8.GetByteCount(s.Content)) <= profile.MaxSourceBytes;

    public static ExpertRequest? Prepare(WorkItem task, ExpertProfile profile, BriefRevision brief)
    {
        if (!profile.Valid()) return null;
        var sources = new List<ExpertSource>();
        long bytes = 0;
        foreach (var path in profile.Documents)
        {
            var matches = task.Memory!.Documents.Where(d => d.Path == path).ToArray();
            if (matches is not [var document] || !document.Matches(task.Source, task.Run!.SourceHash)) return null;
            var content = task.Source.Content(document.Part, path);
            bytes += Encoding.UTF8.GetByteCount(content);
            if (bytes > profile.MaxSourceBytes) return null;
            sources.Add(new(path, document.Digest, content));
        }
        return new(task.Id, task.Run!.RunId, task.Question!, brief.Version, GateInput.Hash(brief),
            task.Run.SourceHash, profile.Version, sources.ToImmutableArray());
    }
}

public record ExpertOpinion(string RequestHash, string Conclusion, string Recommendation, string Alternative,
    string Consequence, string Assumption, string Uncertainty, string[] Citations, string? SuggestedOption)
{
    public bool Supports(ExpertRequest request) => RequestHash == GateInput.Hash(request)
        && new[] { Conclusion, Recommendation, Alternative, Consequence, Assumption, Uncertainty }
            .All(s => !string.IsNullOrWhiteSpace(s) && Encoding.UTF8.GetByteCount(s) <= 1024)
        && (SuggestedOption is null || Encoding.UTF8.GetByteCount(SuggestedOption) <= 128)
        && Citations is { Length: > 0 } && Citations.Distinct(StringComparer.Ordinal).Count() == Citations.Length
        && Citations.All(path => request.Sources.Any(s => s.Path == path));
}

public record ExpertCitation(string Path, string Digest);
public record Consultation(Guid TaskId, Guid RunId, Question Question, int BriefVersion, string BriefHash,
    string Revision, string ProfileVersion, string Domain, string RequiredAuthority, string RequestHash,
    string Conclusion, string Assumption, string Uncertainty, ExpertCitation[] Sources, string? SuggestedOption, DecisionOption[] Options);
