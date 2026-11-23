using System.Text.Json;
using System.Text.RegularExpressions;

namespace Factory;

// This directory is writable by the lifecycle controller and read-only to the trusted adapters.
// Workers compare the packet with the independent admission before using a new source revision.
public record RoundApproval(Guid Id, Guid TaskId, string Scope, string BaseSha, string SourceHash, string PolicyHash)
{
    public static bool Configured => Environment.GetEnvironmentVariable("FACTORY_FOLLOWUP_APPROVALS") is { } root &&
        Path.IsPathFullyQualified(root) && Directory.Exists(root);

    private string FileName()
    {
        var root = Environment.GetEnvironmentVariable("FACTORY_FOLLOWUP_APPROVALS");
        if (root is null || !Path.IsPathFullyQualified(root) || !Directory.Exists(root))
            throw new CommandConflict("Configure the shared follow-up approval directory before starting work.");
        return Path.Combine(root, $"{TaskId:N}-{Id:N}.json");
    }

    public void Persist()
    {
        var file = FileName();
        var tmp = file + "." + Guid.NewGuid().ToString("N");
        try
        {
            File.WriteAllText(tmp, JsonSerializer.Serialize(this, Wire.Json));
            if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(tmp, UnixFileMode.UserRead | UnixFileMode.GroupRead);
            try { File.Move(tmp, file, false); } catch (IOException) when (File.Exists(file)) { }
            if (!MatchesFile(file)) throw new CommandConflict("Conflicting approval for this round.");
        }
        finally { File.Delete(tmp); }
    }

    private bool MatchesFile(string path) => File.Exists(path) && JsonSerializer.Deserialize<RoundApproval>(File.ReadAllText(path), Wire.Json) == this;

    public void Validate(WorkOrder order, IControlledProject project)
    {
        if (Id == Guid.Empty || TaskId != order.Assignment.TaskId || Scope != order.Context?.Brief.Goal ||
            !Regex.IsMatch(BaseSha, @"\A[0-9a-f]{40}\z") || SourceHash != order.Source.Fingerprint() ||
            PolicyHash != (project.Ci?.Hash ?? "") || !MatchesFile(FileName()))
            throw new InvalidDataException("Follow-up differs from the operator's recorded admission.");
    }
}
