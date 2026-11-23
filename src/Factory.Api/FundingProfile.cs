using System.Net;
using System.Text.Json;

namespace Factory;

public record AllowanceWindow(int Minutes, decimal UsedPercent, long? ResetsAt = null);
public record SubscriptionObservation(DateTimeOffset ObservedUtc, string Auth, AllowanceWindow[]? Windows = null,
    string? AdvertisedDefaultModel = null);
public record MoneyObservation(long UsdMicros, DateTimeOffset AsOf, string Source);
public record FixtureAllocation(string Account, string Model, string Endpoint, long AccountLimitMicros,
    long RunLimitMicros, int MaxInputBytes, int MaxOutputTokens, int InputMicrosPerToken, int OutputMicrosPerToken,
    Dictionary<Guid,long> TaskLimitsMicros)
{
    public const long MaxAmount = 1_000_000_000_000;
    // Changing a ceiling keeps the same ledger. A different destination cannot inherit a pending operation.
    public string Identity => GateInput.Hash(new { Account, Model, Endpoint, InputMicrosPerToken, OutputMicrosPerToken });
    public void Validate()
    {
        if (!Uri.TryCreate(Endpoint, UriKind.Absolute, out var uri) || uri.Scheme != "http" ||
            !IPAddress.TryParse(uri.DnsSafeHost, out var ip) || !IPAddress.IsLoopback(ip) ||
            uri.UserInfo != "" || uri.AbsolutePath != "/" || uri.Query != "" || uri.Fragment != "" ||
            string.IsNullOrWhiteSpace(Account) || Account.Length > 100 || string.IsNullOrWhiteSpace(Model) || Model.Length > 100 ||
            MaxInputBytes is < 1 or > 32768 || MaxOutputTokens is < 1 or > 8192 ||
            InputMicrosPerToken is < 1 or > 100000 || OutputMicrosPerToken is < 1 or > 100000 ||
            AccountLimitMicros is < 0 or > MaxAmount || RunLimitMicros is < 1 or > MaxAmount ||
            TaskLimitsMicros is null || TaskLimitsMicros.Any(t => t.Key == Guid.Empty || t.Value is < 0 or > MaxAmount))
            throw new InvalidDataException("Invalid loopback provider, execution bounds or funding allocation.");
    }
}
public record FundingProfile(FixtureAllocation? FixtureApi = null, SubscriptionObservation? Subscription = null,
    MoneyObservation? ManualApiBalance = null, MoneyObservation? InfrastructureCost = null, string? SubscriptionFile = null)
{
    public static FundingProfile Load(string? path)
    {
        if (string.IsNullOrWhiteSpace(path)) return new();
        var profile = JsonSerializer.Deserialize<FundingProfile>(File.ReadAllText(path), Wire.ResultJson)
            ?? throw new InvalidDataException("Empty funding profile.");
        profile.FixtureApi?.Validate();
        if (profile.SubscriptionFile is { } source)
        {
            if (profile.Subscription is not null || string.IsNullOrWhiteSpace(source)) throw new InvalidDataException("Choose one subscription observation source.");
            profile = profile with { SubscriptionFile = Path.GetFullPath(source, Path.GetDirectoryName(Path.GetFullPath(path))!) };
        }
        if (profile.Subscription is { } sub && !ValidSubscription(sub))
            throw new InvalidDataException("Invalid subscription observation.");
        foreach (var item in new[] { profile.ManualApiBalance, profile.InfrastructureCost })
            if (item is not null && (!ValidDate(item.AsOf) || item.UsdMicros < 0 || string.IsNullOrWhiteSpace(item.Source)))
                throw new InvalidDataException("Monetary observations need a nonnegative amount, date and source.");
        return profile;
    }
    public SubscriptionObservation? ReadSubscription()
    {
        if (SubscriptionFile is null) return Subscription;
        try
        {
            using var file = File.OpenRead(SubscriptionFile);
            if (file.Length > 65536) return null;
            var snapshot = JsonSerializer.Deserialize<SubscriptionObservation>(file, Wire.ResultJson);
            return snapshot is not null && ValidSubscription(snapshot) ? snapshot : null;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException) { return null; }
    }
    private static bool ValidSubscription(SubscriptionObservation sub) => sub.Auth == "ChatGPT" && ValidDate(sub.ObservedUtc)
        && sub.Windows?.Any(w => w is null || w.Minutes <= 0 || w.UsedPercent is < 0 or > 100) != true;

    private static bool ValidDate(DateTimeOffset value) => value != default && value <= DateTimeOffset.UtcNow.AddMinutes(1);
}
