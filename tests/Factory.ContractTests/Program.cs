using Factory;
using Factory.Fixtures;
using System.Text.Json;
using System.Text.Json.Nodes;

// Exercise only the Git object builder against a local transport. This cannot create refs or Drafts.
if (args is ["--check-scoped-tree", var localUrl])
{
    var project = new SampleRuntime("checkout-scope/v2");
    var ci = project.Ci!;
    var settings = ci.Rules.Settings;
    var revision = settings.GetProperty("base").GetString()!;
    var uri = new Uri(localUrl);
    if (uri.Scheme != "http" || !uri.IsLoopback) throw new InvalidDataException("Expected local Git fixture.");
    var config = new GitHubProject(settings.GetProperty("repository").GetString()!, settings.GetProperty("repositoryId").GetInt64(),
        "main", [], localUrl);
    using var git = new GitHubRepository(config, project.Policy.Version, ci.Rules.RepositoryLayout);
    var pinnedTree = await git.Snapshot(revision,project,ci);
    var candidate = Console.In.ReadToEnd();
    if (project.CandidateError(candidate) is not null) throw new InvalidDataException("Invalid scoped candidate.");
    var origin = new IssueOrigin(localUrl,config.RepositoryId,config.Repository,1,1,"main",revision,pinnedTree.Tree,
        pinnedTree.Source.Fingerprint(),pinnedTree.Path,"known-decision","contract-test");
    var intent = new DraftIntent(Guid.NewGuid(),"unused","unused",candidate,Wire.Digest(candidate),Guid.NewGuid(),Guid.NewGuid(),"unit",DateTimeOffset.UtcNow);
    Console.WriteLine(await git.BuildCommit(origin,intent));
    return;
}

// These are contract tests. They never submit native output into the lifecycle.
var checkout = new SampleRuntime("checkout-scope/v1");
var checkoutOwner = new Lifecycle(checkout, checkout.Ci);
var checkoutTask = checkoutOwner.Start(Guid.NewGuid(), "known-decision");
var scope = checkout.Ci!.Rules.RepositoryLayout!;
if (!scope.Valid() || (scope with { Source = "../outside" }).Valid() ||
    (scope with { Commands = [["node", "-e", "unadmitted"]] }).Valid())
    throw new Exception("Invalid checkout file or command scope.");
var checkoutDocs = checkout.Describe(checkoutTask.Source).Documents;
if (checkoutDocs.Length != 4 || !checkoutDocs.All(d => d.Matches(checkoutTask.Source, checkoutTask.Source.Fingerprint())))
    throw new Exception("Supplemental test source was lost from the pinned owner brief.");
for (int i = 0; i < 8; i++)
{
    var order = checkoutOwner.Prepare(checkoutTask) ?? throw new Exception("Checkout owner did not resume.");
    if (order.Action == WorkAction.Implement)
    {
        order.Validate(checkout);
        if (checkoutTask.Answer?.Value != "5000" || order.Context!.Documents.Length != 4)
            throw new Exception("Checkout policy or approved documents missing after owner decision.");
        try { order.Execute(checkout); throw new Exception("Scripted worker ran on the live checkout policy."); }
        catch (InvalidOperationException e) when (e.Message.Contains("native Codex")) { }
        break;
    }
    checkoutOwner.Apply(checkoutTask, order, order.Execute(checkout), Guid.NewGuid());
    if (i == 7) throw new Exception("Checkout owner did not reach the writer.");
}
Console.WriteLine("Passed: scoped checkout owner, pinned documents, command admission and required live writer");

// Submit faulty worker outputs at the real controller boundary without exposing a test HTTP route.
var fixture = new SampleRuntime();
foreach (var fault in new[] { "foreign-run", "stale-policy", "wrong-input", "unknown-schema", "failed-status", "empty-plan", "missing-check" })
{
    var owner = new Lifecycle(new FaultyWorker(fixture, fault));
    var task = owner.Start(Guid.NewGuid(), "known-decision");
    for (int i = 0; i < 24 && task.Activity != Activity.Blocked; i++)
        owner.Advance(task);
    if (task.Activity != Activity.Blocked || task.Repairs != 0 || task.Review is not null)
        throw new Exception($"Controller admitted or attempted to repair {fault}.");
    if (fault == "missing-check")
    {
        if (task.Receipts.Count != 1 || task.Phase != Phase.Verification || !task.Receipts[0].Passed)
            throw new Exception("An incomplete all-green receipt must be retained and denied at verification.");
    }
    else if (task.Plan is not null || task.Results.Count != 0 || task.Phase != Phase.Planning)
        throw new Exception($"Controller consumed a rejected planning result for {fault}.");
    Console.WriteLine($"Passed: {fault}");
}

// A repair that returns identical evidence must stop even if the project has a larger budget.
var stalledOwner = new Lifecycle(new FaultyWorker(fixture, "unchanged-repair", fixture.Policy with { MaxRepairs = 5 }));
var stalled = stalledOwner.Start(Guid.NewGuid(), "known-decision");
for (int i = 0; i < 40 && stalled.Activity != Activity.Blocked; i++)
{
    stalledOwner.Advance(stalled);
    stalled = JsonSerializer.Deserialize<WorkItem>(JsonSerializer.Serialize(stalled, Wire.Json), Wire.Json)!;
}
if (stalled.Activity != Activity.Blocked || stalled.Receipts.Count != 2 || stalled.Repairs != 1
    || !stalled.Gates[^1].Reason.Contains("same candidate") || stalledOwner.Prepare(stalled) is not null)
    throw new Exception("Unchanged evidence consumed another repair attempt.");
Console.WriteLine("Passed: repeated candidate and failed checks stop after one corrective attempt");

// Run different failed candidates under two trusted profiles. Restore between attempts.
foreach (var policy in new[] { fixture.Policy, new ProjectPolicy("audit/v1",
             [..fixture.Policy.RequiredChecks, "order-offset/2"], 2) })
{
    var project = new FaultyWorker(fixture, "persistent-failure", policy);
    var owner = new Lifecycle(project);
    var task = owner.Start(Guid.NewGuid(), "known-decision");
    for (int i = 0; i < 40 && task.Activity != Activity.Blocked; i++)
    {
        owner.Advance(task);
        task = JsonSerializer.Deserialize<WorkItem>(JsonSerializer.Serialize(task, Wire.Json), Wire.Json)!;
    }
    if (task.Activity != Activity.Blocked || task.Repairs != policy.MaxRepairs || task.Receipts.Count != policy.MaxRepairs + 1
        || task.Receipts.Any(r => r.PolicyVersion != policy.Version || r.Passed || !r.Checks.Select(c => c.Id).SequenceEqual(policy.RequiredChecks)))
        throw new Exception($"Incorrect restored verification policy or repair budget for {policy.Version}.");
    Console.WriteLine($"Passed: {policy.Version} check coverage and cumulative repair budget across restore");
}
try
{
    JsonSerializer.Deserialize<ProjectPolicy>("""{"version":"missing-limit","requiredChecks":["a"]}""", Wire.Json);
    throw new Exception("Missing repair limit was accepted.");
}
catch (JsonException) { Console.WriteLine("Passed: missing repair limit rejected"); }

// Conflicting acceptance evidence and ambiguous source decisions cannot ground an owner answer.
var snapshot = fixture.Snapshot();
var question = new Question("standard-threshold", "Which threshold applies?", false);
var duplicate = JsonNode.Parse(snapshot.Decisions)!.AsArray();
duplicate.Add(duplicate[0]!.DeepClone());
var contradicted = JsonNode.Parse(snapshot.Cases)!.AsArray();
contradicted.Add(new JsonObject { ["offsetCents"] = 0, ["freeShipping"] = false });
foreach (var incomplete in new[] { snapshot with { Decisions = duplicate.ToJsonString() },
    snapshot with { Cases = contradicted.ToJsonString() }, snapshot with { Decisions = "[]" } })
{
    if (fixture.Investigate(question, incomplete) is not null)
        throw new Exception("Ungrounded answer accepted.");
}
Console.WriteLine("Passed: missing, ambiguous and contradictory source evidence cannot resolve a question");

var packetOwner = new Lifecycle(fixture);
var firstOrder = packetOwner.Prepare(packetOwner.Start(Guid.NewGuid(), "known-decision"))!;
var brief = firstOrder.Context!.Brief;
var incompatible = brief with { Requirements = brief.Requirements with { Criteria = [new("different", "Different requirement.")] } };
if (fixture.Investigate(question, snapshot, incompatible) is not null)
    throw new Exception("The owner ignored a conflicting retained requirement.");
var secondOrder = packetOwner.Prepare(packetOwner.Start(Guid.NewGuid(), "known-decision"))!;
foreach (var invalid in new[] { secondOrder.Context!, firstOrder.Context! with { SchemaVersion = 77 },
    firstOrder.Context! with { Action = WorkAction.Review } })
{
    try
    {
        (firstOrder with { Context = invalid }).Execute(fixture);
        throw new Exception("Invalid phase context accepted.");
    }
    catch (InvalidDataException) { }
}
Console.WriteLine("Passed: foreign, wrong-action and unsupported phase contexts rejected");

var divergent = packetOwner.Start(Guid.NewGuid(), "known-decision");
var initialBrief = divergent.Memory!.Briefs[0];
divergent.Memory.Briefs[0] = initialBrief with { Goal = "unrelated goal" };
packetOwner.Advance(divergent);
if (divergent.Activity != Activity.Blocked || divergent.Assignments.Count != 0)
    throw new Exception("A divergent owner brief admitted a worker.");
Console.WriteLine("Passed: divergent owner brief blocks admission");

// Drive the public assignment/result boundary. Faults retain valid IDs and current hashes unless the test changes them.
void GateFault(WorkAction action, Action<JsonObject, WorkItem> alter, string reason, bool reuseWriter = false, string scenario = "known-decision")
{
    var lifecycle = new Lifecycle(fixture);
    var task = lifecycle.Start(Guid.NewGuid(), scenario);
    for (int i = 0; i < 32; i++)
    {
        var order = lifecycle.Prepare(task)!;
        var body = JsonNode.Parse(order.Execute(fixture).GetRawText())!.AsObject();
        if (order.Action != action)
        {
            lifecycle.Apply(task, order, JsonSerializer.SerializeToElement(body), Guid.NewGuid());
            continue;
        }
        alter(body, task);
        var session = reuseWriter ? task.Gates.Last(g => g.Input.OperationId?.EndsWith(".Implement") == true).Input.SessionId : Guid.NewGuid();
        lifecycle.Apply(task, order, JsonSerializer.SerializeToElement(body), session);
        var gate = task.Gates[^1];
        if (gate.Outcome != GateOutcome.Block || task.Activity != Activity.Blocked || gate.Input.OperationId != order.Id
            || gate.Input.OutputHash is null || !gate.Reason.Contains(reason, StringComparison.OrdinalIgnoreCase))
            throw new Exception($"Unexpected {action} gate: {gate.Outcome} {gate.Reason}");
        Console.WriteLine($"Passed: {action} blocks {reason}");
        return;
    }
    throw new Exception("Expected gate was never reached.");
}
GateFault(WorkAction.Ask, (body, _) => body["output"]!["reserved"] = false, "subject or authority", scenario: "scope-change");
GateFault(WorkAction.Ask, (body, _) => body["output"]!["text"] = "Approve a new pricing policy.", "subject or authority");
GateFault(WorkAction.Consult, (body, _) => body["requestHash"] = new string('0', 64), "grounded evidence", scenario: "expert-comparison");
GateFault(WorkAction.Consult, (body, _) => body["conclusion"] = new string('x', 1025), "grounded evidence", scenario: "scope-change");
GateFault(WorkAction.Plan, (body, _) => body["status"] = 0, "result contract");
GateFault(WorkAction.Plan, (body, _) => body.Remove("planEvidence"), "plan does not cover");
GateFault(WorkAction.Plan, (body, _) => body["planEvidence"]!["affectedPaths"] = new JsonArray("other.json"), "scope");
GateFault(WorkAction.Implement, (body, _) => body["output"] = "All tests passed", "scoped shipping rule");
GateFault(WorkAction.Verify, (body, _) =>
{
    foreach (var check in body["checks"]!.AsArray()) check!["passed"] = true;
}, "reported checks disagree");
GateFault(WorkAction.Review, (body, _) => body["output"]!.AsObject().Remove("evidence"), "verification evidence");
GateFault(WorkAction.Review, (body, _) => body["output"]!["evidence"]!["briefHash"] = Wire.Digest("old brief"), "brief");
GateFault(WorkAction.Review, (body, _) =>
    body["output"]!["evidence"]!["findings"] = new JsonArray(new JsonObject {
        ["criterion"] = "order-offset/0", ["severity"] = "Blocking", ["evidence"] = "Threshold remains exclusive.",
        ["recommendation"] = "Inspect the comparison." }), "blocking finding");
GateFault(WorkAction.Review, (_, _) => { }, "session was already used", reuseWriter: true);

// Drive both authority paths through the same worker, owner and gate contracts used by dispatch.
foreach (var (scenario, fault, expected, count) in new[] {
    ("known-decision", "normal", Activity.Delivered, 0),
    ("scope-change", "prior-product-ruling", Activity.Delivered, 0),
    ("expert-comparison", "normal", Activity.Delivered, 1),
    ("expert-comparison", "pricing-suggestion", Activity.Blocked, 1),
    ("expert-comparison", "foreign-opinion", Activity.Blocked, 1),
    ("scope-change", "foreign-citation", Activity.Blocked, 1),
    ("scope-change", "expert-budget", Activity.Blocked, 0),
    ("scope-change", "pricing-suggestion", Activity.Waiting, 1)
})
{
    var project = new FaultyWorker(fixture, fault);
    var owner = new Lifecycle(project);
    var task = owner.Start(Guid.NewGuid(), scenario);
    for (int i = 0; i < 40 && task.Activity is Activity.Queued or Activity.Running; i++) owner.Advance(task);
    if (task.Activity != expected || project.Consultations != count || task.HumanChoice is not null)
        throw new Exception($"Incorrect authority routing for {scenario}/{fault}.");
    if (expected != Activity.Delivered && task.Answer is not null)
        throw new Exception("Advice changed the decision without authority.");
    if (fault == "prior-product-ruling" && task.Answer?.Id != "SHIP-02")
        throw new Exception("An existing product ruling was not reused.");
    if (expected == Activity.Waiting)
    {
        if (task.Run is not null || task.Advice?.Consultation?.RequiredAuthority != "Operator")
            throw new Exception("Reserved decision held a worker or lost the required authority.");
        task.Advice = task.Advice with { Recommendation = "Changed after consultation" };
        try { owner.Decide(task, "approve"); throw new Exception("Altered advice authorized a decision."); }
        catch (CommandConflict) { }
    }
    Console.WriteLine($"Passed: {scenario}/{fault} -> {expected} with {count} consultations");
}

// A substantiated review revision shares the verification repair budget and survives restoration.
var reviewOwner = new Lifecycle(new FaultyWorker(fixture, "review-repair",
    new ProjectPolicy("review-repair/v1", fixture.Policy.RequiredChecks, 2)));
var reviewing = reviewOwner.Start(Guid.NewGuid(), "known-decision");
for (int i = 0; i < 32 && reviewing.Activity != Activity.Blocked; i++)
{
    var order = reviewOwner.Prepare(reviewing)!;
    if (order.Action == WorkAction.Implement && reviewing.Repairs == 2 && order.Context?.ReviewFeedback?.Approved != false)
        throw new Exception("Repair packet omitted the independent finding.");
    var output = order.Execute(fixture);
    if (order.Action == WorkAction.Review)
    {
        var result = output.Deserialize<WorkerResult<Review>>(Wire.Json)!;
        var evidence = result.Output.Evidence! with { Findings = [new("order-offset/0", FindingSeverity.Blocking,
            "Review requires a correction at the threshold.", "Recheck the boundary.")] };
        output = JsonSerializer.SerializeToElement(result with { Output = result.Output with { Approved = false, Evidence = evidence } }, Wire.Json);
    }
    reviewOwner.Apply(reviewing, order, output, Guid.NewGuid());
    reviewing = JsonSerializer.Deserialize<WorkItem>(JsonSerializer.Serialize(reviewing, Wire.Json), Wire.Json)!;
}
if (reviewing.Repairs != 2 || reviewing.Activity != Activity.Blocked ||
    !reviewing.Gates.Where(g => g.Input.Phase == Phase.Review).Select(g => g.Outcome)
        .SequenceEqual(new[] { GateOutcome.Revise, GateOutcome.Block }))
    throw new Exception("Review did not enforce the shared repair limit.");
Console.WriteLine("Passed: specific review finding routed once and cumulative repair limit enforced after restore");

// A restored task cannot spend the same repair round twice or reopen a terminal phase by changing its activity.
var guarded = packetOwner.Start(Guid.NewGuid(), "known-decision");
while (guarded.Repairs == 0) packetOwner.Advance(guarded);
var changedCount = JsonSerializer.Deserialize<WorkItem>(JsonSerializer.Serialize(guarded, Wire.Json), Wire.Json)!;
changedCount.Repairs--;
packetOwner.Prepare(changedCount);
if (changedCount.Activity != Activity.Blocked || !changedCount.Gates[^1].Reason.Contains("repair count"))
    throw new Exception("Repair count rollback accepted.");
while (guarded.Activity != Activity.Delivered) packetOwner.Advance(guarded);
guarded.Phase = Phase.Implementation;
guarded.Activity = Activity.Queued;
packetOwner.Prepare(guarded);
if (guarded.Activity != Activity.Blocked || !guarded.Gates[^1].Reason.Contains("did not authorize"))
    throw new Exception("Terminal transition reopened without authority.");
Console.WriteLine("Passed: repair counter rollback and unauthorized phase reopening denied");

var retail = new SampleRuntime("retail/v2");
var blueprint = retail.Ci!.Rules.Blueprint!;
var invalidTools = new Dictionary<string, string[]>(blueprint.Tools)
    { ["Review"] = [..blueprint.Tools["Review"], "candidate.write"] };
if ((blueprint with { Tools = invalidTools }).Valid(retail.Policy) ||
    (blueprint with { EarlyChecks = ["unregistered"] }).Valid(retail.Policy) ||
    (blueprint with { MaxCiRounds = 0 }).Valid(retail.Policy))
    throw new Exception("An invalid blueprint was admitted.");

var blueprintOwner = new Lifecycle(retail, retail.Ci);
var localTask = blueprintOwner.Start(Guid.NewGuid(), "known-decision");
for (int i = 0; i < 24 && localTask.Repairs == 0; i++) blueprintOwner.Advance(localTask);
var repairOrder = blueprintOwner.Prepare(localTask)!;
if (repairOrder.Action != WorkAction.Implement || localTask.Repairs != 1)
    throw new Exception("Expected a local repair before verification.");
var unchanged = JsonSerializer.Deserialize<WorkItem>(JsonSerializer.Serialize(localTask, Wire.Json), Wire.Json)!;
var repair = repairOrder.Execute(retail).Deserialize<WorkerResult<string>>(Wire.Json)!;
blueprintOwner.Apply(unchanged, repairOrder, JsonSerializer.SerializeToElement(repair with { Output = unchanged.Candidate! }, Wire.Json), Guid.NewGuid());
if (unchanged.Activity != Activity.Blocked || unchanged.Repairs != 1 || unchanged.Receipts.Count != 0 ||
    !unchanged.Gates[^1].Reason.Contains("unchanged failure"))
    throw new Exception("An unchanged local failure escaped the repair budget.");
blueprintOwner.Apply(localTask, repairOrder, JsonSerializer.SerializeToElement(repair, Wire.Json), Guid.NewGuid());
var accepted = localTask.Gates[^1];
if (accepted.Outcome != GateOutcome.Pass) throw new Exception("Expected a passing repaired candidate.");
localTask.Gates[^1] = accepted with { Criteria = accepted.Criteria.Where(c => c.Id != "local/order-offset/0").ToArray() };
if (blueprintOwner.Prepare(localTask) is not null || localTask.Activity != Activity.Blocked)
    throw new Exception("Verification started without mandatory local evidence.");
Console.WriteLine("Passed: project tool registration, local repair limit and pre-CI evidence");

// Both installed profiles admit the renderer blob at the fixture base, even after the offline command changes.
foreach (var version in new[] { FactorySummaryProject.Version, FactorySummaryProject.LiveVersion })
{
    var source = new FactorySummaryProject(version).Snapshot().Source;
    if (Wire.Digest(source) != "5262b76bdb7e5706c861a51c812d01968321537980097197eb6d593cdae33d06")
        throw new Exception($"Unexpected admitted renderer baseline for {version}.");
}
Console.WriteLine("Passed: factory profiles retain the pinned renderer snapshot");

OwnerDiscussionChecks.Run(fixture);

// Selecting subscription consultation pins the mode on the order and requires its own native receipt.
var expertDirectory = Directory.CreateTempSubdirectory();
try
{
    var expertDb = new FactoryDatabase(expertDirectory.FullName,null);
    var observationFile = Path.Combine(expertDirectory.FullName, "fixture-allowance.json");
    var store = new TaskStore(expertDb, new Lifecycle(fixture, expertRuntime:"codex-subscription"), () => fixture,
        funding: new FundingProfile(SubscriptionFile: observationFile));
    store.Create(Guid.NewGuid(), "scope-change");
    bool refused = false;
    for (int i=0; i<16 && !refused; i++)
    {
        var ticket = store.Next();
        if (ticket.Id is null) continue;
        var claim = store.Claim(ticket.Id,"local."+ticket.Id,Guid.NewGuid())!;
        var order = claim.Order;
        if (order.Action != WorkAction.Consult)
        {
            store.Receive(ticket.Id,claim.Token,order.Execute(fixture));
            continue;
        }
        if (claim.WorkerMode != "codex-subscription") throw new Exception("Lost pinned expert runtime.");
        try { order.Execute(fixture); throw new Exception("Scripted execution accepted a native expert request."); }
        catch (InvalidOperationException e) when (e.Message.Contains("native Codex")) {}
        var opinion = fixture.Consult(order.Consultation!)!;
        try { store.Receive(ticket.Id,claim.Token,JsonSerializer.SerializeToElement(opinion,Wire.Json)); }
        catch (CommandConflict e) when (e.Message.Contains("native Codex turn")) { refused=true; }
        if (refused) CheckExpertRecovery(store, expertDb, order, claim, observationFile);
    }
    if (!refused || store.List().Single().Advice is not null) throw new Exception("Native expert evidence was bypassed.");
    Console.WriteLine("Passed: live expert refuses fixture execution and unsigned advice on a real dispatch claim");
}
finally { Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools(); expertDirectory.Delete(true); }

// Entirely synthetic allowance records for this disposable task. No native session is opened.
static void CheckExpertRecovery(TaskStore store, FactoryDatabase db, WorkOrder order, DispatchClaim claim, string observationFile)
{
    var id = order.Id;
    var task = order.Assignment.TaskId;
    try
    {
        store.ParkRuntime(id, new(claim.Token, "Budget"));
        throw new Exception("A native claim accepted an API budget wait.");
    }
    catch (CommandConflict e) when (e.Message.Contains("budget wait")) { }
    store.ParkRuntime(id, new(claim.Token, "Quota"));
    store.ParkRuntime(id, new(claim.Token, "Quota", "Subscription"));
    var blocked = store.List().Single();
    var gate = store.Gates(task).Last();
    var command = new RecoveryCommand(id, gate.Sequence, "Controlled expert recovery regression.");
    string? Clearance() => db.Read(s => s.Text("SELECT clearance FROM subscription_waits WHERE id=@id", ("id",id)));
    void Observe(DateTimeOffset time, int fiveHourUsed, int weeklyUsed = 40) => File.WriteAllText(observationFile,
        JsonSerializer.Serialize(new SubscriptionObservation(time, "ChatGPT", [new(300,fiveHourUsed),new(10080,weeklyUsed)]), Wire.Json));
    void ExpectHold()
    {
        try { store.Recover(task, command); throw new Exception("Recovered without current subscription capacity."); }
        catch (CommandConflict e) when (e.Message.Contains("Refresh subscription")) { }
        if (store.List().Single().Activity != Activity.Blocked || store.Next().Id is not null || Clearance() is not null)
            throw new Exception("Rejected recovery changed the blocked task or its clearance.");
    }
    if (db.Read(s => s.Text("SELECT observed_after FROM subscription_waits WHERE id=@id", ("id",id))) is null)
        throw new Exception("Subscription claim lost its funding requirement.");
    ExpectHold();
    // Recreate the older checkpoint shape, whose quota hold predates controller-derived tagging.
    db.Write(s => s.Execute("DELETE FROM subscription_waits WHERE id=@id", ("id",id)));
    Observe(gate.EvaluatedAt.AddMinutes(-1), 10);
    ExpectHold();
    Observe(DateTimeOffset.UtcNow, 100);
    ExpectHold();
    Observe(DateTimeOffset.UtcNow, 10, 100);
    ExpectHold();
    Observe(DateTimeOffset.UtcNow, 10);
    var recovered = store.Recover(task, command);
    store.Recover(task, command);
    if (recovered.Activity != Activity.Queued || recovered.OwnerId != blocked.OwnerId
        || Clearance() != File.ReadAllText(observationFile) || store.Gates(task).Count(g => g.Outcome == GateOutcome.Resume) != 1)
        throw new Exception("Expected one recorded recovery and matching clearance.");
    var next = store.Next();
    if (next.Id is null || next.Id == id || !next.Id.EndsWith(".Consult")
        || store.Claim(id, "local."+id, Guid.NewGuid()) is not null)
        throw new Exception("Expected a fresh consultation and a fenced prior claim.");
    try
    {
        store.Receive(id, claim.Token, JsonSerializer.SerializeToElement(new { }));
        throw new Exception("A retired expert claim still accepts a result.");
    }
    catch (CommandConflict e) when (e.Message.Contains("retired")) { }
    var successor = store.Claim(next.Id, "local."+next.Id, Guid.NewGuid());
    if (successor?.WorkerMode != "codex-subscription" || successor.Order.Assignment.TaskId != task
        || successor.Order.Consultation!.Question != order.Consultation!.Question)
        throw new Exception("Expert recovery lost the original task, question or runtime.");
    Console.WriteLine("Passed: native expert quota hold, historical clearance, replay and fenced successor");
}

// A correct assignment and a valid dispatch token still require native execution evidence.
var temporary = Directory.CreateTempSubdirectory("factory-native-contract-");
var priorJournal = Environment.GetEnvironmentVariable("FACTORY_LEASE_DB");
try
{
    Environment.SetEnvironmentVariable("FACTORY_LEASE_DB", Path.Combine(temporary.FullName,"absent.sqlite"));
    var live = new FactorySummaryProject(FactorySummaryProject.LiveVersion);
    var storage = new TaskStore(new FactoryDatabase(temporary.FullName,null),new Lifecycle(live,live.Ci),()=>live,ciProfile:live.Ci);
    var id = Guid.NewGuid();
    storage.Create(id,"render-repairs");
    bool rejected = false;
    for (int step=0; step<15 && !rejected; step++)
    {
        var ticket = storage.Next(remote:true);
        if (ticket.Id is null) continue;
        var scheduler = Guid.NewGuid().ToString();
        var claim = storage.Claim(ticket.Id,scheduler,Guid.NewGuid())!;
        var order = claim.Order;
        var expectedWorker = order.Action == WorkAction.Implement ? "codex-subscription" : "controlled";
        if (claim.WorkerMode != expectedWorker) throw new Exception("Worker mode differs from the installed policy.");
        if (order.Action != WorkAction.Implement)
        {
            storage.Receive(ticket.Id,claim.Token,order.Execute(live));
            continue;
        }
        var unsupported = new WorkerResult<string>(order.Assignment.Reference(),ResultStatus.Completed,order.Source.Source);
        try { storage.Receive(ticket.Id,claim.Token,JsonSerializer.SerializeToElement(unsupported,Wire.Json)); }
        catch (CommandConflict ex) when (ex.Message.Contains("native Codex turn")) { rejected = true; }
        if (storage.List().Single().Candidate is not null) throw new Exception("Accepted an unexecuted native result.");
    }
    if (!rejected) throw new Exception("A live writer bypassed its lease and native evidence gate.");
    Console.WriteLine("Passed: normal result intake rejects an assigned live writer with no native execution evidence");
}
finally
{
    Environment.SetEnvironmentVariable("FACTORY_LEASE_DB",priorJournal);
    Microsoft.Data.Sqlite.SqliteConnection.ClearAllPools();
    temporary.Delete(true);
}

// Emit real messages when checking the published JSON Schema.
if (args is [var exampleDirectory])
{
    Directory.CreateDirectory(exampleDirectory);
    var owner = new Lifecycle(fixture);
    var task = owner.Start(Guid.NewGuid(), "known-decision");
    owner.Advance(task);
    void Save<T>(string name, T value) =>
        File.WriteAllText(Path.Combine(exampleDirectory, name + ".json"), JsonSerializer.Serialize(value, Wire.Json));
    Save("blueprint-context", repairOrder.Context);
    Save("assignment", task.Run);
    Save("worker-result", fixture.Plan(task.Run!, owner.Prepare(task)!.Context));
    Save("project-policy", task.Policy);
    for (int i = 0; i < 24 && task.Receipts.Count == 0; i++) owner.Advance(task);
    Save("receipt", task.Receipts[0]);
    Save("gate-evaluation", task.Gates[^1]);
    Save("phase-context", owner.Prepare(task)!.Context);
}

sealed class FaultyWorker(SampleRuntime sample, string fault, ProjectPolicy? profile = null) : IControlledProject
{
    public ProjectPolicy Policy => profile ?? sample.Policy;
    public string Goal => sample.Goal;
    public ProjectBrief Describe(SourcePacket source) => sample.Describe(source);
    public ScenarioOption[] Scenarios => sample.Scenarios;
    public DecisionOption[] Choices(SourcePacket source) => sample.Choices(source);
    public bool Supports(string scenario) => sample.Supports(scenario);
    public SourcePacket Snapshot()
    {
        var source = sample.Snapshot();
        if (fault == "prior-product-ruling")
        {
            var records = JsonNode.Parse(source.Decisions)!.AsArray();
            records.Add(JsonSerializer.SerializeToNode(new Decision("SHIP-02", "lower-threshold",
                "The recorded product ruling retains 5000 cents for this proposal.", "5000"), Wire.Json));
            return source with { Decisions = records.ToJsonString() };
        }
        if (Policy.Version != "audit/v1") return source;
        var cases = JsonNode.Parse(source.Cases)!.AsArray();
        cases.Add(new JsonObject { ["offsetCents"] = 2, ["freeShipping"] = true });
        return source with { Cases = cases.ToJsonString() };
    }
    public WorkerResult<string> Plan(Assignment run, PhaseContext? context = null)
    {
        var result = sample.Plan(run, context);
        var reference = result.Assignment;
        return fault switch
        {
            "foreign-run" => result with { Assignment = reference with { RunId = Guid.NewGuid() } },
            "stale-policy" => result with { Assignment = reference with { PolicyVersion = "shipping/obsolete" } },
            "wrong-input" => result with { Assignment = reference with { InputHash = Wire.Digest("different source") } },
            "unknown-schema" => result with { Assignment = reference with { SchemaVersion = 99 } },
            "failed-status" => result with { Status = ResultStatus.Failed },
            "empty-plan" => result with { Output = "" },
            _ => result
        };
    }
    public WorkerResult<Question> Ask(Assignment run, string scenario, PhaseContext? context = null) => sample.Ask(run, scenario, context);
    public Decision? Investigate(Question question, SourcePacket packet, BriefRevision? brief = null) => sample.Investigate(question, packet, brief);
    public ExpertProfile ExpertProfile => fault == "expert-budget" ? sample.ExpertProfile with { MaxSourceBytes = 1 } : sample.ExpertProfile;
    public Question AdmittedQuestion(string scenario) => sample.AdmittedQuestion(scenario);
    public int Consultations { get; private set; }
    public ExpertOpinion? Consult(ExpertRequest request)
    {
        Consultations++;
        if (!request.Sources.Select(s => s.Path).Order().SequenceEqual(new[] { "acceptance.json", "decisions.json" })
            || request.Sources.Any(s => s.Digest != Wire.Digest(s.Content)))
            throw new Exception("Expert received sources outside its pinned profile.");
        var opinion = sample.Consult(request)!;
        return fault switch
        {
            "foreign-opinion" => opinion with { RequestHash = Wire.Digest("another question") },
            "foreign-citation" => opinion with { Citations = ["shipping-rule.json"] },
            "pricing-suggestion" => opinion with { SuggestedOption = "approve" },
            _ => opinion
        };
    }
    public Decision? ResolveWithinScope(Question question, string? option, SourcePacket source, BriefRevision brief) =>
        sample.ResolveWithinScope(question, option, source, brief);
    public DecisionResolution ApplyChoice(string choice, SourcePacket packet) => sample.ApplyChoice(choice, packet);
    public WorkerResult<string> Implement(Assignment run, SourcePacket source, Decision answer, Receipt? feedback, PhaseContext? context = null)
    {
        if (fault == "unchanged-repair") return sample.Implement(run, source, answer, null);
        if (fault == "persistent-failure" && feedback is not null)
        {
            var rule = JsonNode.Parse(feedback.Candidate)!.AsObject();
            rule["thresholdCents"] = (int)rule["thresholdCents"]! + 1;
            return new(run.Reference(), ResultStatus.Completed, rule.ToJsonString());
        }
        return sample.Implement(run, source, answer, feedback, context);
    }
    public string? CandidateError(string candidate) => sample.CandidateError(candidate);
    public string? VerificationError(Receipt receipt, Decision answer, SourcePacket source) =>
        sample.VerificationError(receipt, answer, source);
    public Receipt Verify(Assignment run, string candidate, Decision answer, SourcePacket packet, PhaseContext? context = null)
    {
        var receipt = sample.Verify(run, candidate, answer, packet, context);
        return fault == "missing-check" ? receipt with { Checks = receipt.Checks.Where(c => c.Passed).ToList() } : receipt;
    }
    public WorkerResult<Review> Assess(Assignment run, string candidate, Decision answer, PhaseContext? context = null) => sample.Assess(run, candidate, answer, context);
}
