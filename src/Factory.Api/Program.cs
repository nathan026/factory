using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Factory;
using Factory.Fixtures;

static IControlledProject LoadProject(string version) => version is FactorySummaryProject.Version or FactorySummaryProject.LiveVersion
    ? new FactorySummaryProject(version) : new SampleRuntime(version, Environment.GetEnvironmentVariable("FACTORY_OWNER_RUNTIME") == "codex-subscription");

static string ControllerDigest()
{
    using var binary = File.OpenRead(typeof(FactoryDatabase).Assembly.Location);
    return Convert.ToHexString(SHA256.HashData(binary)).ToLowerInvariant();
}

if (args is ["--release-info"])
{
    var assembly = typeof(FactoryDatabase).Assembly;
    var version = System.Reflection.CustomAttributeExtensions.GetCustomAttribute<System.Reflection.AssemblyInformationalVersionAttribute>(assembly);
    Console.WriteLine(JsonSerializer.Serialize(new { version = version?.InformationalVersion, sha256 = ControllerDigest() }));
    return;
}
if (args is ["--fixture-worker"] or ["--codex-bindings"])
{
    Console.InputEncoding = new UTF8Encoding(false, true);
    Console.OutputEncoding = new UTF8Encoding(false, true);
    var order = JsonSerializer.Deserialize<WorkOrder>(Console.In.ReadToEnd(), Wire.Json)!;
    var installed = LoadProject(order.Assignment.PolicyVersion);
    if (args[0] == "--codex-bindings")
    {
        if (order.Action is WorkAction.Discuss or WorkAction.AnswerWorker)
        {
            order.Validate(installed);
            Console.WriteLine(JsonSerializer.Serialize(new { requestHash = GateInput.Hash(order.Discussion) }));
            return;
        }
        if (order.Action == WorkAction.Consult && order.ExpertRuntime == "codex-subscription")
        {
            order.Validate(installed);
            var request = order.Consultation!;
            Console.WriteLine(JsonSerializer.Serialize(new { requestHash = GateInput.Hash(request),
                suggestions = installed.ExpertSuggestions(request.Question) }, Wire.Json));
            return;
        }
        if (installed.Ci?.RequiresCodex != true || order.Context is null ||
            order.Action is not (WorkAction.Implement or WorkAction.Review))
            throw new InvalidDataException("Expected a live repository work order.");
        order.Validate(installed);
        var finding = order.Context.Findings.LastOrDefault();
        Console.WriteLine(JsonSerializer.Serialize(new { assignment = order.Assignment.Reference(),
            briefHash = GateInput.Hash(order.Context.Brief), checksHash = finding is null ? null : GateInput.Hash(finding),
            gitRevision = installed.Ci!.Rules.Settings.GetProperty("base").GetString() }, Wire.Json));
    }
    else Console.WriteLine(order.Execute(installed).GetRawText());
    return;
}
var builder = WebApplication.CreateBuilder(args);
var port = int.Parse(Environment.GetEnvironmentVariable("FACTORY_PORT") ?? "5187");
builder.WebHost.ConfigureKestrel(server => server.Listen(IPAddress.Loopback, port));
builder.Services.ConfigureHttpJsonOptions(options =>
    options.SerializerOptions.Converters.Add(new JsonStringEnumConverter()));
var manual = Environment.GetEnvironmentVariable("FACTORY_MANUAL_DISPATCH") == "1";
var token = Environment.GetEnvironmentVariable("FACTORY_LOCAL_TOKEN");
if (string.IsNullOrWhiteSpace(token))
{
    token = Convert.ToHexString(RandomNumberGenerator.GetBytes(24));
    Console.WriteLine($"Local dashboard sign-in token: {token}");
}
var expected = SHA256.HashData(Encoding.UTF8.GetBytes("Bearer " + token));
var project = LoadProject(Environment.GetEnvironmentVariable("FACTORY_PROJECT") ?? "shipping/v1");
var directory = Environment.GetEnvironmentVariable("FACTORY_STATE") ?? Path.Combine(builder.Environment.ContentRootPath, ".factory");
var hatchet = Environment.GetEnvironmentVariable("FACTORY_DISPATCH") switch
{
    "hatchet" => true,
    null or "local" => false,
    _ => throw new InvalidOperationException("Unknown dispatch mode.")
};
var pinnedRepository = project is FactorySummaryProject || project.Ci?.Rules.RepositoryLayout is not null;
if (pinnedRepository && (!hatchet || manual || string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("FACTORY_GITHUB_PROJECT"))))
    throw new InvalidOperationException("Factory-source work requires normal GitHub intake and Hatchet dispatch.");
var dispatchToken = Environment.GetEnvironmentVariable("FACTORY_DISPATCH_TOKEN");
var postgres = Environment.GetEnvironmentVariable("FACTORY_POSTGRES");
if (!hatchet && !string.IsNullOrWhiteSpace(postgres))
    throw new InvalidOperationException("Select Hatchet dispatch for the factory PostgreSQL database.");
if (hatchet && (string.IsNullOrWhiteSpace(dispatchToken) || dispatchToken == token || string.IsNullOrWhiteSpace(postgres)))
    throw new InvalidOperationException("Hatchet requires PostgreSQL and a distinct dispatcher token.");
var dispatchExpected = SHA256.HashData(Encoding.UTF8.GetBytes("Bearer " + dispatchToken));
var stallSeconds = int.Parse(Environment.GetEnvironmentVariable("FACTORY_STALL_SECONDS") ?? "120");
if (stallSeconds <= 0) throw new InvalidOperationException("FACTORY_STALL_SECONDS must be positive.");
var ciToken = Environment.GetEnvironmentVariable("FACTORY_CI_TOKEN");
if (project.Ci is not null && (string.IsNullOrWhiteSpace(ciToken) || ciToken == token || ciToken == dispatchToken))
    throw new InvalidOperationException("A CI project requires a distinct FACTORY_CI_TOKEN.");
var ciExpected = SHA256.HashData(Encoding.UTF8.GetBytes("Bearer " + ciToken));
var fundingProfile = FundingProfile.Load(Environment.GetEnvironmentVariable("FACTORY_FUNDING_PROFILE"));
if (project.Ci?.RequiresCodex == true &&
    (string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("FACTORY_LEASE_DB")) ||
     fundingProfile.SubscriptionFile is null || fundingProfile.FixtureApi is not null))
    throw new InvalidOperationException("Live dispatch requires a shared lease journal and subscription observation file.");
var expertRuntime = Environment.GetEnvironmentVariable("FACTORY_EXPERT_RUNTIME");
if (expertRuntime is not null && (expertRuntime != "codex-subscription" || !hatchet
    || string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("FACTORY_LEASE_DB"))))
    throw new InvalidOperationException("Subscription experts require Hatchet and a shared lease journal.");
var ownerRuntime = Environment.GetEnvironmentVariable("FACTORY_OWNER_RUNTIME");
if (ownerRuntime is not null && (ownerRuntime != "codex-subscription" || !hatchet ||
    string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("FACTORY_LEASE_DB"))))
    throw new InvalidOperationException("Subscription owner discussion requires Hatchet and a shared lease journal.");
var store = new TaskStore(new FactoryDatabase(directory, postgres), new Lifecycle(project, project.Ci, expertRuntime, ownerRuntime),
    () => LoadProject(project.Policy.Version), stallSeconds, project.Ci, fundingProfile, ownerRuntime is not null);
store.BindProject(project.Ci?.Hash ?? "shipping/v1");
var checkpoint = store.LatestRestore();
var restorePaused = checkpoint is { ReleasedUtc: null };
if (restorePaused && checkpoint!.Revision != ControllerDigest())
    throw new InvalidOperationException("Start the paused restore using the controller build recorded in its backup.");
if (!restorePaused)
{
    store.RestoreDispatchHolds();
    store.InitializeDashboard();
}
builder.Services.AddSingleton(store);
if (!manual && !restorePaused) builder.Services.AddHostedService<StallReconciler>();
if (!manual && !restorePaused && !hatchet) builder.Services.AddHostedService<Dispatcher>();
GitHubRepository? repository = null;
GitHubSync? github = null;
if (!restorePaused && Environment.GetEnvironmentVariable("FACTORY_GITHUB_PROJECT") is { Length: > 0 } profilePath)
{
    var config = GitHubProject.Load(profilePath, project);
    repository = new GitHubRepository(config, project.Policy.Version, project.Ci?.Rules.RepositoryLayout);
    github = new(config, repository, store, project, project.Ci);
    builder.Services.AddSingleton(config);
    builder.Services.AddSingleton(github);
    if (!manual && !restorePaused) builder.Services.AddHostedService<GitHubPoller>();
}
SlackTransport? slack = null;
if (!restorePaused && Environment.GetEnvironmentVariable("FACTORY_SLACK_PROFILE") is { Length: > 0 } slackPath)
{
    var profile = SlackProfile.Load(slackPath, project.Policy.Version);
    var secret = Environment.GetEnvironmentVariable("FACTORY_SLACK_SIGNING_SECRET");
    if (profile.DecisionUsers.Length > 0 && (string.IsNullOrWhiteSpace(secret) || secret.Length < 32 || secret == token || secret == dispatchToken || secret == ciToken))
        throw new InvalidDataException("Supply a distinct signing secret of at least 32 characters for the Slack fixture.");
    slack = new SlackTransport(store,profile,port,secret);
    builder.Services.AddSingleton(slack);
    if (!manual) builder.Services.AddHostedService<SlackPoller>();
}
using var funding = new OwnerFunding(store, fundingProfile);
var app = builder.Build();
if (slack is not null) app.Lifetime.ApplicationStopped.Register(slack.Dispose);
if (repository is not null) app.Lifetime.ApplicationStopped.Register(repository.Dispose);
app.UseDefaultFiles();
app.UseStaticFiles();
app.Use(async (ctx, next) =>
{
    var internalRoute = ctx.Request.Path.StartsWithSegments("/internal/dispatch");
    var ciRoute = ctx.Request.Path.StartsWithSegments("/internal/ci");
    if (internalRoute || ciRoute || ctx.Request.Path.StartsWithSegments("/api"))
    {
        var actual = SHA256.HashData(Encoding.UTF8.GetBytes(ctx.Request.Headers.Authorization.ToString()));
        if (!CryptographicOperations.FixedTimeEquals(actual, ciRoute ? ciExpected : internalRoute ? dispatchExpected : expected))
        {
            ctx.Response.StatusCode = 401;
            return;
        }
        ctx.Response.Headers.CacheControl = "no-store";
    }
    if (restorePaused && !HttpMethods.IsGet(ctx.Request.Method) && !HttpMethods.IsHead(ctx.Request.Method)
        && !(HttpMethods.IsPost(ctx.Request.Method) && (ctx.Request.Path == "/api/restore/release" || ctx.Request.Path == "/api/restore/retire")))
    {
        ctx.Response.StatusCode = StatusCodes.Status423Locked;
        await ctx.Response.WriteAsJsonAsync(new { error = "Restored controller is paused. Inspect reconciliation status before restarting dispatch." });
        return;
    }
    try { await next(); }
    catch (CommandConflict e)
    {
        ctx.Response.StatusCode = 409;
        await ctx.Response.WriteAsJsonAsync(new { error = e.Message });
    }
    catch (Exception e) when (ctx.Request.Path.StartsWithSegments("/api/github") && e is (HttpRequestException or TaskCanceledException or GitHubFailure))
    {
        app.Logger.LogWarning(e, "GitHub sync deferred for {Path}", ctx.Request.Path);
        ctx.Response.StatusCode = 502;
        await ctx.Response.WriteAsJsonAsync(new { error = e is GitHubFailure ? e.Message : "Repository connection interrupted. Poll to reconcile the operation." });
    }
    catch (BadHttpRequestException e) { ctx.Response.StatusCode = e.StatusCode; }
    catch (KeyNotFoundException) { ctx.Response.StatusCode = 404; }
});
if (project.Ci is not null)
{
    app.MapPost("/internal/ci/claim", () => Results.Ok(new { job = store.ClaimCi() }));
    app.MapPost("/internal/ci/{id}/unavailable", (string id, CiUnavailable body) => store.CiUnavailable(id, body));
    app.MapPost("/internal/ci/{id}/result", (string id, CiDelivery body) => store.ReceiveCi(id, body));
}
if (github is not null)
    app.MapPost("/api/github/issues/{number:int}/sync", (int number) => github.Poll(number));
app.MapGet("/api/restore", () => new { paused = restorePaused, restore = store.LatestRestore(), blockers = store.RestoreBlockers() });
if (restorePaused) app.MapPost("/api/restore/retire", (RestoreRetirement command) => store.RetireRestoredOperation(command));
app.MapPost("/api/restore/release", (RestoreRelease command) => store.ReleaseRestore(command));
if (slack is not null)
{
    app.MapPost("/callbacks/slack", (HttpRequest request) => slack.Callback(request));
    app.MapPost("/api/notifications/poll", async (CancellationToken stop) => new { attempted = await slack.Poll(stop) });
}
app.MapGet("/api/tasks/{id:guid}/notices", (Guid id) => store.NoticeHistory(id));
app.MapGet("/api/tasks", store.List);
var funds = app.MapGroup("/api/tasks/{id:guid}/funding");
funds.MapGet("", (Guid id) => store.FundingReport(id, fundingProfile));
funds.MapPost("/advice", (Guid id, AdviceRequest body) => funding.Ask(id, body));
funds.MapPost("/advice/{request:guid}/resume", (Guid id, Guid request, AttemptCommand body) => funding.Resume(id, request, body.Attempt));
funds.MapPost("/advice/{request:guid}/cancel", (Guid id, Guid request, AttemptCommand body) => funding.Cancel(id, request, body.Attempt));
funds.MapPost("/advice/{request:guid}/reconcile", (Guid id, Guid request, AttemptCommand body) => funding.Reconcile(id, request, body.Attempt));
app.MapGet("/api/dashboard/scenarios", () => new { project.Goal, project.Scenarios, restorePaused });
app.MapGet("/api/dashboard/queue", store.Queue);
app.MapGet("/api/tasks/{id:guid}/workspace", (Guid id) => store.Workspace(id));
app.MapGet("/api/tasks/{id:guid}/export", (Guid id) =>
    Results.File(store.PilotEvidence(id), "application/zip", $"factory-{id:D}.zip"));
app.MapGet("/api/tasks/{id:guid}/evidence", (Guid id) => store.Evidence(id));
app.MapPost("/api/tasks/{id:guid}/messages", (Guid id, Message body) =>
    body.Id == Guid.Empty || string.IsNullOrWhiteSpace(body.Text) || body.Text.Length > 4000
        ? Results.BadRequest() : Results.Ok(store.Discuss(id, body.Id, body.Text.Trim())));
app.MapGet("/api/tasks/{id:guid}/gates", (Guid id) => store.Gates(id));
app.MapGet("/api/tasks/{id:guid}/owner", (Guid id) => store.Owner(id));
app.MapGet("/api/tasks/{id:guid}/owner/documents/{part}", (Guid id, SourcePart part, string? path) => store.Document(id, part, path));
static IResult CommandResponse(HttpRequest request, WorkItem task) =>
    request.Headers["Prefer"] == "return=minimal" ? Results.NoContent() : Results.Ok(task);
app.MapPost("/api/tasks", (HttpRequest request, Create body) =>
    pinnedRepository || body.Id == Guid.Empty || !project.Supports(body.Scenario) || body.Message?.Length > 4000 ? Results.BadRequest()
        : CommandResponse(request, store.Create(body.Id, body.Scenario, string.IsNullOrWhiteSpace(body.Message) ? null : body.Message.Trim())));
app.MapPost("/api/tasks/{id:guid}/decision", (HttpRequest request, Guid id, Choice body) =>
    CommandResponse(request, store.Decide(id, body.Value, body.QuestionKey)));
app.MapPost("/api/tasks/{id:guid}/recover", (HttpRequest request, Guid id, RecoveryCommand body) =>
    CommandResponse(request, store.Recover(id, body)));
if (hatchet)
{
    var dispatch = app.MapGroup("/internal/dispatch");
    if (project.Ci is not null)
        dispatch.AddEndpointFilter(async (context, next) =>
            context.HttpContext.Request.RouteValues["id"] is string id && id.EndsWith(".Verify", StringComparison.Ordinal)
                ? Results.Conflict(new { error = "Verification belongs to the CI provider." }) : await next(context));
    dispatch.MapPost("/next", () => store.Next(remote: true));
    dispatch.MapPost("/{id}/submitted", (string id, Scheduler body) => store.Submitted(id, body.RunId));
    dispatch.MapPost("/{id}/claim", (string id, Scheduler body) => new { claim = store.Claim(id, body.RunId, body.SessionId) });
    dispatch.MapPost("/{id}/result", (string id, Delivery body) => store.Receive(id, body.Token, body.Output));
    dispatch.MapPost("/{id}/wait", (string id, RuntimeWait body) => store.ParkRuntime(id, body));
    dispatch.MapPost("/{id}/failed", (string id, Scheduler body) => store.Failed(id, body.RunId));
}
if (manual && !hatchet) app.MapPost("/api/step", () => new { advanced = store.Tick() });
app.Run();

record Scheduler(string RunId, Guid? SessionId = null);
record Delivery([property: JsonRequired] string Token, [property: JsonRequired] JsonElement Output);
record Create(Guid Id, string Scenario, string? Message = null);
record Choice(string Value, string? QuestionKey = null);
record Message(Guid Id, string Text);
public class CommandConflict(string message) : InvalidOperationException(message);
sealed class StallReconciler(TaskStore store, ILogger<StallReconciler> logger) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stop)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(30));
        do
        {
            try { store.ReconcileStalls(); }
            catch (System.Data.Common.DbException ex) when (ex.IsTransient)
            {
                logger.LogWarning("Database unavailable during stall inspection. Retrying on the next interval.");
            }
        } while (await timer.WaitForNextTickAsync(stop));
    }
}

sealed class Dispatcher(TaskStore store) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stop)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(350));
        while (await timer.WaitForNextTickAsync(stop)) store.Tick();
    }
}
