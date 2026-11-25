using System.Text.Json.Nodes;

namespace Factory;

public record BranchTarget(string Branch, int Pull, string HeadSha);
public record CleanupEntry(BranchTarget Target, string? Keep);

public sealed partial class GitHubRepository
{
    internal async Task<CleanupEntry[]> CleanupPlan(ApprovedIssue issue, IEnumerable<DeliveredRound> deliveries)
    {
        var candidates = new List<BranchTarget>();
        var repository = config.Repository.Split('/');
        const string query = """
            query IssueBranches($owner:String!, $name:String!, $number:Int!, $after:String) {
              repository(owner:$owner,name:$name) { issue(number:$number) {
                fullDatabaseId closedByPullRequestsReferences(first:100,after:$after,includeClosedPrs:true) {
                  pageInfo { hasNextPage endCursor }
                  nodes { number merged headRefName headRefOid repository { nameWithOwner } headRepository { nameWithOwner } }
                }
              } }
            }
            """;
        string? after = null;
        var cursors = new HashSet<string>();
        do
        {
            var response = await Call("", new {query, variables=new {owner=repository[0], name=repository[1], number=issue.Number, after}}, graphql:true);
            var item = response?["data"]?["repository"]?["issue"];
            var links = item?["closedByPullRequestsReferences"];
            if (response?["errors"] != null || !long.TryParse(item?["fullDatabaseId"]?.ToString(), out var id) || id!=issue.Id ||
                links?["nodes"] is not JsonArray nodes || links["pageInfo"]?["hasNextPage"] is not JsonValue more || !more.TryGetValue<bool>(out var hasNext))
                throw new GitHubFailure("GitHub could not confirm the pull requests linked to this issue.");
            foreach (var pr in nodes)
            {
                if ((bool?)pr?["merged"] != true ||
                    !string.Equals((string?)pr["repository"]?["nameWithOwner"],config.Repository,StringComparison.OrdinalIgnoreCase) ||
                    !string.Equals((string?)pr["headRepository"]?["nameWithOwner"],config.Repository,StringComparison.OrdinalIgnoreCase)) continue;
                if ((string?)pr["headRefName"] is not {Length: >0} branch || (int?)pr["number"] is not >0)
                    throw new GitHubFailure("GitHub returned an incomplete linked pull request.");
                candidates.Add(new(branch,(int)pr["number"]!,Sha(pr["headRefOid"])));
            }
            after = hasNext ? (string?)links["pageInfo"]!["endCursor"] : null;
            if (hasNext && (string.IsNullOrEmpty(after) || !cursors.Add(after)))
                throw new GitHubFailure("GitHub returned an incomplete list of linked pull requests.");
        } while (after != null);

        foreach (var round in deliveries)
        {
            if (round.OwnsBranch && (await PullState(round)).Merged)
                candidates.Add(new(round.Intent.Branch,round.Draft.Number,round.Draft.HeadSha));
        }
        var entries = new List<CleanupEntry>();
        foreach (var group in candidates.Distinct().GroupBy(c=>c.Branch).OrderBy(g=>g.Key,StringComparer.Ordinal))
        {
            // One ref can appear in multiple PRs. Only its current merged head can be removed.
            var head = await Ref(group.Key);
            var target = group.OrderByDescending(c=>c.HeadSha==head).ThenByDescending(c=>c.Pull).First();
            entries.Add(new(target,await KeepBranch(target)));
        }
        return entries.ToArray();
    }
}
