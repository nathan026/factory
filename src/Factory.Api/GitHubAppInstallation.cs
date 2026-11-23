using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Factory;

internal sealed class GitHubAppInstallation(HttpClient api, long repositoryId, string clientId, long installationId,
    string keyFile, TimeProvider clock)
{
    private readonly SemaphoreSlim gate = new(1, 1);
    private string? token;
    private DateTimeOffset expires;
    private static readonly TimeSpan RenewalMargin = TimeSpan.FromMinutes(5);

    public static GitHubAppInstallation? FromEnvironment(HttpClient api, long repositoryId)
    {
        var client = Environment.GetEnvironmentVariable("FACTORY_GITHUB_APP_CLIENT_ID");
        var id = Environment.GetEnvironmentVariable("FACTORY_GITHUB_APP_INSTALLATION_ID");
        var key = Environment.GetEnvironmentVariable("FACTORY_GITHUB_APP_KEY_FILE");
        if (client is null && id is null && key is null) return null;
        if (key is null && Environment.GetEnvironmentVariable("CREDENTIALS_DIRECTORY") is { Length: > 0 } dir)
            key = Path.Combine(dir, "github-app-key.pem");
        if (string.IsNullOrWhiteSpace(client) || !long.TryParse(id, out var installation) || installation <= 0 ||
            key is null || !Path.IsPathFullyQualified(key) || api.BaseAddress?.AbsoluteUri != "https://api.github.com/")
            throw new InvalidDataException("Incomplete GitHub App installation configuration.");
        return new(api, repositoryId, client, installation, key, TimeProvider.System);
    }

    private string AppJwt(DateTimeOffset now)
    {
        static string Encode(byte[] data) => Convert.ToBase64String(data).TrimEnd('=').Replace('+','-').Replace('/','_');
        var header = Encode("""{"alg":"RS256","typ":"JWT"}"""u8.ToArray());
        var claims = Encode(JsonSerializer.SerializeToUtf8Bytes(new { iss = clientId,
            iat = now.AddMinutes(-1).ToUnixTimeSeconds(), exp = now.AddMinutes(8).ToUnixTimeSeconds() }));
        var signed = header + "." + claims;
        using var rsa = RSA.Create();
        rsa.ImportFromPem(File.ReadAllText(keyFile));
        return signed + "." + Encode(rsa.SignData(Encoding.ASCII.GetBytes(signed), HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1));
    }

    public async Task<string> AccessToken()
    {
        await gate.WaitAsync();
        try
        {
            var now = clock.GetUtcNow();
            if (token is not null && expires > now + RenewalMargin) return token;
            using var mint = new HttpRequestMessage(HttpMethod.Post, $"app/installations/{installationId}/access_tokens");
            mint.Headers.Authorization = new AuthenticationHeaderValue("Bearer", AppJwt(now));
            mint.Content = JsonContent.Create(new { repository_ids = new[] {repositoryId},
                permissions = new { contents = "write", pull_requests = "write", issues = "read" } });
            using var response = await api.SendAsync(mint);
            if (!response.IsSuccessStatusCode) throw new GitHubFailure($"GitHub App token issuance returned HTTP {(int)response.StatusCode}.");
            var grant = await response.Content.ReadFromJsonAsync<JsonObject>();
            if (grant?["token"]?.GetValue<string>() is not { Length: > 0 } issued ||
                grant["expires_at"]?.GetValue<DateTimeOffset>() is not { } expiry || expiry <= now + RenewalMargin ||
                grant["permissions"] is not JsonObject p || (string?)p["contents"] != "write" ||
                (string?)p["pull_requests"] != "write" || (string?)p["issues"] != "read" ||
                p.Any(k => k.Key is not ("contents" or "pull_requests" or "issues") && !(k.Key == "metadata" && (string?)k.Value == "read")))
                throw new GitHubFailure("GitHub App returned an invalid or unexpected token grant.");

            using var verify = new HttpRequestMessage(HttpMethod.Get, "installation/repositories?per_page=2");
            verify.Headers.Authorization = new AuthenticationHeaderValue("Bearer", issued);
            using var scope = await api.SendAsync(verify);
            if (!scope.IsSuccessStatusCode) throw new GitHubFailure($"GitHub App repository verification returned HTTP {(int)scope.StatusCode}.");
            var visible = await scope.Content.ReadFromJsonAsync<JsonObject>();
            if ((int?)visible?["total_count"] != 1 || visible["repositories"] is not JsonArray {Count:1} repos ||
                (long?)repos[0]?["id"] != repositoryId)
                throw new GitHubFailure("GitHub App token is outside the approved repository scope.");
            expires = expiry;
            return token = issued;
        }
        finally { gate.Release(); }
    }

    // Discard a rejected token for the next reconciliation. Never replay the original HTTP write here.
    public async Task Rejected(string used)
    {
        await gate.WaitAsync();
        try { if (token == used) token = null; }
        finally { gate.Release(); }
    }
}
