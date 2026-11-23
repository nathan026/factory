using System.Net;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Factory;

static class GitHubAppChecks
{
    private sealed class Clock : TimeProvider
    {
        public DateTimeOffset Now = DateTimeOffset.FromUnixTimeSeconds(1700000000);
        public override DateTimeOffset GetUtcNow() => Now;
    }

    private sealed class Api(RSA key, Clock clock) : HttpMessageHandler
    {
        public int Mints, Checks;
        public bool ExtraPermission, WrongRepository, Deny;
        private static byte[] Decode(string value) =>
            Convert.FromBase64String(value.Replace('-', '+').Replace('_', '/').PadRight((value.Length + 3) / 4 * 4, '='));
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            if (request.RequestUri!.Host != "api.github.com" || request.Headers.Authorization?.Scheme != "Bearer")
                throw new Exception("Unexpected App request destination or authentication.");
            if (request.Method == HttpMethod.Post && request.RequestUri.AbsolutePath == "/app/installations/41/access_tokens")
            {
                var jwt = request.Headers.Authorization.Parameter!.Split('.');
                var payload = JsonNode.Parse(Decode(jwt[1]))!;
                if (jwt.Length != 3 || (string?)JsonNode.Parse(Decode(jwt[0]))!["alg"] != "RS256" ||
                    !key.VerifyData(Encoding.ASCII.GetBytes(jwt[0]+"."+jwt[1]), Decode(jwt[2]), HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1) ||
                    (string?)payload["iss"] != "test-client" || (long?)payload["iat"] != clock.Now.AddMinutes(-1).ToUnixTimeSeconds() ||
                    (long?)payload["exp"] != clock.Now.AddMinutes(8).ToUnixTimeSeconds())
                    throw new Exception("Unexpected signed App identity or validity.");
                var body = await request.Content!.ReadFromJsonAsync<JsonObject>(ct);
                var expected = JsonNode.Parse("""{"repository_ids":[123],"permissions":{"contents":"write","pull_requests":"write","issues":"read"}}""");
                if (!JsonNode.DeepEquals(body, expected)) throw new Exception("Unexpected repository or requested permissions.");
                var permissions = body!["permissions"]!;
                permissions["metadata"] = "read";
                if (ExtraPermission) permissions["administration"] = "write";
                Mints++;
                if (Deny) return new(HttpStatusCode.Forbidden);
                return new(HttpStatusCode.Created) {Content = JsonContent.Create(new {token="test-"+Mints,
                    expires_at=clock.Now.AddHours(1), permissions})};
            }
            if (request.Method != HttpMethod.Get || request.RequestUri.PathAndQuery != "/installation/repositories?per_page=2" ||
                request.Headers.Authorization.Parameter != "test-"+Mints)
                throw new Exception("Unexpected repository scope verification.");
            Checks++;
            return new(HttpStatusCode.OK) {Content = JsonContent.Create(new {total_count=1, repositories=new[] {new {id=WrongRepository ? 124 : 123}}})};
        }
    }

    public static async Task Run()
    {
        var directory = Directory.CreateTempSubdirectory();
        try
        {
            using var key = RSA.Create(2048);
            var path = Path.Combine(directory.FullName,"test.pem");
            File.WriteAllText(path, key.ExportRSAPrivateKeyPem());
            var clock = new Clock();
            var api = new Api(key, clock);
            using var http = new HttpClient(api) {BaseAddress=new Uri("https://api.github.com/")};
            GitHubAppInstallation Start() => new(http, 123, "test-client", 41, path, clock);
            var app = Start();
            var requests = await Task.WhenAll(Enumerable.Range(0, 6).Select(_ => app.AccessToken()));
            if (api.Mints != 1 || api.Checks != 1 || requests.Distinct().Count() != 1) throw new Exception("Parallel issuance was duplicated.");
            clock.Now = clock.Now.AddMinutes(56);
            var renewed = await app.AccessToken();
            await app.Rejected(requests[0]);
            if (renewed == requests[0] || await app.AccessToken() != renewed) throw new Exception("Renewal or stale rejection failed.");
            await app.Rejected(renewed);
            if (await app.AccessToken() == renewed) throw new Exception("Rejected token reused.");
            app = Start();
            await app.AccessToken();
            if (api.Mints != 4 || api.Checks != 4) throw new Exception("Restart did not independently mint and verify.");
            foreach (var failure in new[]{"permissions","repository","revoked"})
            {
                api.ExtraPermission = failure=="permissions";
                api.WrongRepository = failure=="repository";
                api.Deny = failure=="revoked";
                try { await Start().AccessToken(); }
                catch (GitHubFailure) { continue; }
                throw new Exception("Invalid installation became active: "+failure);
            }
            Console.WriteLine("Passed: GitHub App signed issuance, scoped access, renewal, restart, concurrency and rejected grants");
        }
        finally { directory.Delete(recursive:true); }
    }
}
