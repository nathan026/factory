using System.Globalization;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.AspNetCore.WebUtilities;

namespace Factory;

public record SlackProfile(string Project, string TeamId, string AppId, string ChannelId, string[] DecisionUsers, Uri ApiOrigin)
{
    public string Destination => GateInput.Hash(new { Project, TeamId, AppId, ChannelId, ApiOrigin });

    public static SlackProfile Load(string path, string project)
    {
        var p = JsonSerializer.Deserialize<SlackProfile>(File.ReadAllText(path), Wire.ResultJson);
        static bool Id(string? id, string prefix) => id is not null && Regex.IsMatch(id, "^" + prefix + "[A-Z0-9]{1,32}\\z");
        if (p is null || p.Project != project || !Id(p.TeamId,"T") || !Id(p.AppId,"A") || !Id(p.ChannelId,"[CG]") ||
            p.DecisionUsers is null || p.DecisionUsers.Any(u => !Id(u,"[UW]")) ||
            p.ApiOrigin is not { IsAbsoluteUri:true, Scheme:"http", UserInfo:"", AbsolutePath:"/", Query:"", Fragment:"" } uri ||
            !IPAddress.TryParse(uri.DnsSafeHost,out var ip) || !IPAddress.IsLoopback(ip))
            throw new InvalidDataException("Invalid project, Slack scope or loopback fixture origin.");
        return p;
    }
}

public sealed class SlackTransport(TaskStore store, SlackProfile profile, int deskPort, string? signingSecret) : IDisposable
{
    private readonly HttpClient http = new(new HttpClientHandler { AllowAutoRedirect = false, UseProxy = false, UseCookies = false,
        UseDefaultCredentials = false }) { Timeout = TimeSpan.FromSeconds(5), MaxResponseContentBufferSize = 65536 };
    public void Dispose() => http.Dispose();

    public async Task<int> Poll(CancellationToken stop)
    {
        var count = 0;
        foreach (var task in store.NotificationCandidates())
        {
            stop.ThrowIfCancellationRequested();
            if (store.ReserveNotice(task, profile) is not { } notice) continue;
            count++;
            await Post(notice, stop);
        }
        return count;
    }

    private async Task Post(SlackNotice notice, CancellationToken stop)
    {
        var link = $"http://127.0.0.1:{deskPort}/?task={notice.TaskId:D}";
        var blocks = new List<object> {
            new { type = "section", text = new { type = "plain_text", text = notice.Text } },
            new { type = "section", text = new { type = "mrkdwn", text = $"<{link}|Open task desk>" } }
        };
        if (notice.Choices.Length > 0)
            blocks.Add(new { type = "actions", block_id = notice.Id.ToString(), elements = notice.Choices.Select((c,i) =>
                new { type = "button", action_id = $"choice_{i}", value = c.Value, text = new { type = "plain_text", text = c.Label } }) });
        try
        {
            using var response = await http.PostAsJsonAsync(new Uri(profile.ApiOrigin,"api/chat.postMessage"),
                new { channel = profile.ChannelId, text = $"Factory task {notice.TaskId:D} needs attention. {link}",
                    blocks, unfurl_links = false, unfurl_media = false }, Wire.Json, stop);
            if (response.StatusCode == HttpStatusCode.TooManyRequests)
            {
                var delay = response.Headers.RetryAfter;
                var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                var reset = delay?.Date?.ToUnixTimeMilliseconds() ?? now + (long)(delay?.Delta ?? TimeSpan.FromMinutes(1)).TotalMilliseconds;
                store.FinishNotice(notice.Id,"RateLimited",retry:Math.Max(now+1000,reset));
                return;
            }
            if (!response.IsSuccessStatusCode) return;
            using var reply = JsonDocument.Parse(await response.Content.ReadAsStringAsync(stop));
            var root = reply.RootElement;
            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("ok",out var ok)) return;
            if (ok.ValueKind == JsonValueKind.True && Field(root,"channel") == profile.ChannelId && Field(root,"ts") is { } ts &&
                Regex.IsMatch(ts, @"^[0-9]{1,16}\.[0-9]{1,8}\z"))
                store.FinishNotice(notice.Id,"Sent",ts);
            // Ambiguous errors retain their reservation. Only an explicit refusal is classified as rejected.
            else if (ok.ValueKind == JsonValueKind.False && Field(root,"error") is "channel_not_found" or "not_in_channel" or "invalid_auth")
                store.FinishNotice(notice.Id,"Rejected");
        }
        catch (Exception e) when (e is HttpRequestException or IOException or JsonException or TaskCanceledException)
        {
            // The write may already have reached the destination. Never repeat an uncertain submission.
        }
    }

    private static string? Field(JsonElement o, string name) => o.ValueKind == JsonValueKind.Object &&
        o.TryGetProperty(name,out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
    private static string? Child(JsonElement o, string parent, string name) =>
        o.ValueKind == JsonValueKind.Object && o.TryGetProperty(parent,out var child) ? Field(child,name) : null;

    private static bool WitnessesMessage(JsonElement p, string app, string ts, Guid notice, string action, string choice)
    {
        if (!p.TryGetProperty("message",out var message) || Field(message,"app_id") != app || Field(message,"ts") != ts ||
            !message.TryGetProperty("blocks",out var blocks) || blocks.ValueKind != JsonValueKind.Array) return false;
        return blocks.EnumerateArray().Any(b => Field(b,"type") == "actions" && Field(b,"block_id") == notice.ToString() &&
            b.TryGetProperty("elements",out var elements) && elements.ValueKind == JsonValueKind.Array &&
            elements.EnumerateArray().Any(e => Field(e,"type") == "button" && Field(e,"action_id") == action && Field(e,"value") == choice));
    }

    public async Task<IResult> Callback(HttpRequest request)
    {
        if (profile.DecisionUsers.Length == 0 || signingSecret is null) return Results.StatusCode(403);
        const int maxBody = 65536;
        if (!request.HasFormContentType || !string.Equals(request.ContentType?.Split(';')[0].Trim(),
            "application/x-www-form-urlencoded", StringComparison.OrdinalIgnoreCase)) return Results.StatusCode(415);
        var timestamp = request.Headers["X-Slack-Request-Timestamp"].ToString();
        var signature = request.Headers["X-Slack-Signature"].ToString();
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        if (signature.Length != 67 || !long.TryParse(timestamp,NumberStyles.None,CultureInfo.InvariantCulture,out var unix) ||
            unix < now-300 || unix > now+300) return Results.Unauthorized();
        if (request.ContentLength > maxBody) return Results.StatusCode(413);
        using var raw = new MemoryStream();
        var buffer = new byte[4096];
        int read;
        while ((read = await request.Body.ReadAsync(buffer, request.HttpContext.RequestAborted)) > 0)
        {
            if (raw.Length+read > maxBody) return Results.StatusCode(413);
            raw.Write(buffer,0,read);
        }
        using var mac = IncrementalHash.CreateHMAC(HashAlgorithmName.SHA256,Encoding.UTF8.GetBytes(signingSecret));
        mac.AppendData(Encoding.ASCII.GetBytes($"v0:{timestamp}:"));
        var bytes = raw.ToArray();
        mac.AppendData(bytes);
        var expected = "v0=" + Convert.ToHexStringLower(mac.GetHashAndReset());
        if (!CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(expected),Encoding.ASCII.GetBytes(signature)))
            return Results.Unauthorized();
        var form = QueryHelpers.ParseQuery(Encoding.UTF8.GetString(bytes));
        if (!form.TryGetValue("payload",out var values) || values.Count != 1) return Results.BadRequest();
        try
        {
            using var doc = JsonDocument.Parse(values[0]!);
            var p = doc.RootElement;
            if (Field(p,"type") != "block_actions" || Field(p,"api_app_id") != profile.AppId || Child(p,"team","id") != profile.TeamId ||
                Child(p,"channel","id") != profile.ChannelId || Child(p,"container","channel_id") != profile.ChannelId ||
                Child(p,"container","type") != "message" || Child(p,"user","id") is not { } user || !profile.DecisionUsers.Contains(user))
                return Results.StatusCode(403);
            if (!p.TryGetProperty("actions",out var a) || a.ValueKind != JsonValueKind.Array || a.GetArrayLength() != 1 || Field(a[0],"type") != "button" ||
                !Guid.TryParseExact(Field(a[0],"block_id"),"D",out var id) || Field(a[0],"action_id") is not { } action ||
                Field(a[0],"value") is not { } choice || Child(p,"container","message_ts") is not { } ts) return Results.BadRequest();
            if (!Regex.IsMatch(ts, @"^[0-9]{1,16}\.[0-9]{1,8}\z")) return Results.BadRequest();
            store.ApplySlackDecision(profile,new(id,action,choice,user,ts,WitnessesMessage(p,profile.AppId,ts,id,action,choice)));
            return Results.Ok();
        }
        catch (JsonException) { return Results.BadRequest(); }
    }
}

public sealed class SlackPoller(SlackTransport transport, ILogger<SlackPoller> logger) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stop)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(30));
        do
        {
            try { await transport.Poll(stop); }
            catch (System.Data.Common.DbException e) when (e.IsTransient) { logger.LogWarning("Notification database unavailable. Retrying on next poll."); }
        } while (await timer.WaitForNextTickAsync(stop));
    }
}
