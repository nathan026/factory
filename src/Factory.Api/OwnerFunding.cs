using System.Net;
using System.Text.Json;

namespace Factory;

public record AttemptCommand(int Attempt);
public record ProviderRejection(Guid RequestId, int Attempt, string Account, string Model, bool Executed);
public record CancellationReceipt(Guid RequestId, int Attempt, string Account, string Model, string Status, bool Final, int InputTokens, int OutputTokens);
public record ProviderAvailability(string Account, string Model, bool Authenticated, bool QuotaAvailable);

// Only the trusted controller reaches the loopback qualification provider. Workers receive no billing credentials.
public sealed class OwnerFunding(TaskStore store, FundingProfile profile) : IDisposable
{
    private readonly HttpClient http = new(new HttpClientHandler { AllowAutoRedirect = false, UseProxy = false,
        UseCookies = false, UseDefaultCredentials = false })
        { Timeout = TimeSpan.FromSeconds(10), MaxResponseContentBufferSize = 65536 };

    private FixtureAllocation Allocation => profile.FixtureApi ?? throw new CommandConflict("Owner-advice allocation is disabled.");
    public void Dispose() => http.Dispose();

    public Task<FundedAdvice> Ask(Guid task, AdviceRequest request) =>
        Submit(task, store.BeginAdvice(task, request, Allocation));

    public async Task<FundedAdvice> Resume(Guid task, Guid id, int attempt)
    {
        var prior = store.Advice(task, id);
        if (prior.Latest.Attempt != attempt || prior.Latest.State is not ("Quota" or "Budget" or "Authentication")) return prior;
        var allocation = Allocation;
        store.CheckFundingIdentity(task, id, allocation);
        var wait = store.AccountWait(allocation.Account);
        if (wait is not null)
        {
            if (wait.RetryAt > DateTimeOffset.UtcNow.ToUnixTimeSeconds()) return prior;
            // Probe the provider without submitting an inference request. Only clear the epoch that was observed.
            try
            {
                using var response = await http.GetAsync(Url("availability"));
                if (!response.IsSuccessStatusCode) return prior;
                var available = await Read<ProviderAvailability>(response);
                if (available is not { Authenticated:true, QuotaAvailable:true } ||
                    available.Account != allocation.Account || available.Model != allocation.Model) return prior;
            }
            catch (Exception e) when (TransportError(e)) { return prior; }
        }
        return await Submit(task, store.BeginAdvice(task, new(id, prior.Question), allocation, attempt, wait?.Epoch));
    }

    public async Task<FundedAdvice> Reconcile(Guid task, Guid id, int attempt)
    {
        var prior = store.Advice(task, id);
        if (prior.Latest.Attempt != attempt || prior.Latest.State is not ("InFlight" or "Unconfirmed" or "CancelPending")) return prior;
        store.CheckFundingIdentity(task, id, Allocation);
        try
        {
            using var response = await http.GetAsync(Url($"receipts/{id:D}/{attempt}"));
            if (response.IsSuccessStatusCode) return await Resolve(task, id, attempt, response);
        }
        catch (Exception e) when (TransportError(e)) { }
        return store.Advice(task, id);
    }

    public async Task<FundedAdvice> Cancel(Guid task, Guid id, int attempt)
    {
        var cancellation = store.CancelAdvice(task, id, attempt, profile.FixtureApi);
        if (!cancellation.Send) return cancellation.Request;
        try
        {
            using var response = await http.PostAsJsonAsync(Url($"cancellations/{id:D}/{attempt}"), new { Allocation.Account, Allocation.Model }, Wire.Json);
            if (response.IsSuccessStatusCode) return await Resolve(task, id, attempt, response);
        }
        catch (Exception e) when (TransportError(e)) { }
        return store.Advice(task, id);
    }

    private async Task<FundedAdvice> Resolve(Guid task, Guid id, int attempt, HttpResponseMessage response)
    {
        using var document = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
        var root = document.RootElement;
        if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty("status", out _))
        {
            var c = root.Deserialize<CancellationReceipt>(Wire.ResultJson);
            if (c is { Status:"cancelled", Final:true, InputTokens:0, OutputTokens:0 } && c.RequestId == id && c.Attempt == attempt &&
                c.Account == Allocation.Account && c.Model == Allocation.Model)
                return store.ConfirmCancellation(task, id, attempt);
        }
        else if (await ValidReceipt(response, id, attempt) is { } receipt)
            return store.FinishAdvice(task, id, attempt, Allocation, receipt);
        return store.Advice(task, id);
    }

    private async Task<FundedAdvice> Submit(Guid task, AdviceTicket ticket)
    {
        var prior = ticket.Request;
        if (ticket.Input is null) return prior;
        var allocation = Allocation;
        var id = prior.Id;
        var attempt = prior.Latest.Attempt;
        try
        {
            using var response = await http.PostAsJsonAsync(Url("owner/requests"), new { requestId = id, attempt, allocation.Account,
                allocation.Model, input = ticket.Input, allocation.MaxOutputTokens, tools = Array.Empty<string>() }, Wire.Json);
            if (response.IsSuccessStatusCode && await ValidReceipt(response, id, attempt) is { } receipt)
                return store.FinishAdvice(task, id, attempt, allocation, receipt);
            if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden or HttpStatusCode.TooManyRequests &&
                await Read<ProviderRejection>(response) is { Executed:false } rejection && rejection.RequestId == id &&
                rejection.Attempt == attempt && rejection.Account == allocation.Account && rejection.Model == allocation.Model)
            {
                var retry = response.Headers.RetryAfter;
                var reset = retry?.Date ?? (retry?.Delta is { } delay ? DateTimeOffset.UtcNow + delay : (DateTimeOffset?)null);
                return store.DeferAdvice(task, id, attempt, response.StatusCode == HttpStatusCode.TooManyRequests ? "Quota" : "Authentication",
                    reset?.ToUnixTimeSeconds());
            }
        }
        catch (Exception e) when (TransportError(e)) { }
        return store.DeferAdvice(task, id, attempt, "Unconfirmed");
    }

    private Uri Url(string path) => new(new Uri(Allocation.Endpoint), path);
    private static bool TransportError(Exception e) => e is HttpRequestException or OperationCanceledException or JsonException or IOException;
    private static async Task<T?> Read<T>(HttpResponseMessage response) =>
        JsonSerializer.Deserialize<T>(await response.Content.ReadAsStringAsync(), Wire.ResultJson);

    private async Task<AdviceReceipt?> ValidReceipt(HttpResponseMessage response, Guid id, int attempt)
    {
        var r = await Read<AdviceReceipt>(response);
        return r is not null && r.RequestId == id && r.Attempt == attempt && r.Account == Allocation.Account && r.Model == Allocation.Model &&
            !string.IsNullOrWhiteSpace(r.ResponseId) && r.ResponseId.Length <= 200 && !string.IsNullOrWhiteSpace(r.Answer) &&
            r.Answer.Length <= 8192 && r.InputTokens >= 0 && r.OutputTokens >= 0 ? r : null;
    }
}
