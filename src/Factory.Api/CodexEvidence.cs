using Microsoft.Data.Sqlite;
using System.Text.Json;
using Factory.Fixtures;

namespace Factory;

internal static class CodexEvidence
{
    private static SqliteConnection Journal() => new(new SqliteConnectionStringBuilder {
        DataSource = Environment.GetEnvironmentVariable("FACTORY_LEASE_DB"), Mode = SqliteOpenMode.ReadOnly
    }.ToString());

    public static bool ReleasedInterruptedWriter(Guid task, Guid run, string policy, string source)
    {
        try
        {
            using var db = Journal();
            db.Open();
            using var cmd = db.CreateCommand();
            cmd.CommandText = """
                SELECT 1 FROM codex_attempts a JOIN leases l ON a.generation=l.generation AND a.task=l.task AND a.run=l.run
                WHERE a.task=$task AND a.run=$run AND a.action='Implement' AND a.status IN ('interrupted','Coding timeout')
                AND a.ended IS NOT NULL AND a.result IS NULL AND a.policy=$policy AND a.source=$source
                AND l.role='writer' AND l.operation=a.action AND l.policy=a.policy AND l.state='released' AND l.outcome_hash IS NULL
                AND NOT EXISTS (SELECT 1 FROM leases WHERE task=$task AND state<>'released')
                """;
            cmd.Parameters.AddWithValue("$task",task.ToString());
            cmd.Parameters.AddWithValue("$run",run.ToString());
            cmd.Parameters.AddWithValue("$policy",policy);
            cmd.Parameters.AddWithValue("$source",source);
            return cmd.ExecuteScalar() is not null;
        }
        catch (SqliteException) { return false; }
    }

    // Only allowlisted receipt fields leave the process. Raw prompts, output, and account data stay private.
    public static object? Attempts(Guid task)
    {
        try
        {
            using var db = Journal();
            db.Open();
            using var cmd = db.CreateCommand();
            cmd.CommandText = "SELECT run,action,status,started,ended,thread,generation,usage,events FROM codex_attempts WHERE task=$task ORDER BY started";
            cmd.Parameters.AddWithValue("$task",task.ToString());
            using var rows = cmd.ExecuteReader();
            var attempts = new List<object>();
            while (rows.Read())
            {
                using var usage = JsonDocument.Parse(rows.IsDBNull(7) ? "{}" : rows.GetString(7));
                using var events = JsonDocument.Parse(rows.IsDBNull(8) ? "[]" : rows.GetString(8));
                long? Tokens(string key) => usage.RootElement.TryGetProperty(key,out var value) && value.TryGetInt64(out var count) && count>=0 ? count : null;
                attempts.Add(new { runId=Guid.Parse(rows.GetString(0)), action=rows.GetString(1), status=rows.GetString(2),
                    startedUnix=rows.GetDouble(3), endedUnix=rows.IsDBNull(4) ? (double?)null : rows.GetDouble(4),
                    threadId=rows.IsDBNull(5) ? (Guid?)null : Guid.Parse(rows.GetString(5)),
                    generation=rows.IsDBNull(6) ? (Guid?)null : Guid.Parse(rows.GetString(6)),
                    inputTokens=Tokens("input_tokens"), cachedInputTokens=Tokens("cached_input_tokens"), outputTokens=Tokens("output_tokens"),
                    repositoryReads=events.RootElement.EnumerateArray().Count(e=>e.GetProperty("tool").GetString()=="read_file"),
                    repositoryEdits=events.RootElement.EnumerateArray().Count(e=>e.GetProperty("tool").GetString()=="replace_text") });
            }
            return attempts;
        }
        catch (Exception e) when (e is SqliteException or JsonException or FormatException or InvalidOperationException)
        {
            return null;
        }
    }

    // The native supervisor owns this journal. The model and its containers have no journal or controller access.
    public static void Require(WorkOrder order, JsonElement output, RepositoryLayout? layout, string? executionSession = null)
    {
        var reject = new CommandConflict("Live result lacks a matching released lease and native Codex turn.");
        try
        {
            using var connection = Journal();
            connection.Open();
            using var query = connection.CreateCommand();
            bool writer = order.Action == WorkAction.Implement;
            query.CommandText = """
                SELECT a.thread,a.result,a.events,l.outcome_hash,l.candidate_hash
                FROM codex_attempts a JOIN leases l ON a.generation=l.generation AND a.task=l.task AND a.run=l.run
                WHERE a.task=$task AND a.run=$run AND a.action=$action AND l.operation=a.action AND l.role=$role
                  AND a.status='sealed' AND a.source=$source AND a.policy=$policy AND l.policy=a.policy AND l.state='released'
                """;
            var (role, policy) = order.Action switch
            {
                WorkAction.Discuss or WorkAction.AnswerWorker when executionSession is not null => ("owner", GateInput.Hash(order.Discussion)),
                WorkAction.Consult when executionSession is not null => ("expert", GateInput.Hash(order.Consultation)),
                WorkAction.Implement => ("writer", order.Context!.Blueprint!.ManifestHash),
                WorkAction.Review => ("reviewer", order.Context!.Blueprint!.ManifestHash),
                _ => throw reject
            };
            query.Parameters.AddWithValue("$task", order.Assignment.TaskId.ToString());
            query.Parameters.AddWithValue("$run", executionSession ?? order.Assignment.RunId.ToString());
            query.Parameters.AddWithValue("$action", order.Action.ToString());
            query.Parameters.AddWithValue("$role", role);
            query.Parameters.AddWithValue("$source", order.Assignment.SourceHash);
            query.Parameters.AddWithValue("$policy", policy);
            using var row = query.ExecuteReader();
            if (!row.Read() || row.IsDBNull(0) || !Guid.TryParse(row.GetString(0),out var thread) || thread == Guid.Empty)
                throw reject;
            var sealedResult = row.GetString(1);
            using var result = JsonDocument.Parse(sealedResult);
            if (Wire.Digest(sealedResult) != row.GetString(3) || !JsonElement.DeepEquals(result.RootElement, output))
                throw reject;
            using var events = JsonDocument.Parse(row.GetString(2));
            if (order.Discussion is { } question)
            {
                var reads = events.RootElement;
                if (output.GetProperty("requestHash").GetString() != GateInput.Hash(question) ||
                    !output.GetProperty("fact").TryGetInt32(out var fact) || fact < -1 ||
                    fact >= JsonSerializer.Deserialize<string[]>(question.Evidence.Content)!.Length ||
                    reads.GetArrayLength() == 0 || reads.EnumerateArray().Any(e =>
                        e.GetProperty("tool").GetString() != "read_file" || e.GetProperty("path").GetString() != question.Evidence.Path
                        || e.GetProperty("sha256").GetString() != question.Evidence.Digest
                        || e.GetProperty("view").GetString() is not ("base" or "current"))) throw reject;
                return;
            }
            if (order.Action == WorkAction.Consult)
            {
                var request = order.Consultation!;
                var opinion = output.Deserialize<ExpertOpinion>(Wire.ResultJson);
                if (opinion?.Supports(request) != true || !opinion.Citations.All(path =>
                    events.RootElement.EnumerateArray().Any(e => e.GetProperty("tool").GetString() == "read_file"
                        && e.GetProperty("path").GetString() == path && request.Sources.Any(s =>
                            s.Path == path && s.Digest == e.GetProperty("sha256").GetString())))) throw reject;
                return;
            }
            var candidate = writer ? output.GetProperty("output").GetString() : order.Candidate;
            if (candidate is null || writer && (row.IsDBNull(4) || row.GetString(4) != Wire.Digest(candidate))
                || !writer && order.Assignment.InputHash != Wire.Digest(candidate)) throw reject;
            var files = layout?.CandidateFiles(candidate) ?? new Dictionary<string,string> { [FactorySummaryProject.Target] = candidate };
            var previous = writer && order.Candidate is not null ? layout?.CandidateFiles(order.Candidate) : null;
            bool edited = false;
            foreach (var (path, content) in files)
            {
                var target = events.RootElement.EnumerateArray().Where(e => e.GetProperty("path").GetString() == path).ToArray();
                bool Seen(string tool, string? view, string? hash) => target.Any(e => e.GetProperty("tool").GetString() == tool
                    && (view is null || e.GetProperty("view").GetString() == view)
                    && (hash is null || e.GetProperty("sha256").GetString() == hash));
                var digest = Wire.Digest(content);
                var baseline = order.Source.Content(SourcePart.Source, path);
                var initial = previous?.GetValueOrDefault(path) ?? (layout is null ? order.Candidate : null) ?? baseline;
                bool changedFile = initial != content;
                edited |= changedFile;
                if (writer && (!Seen("read_file", "current", null) ||
                               (changedFile || layout?.AdditionalWrites is not { Length: > 0 }) && !Seen("replace_text", null, digest))
                    || !writer && (!Seen("read_file", "base", Wire.Digest(baseline))
                                   || !Seen("read_file", "current", digest)))
                    throw reject;
            }
            if (writer && !edited && layout?.AdditionalWrites is { Length: > 0 }) throw reject;
        }
        catch (Exception e) when (e is SqliteException or JsonException or InvalidOperationException or InvalidDataException or KeyNotFoundException)
        {
            throw reject;
        }
    }
}
