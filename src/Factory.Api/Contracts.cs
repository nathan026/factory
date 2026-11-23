using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Factory;

public static class Wire
{
    public static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        Converters = { new JsonStringEnumConverter() }
    };

    // Current worker messages follow the wire contract. Historical task snapshots use the tolerant reader above.
    public static readonly JsonSerializerOptions ResultJson = new(JsonSerializerDefaults.Web)
    {
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        RespectRequiredConstructorParameters = true,
        Converters = { new JsonStringEnumConverter(allowIntegerValues: false) }
    };

    public static string Digest(string content) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(content))).ToLowerInvariant();
}

public enum Phase { Planning, Implementation, Verification, Review, Delivery }
public enum Activity { Queued, Running, Waiting, Blocked, Delivered }
public record ProjectPolicy([property: JsonRequired] string Version, [property: JsonRequired] string[] RequiredChecks,
    [property: JsonRequired] int MaxRepairs)
{
    public bool Valid() => !string.IsNullOrWhiteSpace(Version) && MaxRepairs >= 0
        && RequiredChecks is { Length: > 0 } && RequiredChecks.All(id => !string.IsNullOrWhiteSpace(id))
        && RequiredChecks.Distinct(StringComparer.Ordinal).Count() == RequiredChecks.Length;
}
public record RunReference(int SchemaVersion, Guid TaskId, Guid RunId, Phase Phase,
    string InputHash, string SourceHash, string PolicyVersion);
public record Assignment(Guid RunId, Phase Phase, string Objective, string InputHash,
    int SchemaVersion, Guid TaskId, string SourceHash, string PolicyVersion, string[] RequiredChecks)
{
    public RunReference Reference() => new(SchemaVersion, TaskId, RunId, Phase, InputHash, SourceHash, PolicyVersion);
}
public enum ResultStatus { Completed, NeedsInput, Blocked, Failed, Cancelled }
public record PlanEvidence(string[] AffectedPaths, string[] RequiredChecks, string[] Dependencies, string Risk, string BriefHash);
public record WorkerResult<T>([property: JsonRequired] RunReference Assignment, [property: JsonRequired] ResultStatus Status,
    [property: JsonRequired] T Output,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] PlanEvidence? PlanEvidence = null);
public record AcceptedResult(RunReference Assignment, ResultStatus Status, string OutputHash);
public record Entry(int Number, string Actor, string Text, Guid? RunId = null);
public record Decision(string Id, string Subject, string Text, string Value);
public record SourcePacket(string Source, string Decisions, string Cases,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? AdditionalFiles = null)
{
    public string Content(SourcePart part) => part switch
    {
        SourcePart.Source => Source, SourcePart.Decisions => Decisions, SourcePart.Cases => Cases,
        _ => throw new ArgumentOutOfRangeException(nameof(part))
    };
    public string Content(SourcePart part, string path)
    {
        if (AdditionalFiles is not null)
        {
            using var files = JsonDocument.Parse(AdditionalFiles);
            if (files.RootElement.TryGetProperty(path, out var file)) return file.GetString()!;
        }
        return Content(part);
    }
    public string Fingerprint() => Wire.Digest(JsonSerializer.Serialize(this, Wire.Json));
}
public record Question(string Subject, string Text, bool Reserved);
public record Advice(string Source, string Recommendation, string Alternative, string Consequence,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] Consultation? Consultation = null);
public record Check(string Assertion, [property: JsonRequired] bool Passed, string Id);
public record Receipt(Guid TaskId, Guid RunId, string Candidate, string CandidateHash, string SourceHash,
    List<Check> Checks, int SchemaVersion, string PolicyVersion, string GateVersion)
{
    public bool Passed => Checks is { Count: > 0 } && Checks.All(c => c is { Passed: true });
}
public enum FindingSeverity { Advisory, Blocking }
public record ReviewFinding(string Criterion, [property: JsonRequired] FindingSeverity Severity, string Evidence, string Recommendation);
public record ReviewEvidence(Guid VerifierRunId, string ChecksHash, string BriefHash, ReviewFinding[] Findings);
public record Review(Guid RunId, string CandidateHash, bool Approved, string Finding,
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] ReviewEvidence? Evidence = null);
public record ReceiptRejection(int Index, string Reason);
public sealed class WorkItem
{
    public DateTimeOffset? OpenedAt { get; init; }
    public string? OpeningMessage { get; init; }
    public required Guid Id { get; init; }
    public required string Scenario { get; init; }
    public required string Goal { get; init; }
    public required SourcePacket Source { get; init; }
    public int ContractVersion { get; set; }
    public ProjectPolicy? Policy { get; set; }
    public Guid OwnerId { get; init; } = Guid.NewGuid();
    public Phase Phase { get; set; } = Phase.Planning;
    public Activity Activity { get; set; } = Activity.Queued;
    public Assignment? Run { get; set; }
    public List<Assignment> Assignments { get; init; } = [];
    public List<AcceptedResult> Results { get; init; } = [];
    public string? GateRule { get; set; }
    public Guid? LegacyWriter { get; set; }
    public Guid? LegacyPlanner { get; set; }
    public string? LegacyOperation { get; set; }
    public List<GateEvaluation> Gates { get; set; } = [];
    public OwnerMemory? Memory { get; set; }
    public PlanEvidence? PlanningEvidence { get; set; }
    public string? Plan { get; set; }
    public Question? Question { get; set; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? ExpertRuntime { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Guid? ExpertQuestionRunId { get; set; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? OwnerRuntime { get; init; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Guid? AskingRun { get; set; }
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public WorkerReply? OwnerReply { get; set; }
    public ExpertProfile? ExpertProfile { get; set; }
    public Advice? Advice { get; set; }
    public Decision? Answer { get; set; }
    public string? HumanChoice { get; set; }
    public string? Candidate { get; set; }
    public Guid? Writer { get; set; }
    public int Repairs { get; set; }
    public List<Receipt> Receipts { get; init; } = [];
    public ReceiptRejection? RejectedReceipt { get; set; }
    public Review? Review { get; set; }
    public List<Entry> History { get; init; } = [];

    public void Record(string actor, string message) =>
        History.Add(new(History.Count + 1, actor, message, Run?.RunId));
}
