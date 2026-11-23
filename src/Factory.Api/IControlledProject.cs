namespace Factory;

// The controller and the dispatched fixture process share these deterministic project operations.
public interface IControlledProject
{
    CiProfile? Ci => null;
    string Goal { get; }
    ProjectPolicy Policy { get; }
    WorkerResult<string> Plan(Assignment assignment, PhaseContext? context = null);
    ProjectBrief Describe(SourcePacket snapshot);
    ScenarioOption[] Scenarios { get; }
    DecisionOption[] Choices(SourcePacket source);
    bool Supports(string scenario);
    SourcePacket Snapshot();
    WorkerResult<Question> Ask(Assignment assignment, string scenario, PhaseContext? context = null);
    Decision? Investigate(Question question, SourcePacket packet, BriefRevision? brief = null);
    ExpertProfile ExpertProfile { get; }
    Question AdmittedQuestion(string scenario);
    string[] ExpertSuggestions(Question question) => [];
    ExpertOpinion? Consult(ExpertRequest request);
    Decision? ResolveWithinScope(Question question, string? suggestedOption, SourcePacket source, BriefRevision brief);
    DecisionResolution ApplyChoice(string choice, SourcePacket packet);
    WorkerResult<string> Implement(Assignment assignment, SourcePacket source, Decision answer, Receipt? feedback, PhaseContext? context = null);
    // Pure policy checks on bounded fixture data. These methods perform no worker execution or I/O.
    string? CandidateError(string candidate);
    IReadOnlyList<Check> LocalChecks(string candidate, Decision answer, SourcePacket source, string[] selectors) =>
        throw new InvalidOperationException("No deterministic local checks installed for this project.");
    string? VerificationError(Receipt receipt, Decision answer, SourcePacket source);
    Receipt Verify(Assignment assignment, string candidate, Decision answer, SourcePacket packet, PhaseContext? context = null);
    WorkerResult<Review> Assess(Assignment assignment, string candidate, Decision answer, PhaseContext? context = null);
}
