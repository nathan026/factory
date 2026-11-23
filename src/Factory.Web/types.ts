export type Phase = "Planning" | "Implementation" | "Verification" | "Review" | "Delivery";
export type Activity = "Queued" | "Running" | "Waiting" | "Blocked" | "Delivered";
export type Task = {
  id: string;
  scenario: string;
  goal: string;
  phase: Phase;
  activity: Activity;
  openedAt: string | null;
  draftNumber?: number | null;
  issueFinished?: boolean;
  lastActivity?: string;
  waitReason?: string;
};
export type QueueRow = { task: Task; revision: number; stateRevision: number };
export type GitHubIntake = {
  repository: string;
  repositoryId: number;
  issues: { number: number; id: number; scopeHash: string; scenario: string }[];
};
export type Catalog = { issueActions?: boolean; restorePaused: boolean; goal: string; scenarios: { id: string; label: string }[]; githubIntake?: GitHubIntake | null };
export type Exchange = { id: string; afterEntry: number; text: string; reply: string; sentAt: string };
export type IssueAction = { id: string; kind: "follow-up" | "finish"; stage: string; scope: string; baseSha: string; headSha: string; commentId: string | null };
export type Workspace = Task & {
  revisionAvailable?: boolean;
  writerRestart?: boolean;
  previousDrafts?: {number: number; url: string; headSha: string}[];
  issueAction?: IssueAction | null;
  summary: { issue: string; request: string | null; work: string[]; checks: string[] };
  recovery: {
    operationId: string;
    gateSequence: number;
    kind: string;
    reason: string;
    since: string;
    schedulerId: string | null;
    sessionId: string | null;
    retainedOutputHash: string | null;
  } | null;
  repository?: {
    repository: string;
    number: number;
    issueUrl: string;
    baseSha: string;
    eligibility: string;
    awaitingConfirmation: boolean;
    requiresReconciliation?: boolean;
    problem: string | null;
    draft: { number: number; url: string; headSha: string } | null;
  } | null;
  ownerId: string;
  nextAction: string;
  run: { phase: Phase; runId: string } | null;
  history: { number: number; actor: string; text: string }[];
  messages: Exchange[];
  brief: {
    version: number;
    requirements: { criteria: { id: string; text: string }[]; constraints: string[]; exclusions: string[] };
    plan: string | null;
  };
  decisions: {
    actor: string;
    authority: string;
    decision: { text: string };
    rationale: string;
    sources: string[];
  }[];
  question: { text: string } | null;
  advice: {
    source: string;
    recommendation: string;
    alternative: string;
    consequence: string;
    consultation?: {
      profileVersion: string;
      domain: string;
      requiredAuthority: string;
      briefVersion: number;
      question: { text: string };
      conclusion: string;
      assumption: string;
      uncertainty: string;
      sources: { path: string; digest: string }[];
    };
  } | null;
  choices: { value: string; label: string }[];
  questionKey: string;
};
export type Evidence = {
  ciPolicy?: { policy: { version: string; maxRepairs: number }; environment: string } | null;
  ciBudget?: { admitted: number; limit: number; secondsPerRound: number } | null;
  earlierVerification?: { policy: string; goal: string; attempts: { candidateHash: string; failed: string[] }[] }[];
  ciExecutions?: {
    binding: { executionId: string; revisionKind: string; revision: string; candidateHash: string };
    checks: { id: string; exitCode: number }[];
  }[];
  id: string;
  gates: {
    sequence: number;
    outcome: string;
    reason: string;
    nextPhase: string;
    nextActivity: string;
    input: { phase: string; sessionId: string | null };
    criteria: { id: string; passed: boolean; finding: string }[];
  }[];
  receipts: { passed: boolean; candidateHash: string; checks: { assertion: string; passed: boolean }[] }[];
  rejectedReceipt: { index: number; reason: string } | null;
  candidate: string | null;
  review: { finding: string; approved: boolean } | null;
  documents: { part: string; path: string; digest: string; revision: string }[];
};
export type Api = <T>(
  path: string,
  options?: { body?: object; signal?: AbortSignal; minimal?: boolean; download?: boolean; timeoutMs?: number },
) => Promise<T>;

export function status(task: Task) {
  if (task.issueFinished) return "Issue closed";
  if (task.draftNumber) return "Draft PR #" + task.draftNumber;
  return task.activity === "Waiting"
    ? "Decision needed"
    : task.activity === "Delivered"
      ? "Delivered locally"
      : task.activity;
}

export function age(date: string | null) {
  if (!date) return "";
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(date)) / 60000));
  if (!minutes) return "Just opened";
  if (minutes < 60) return minutes + "m ago";
  if (minutes < 1440) return Math.floor(minutes / 60) + "h ago";
  return Math.floor(minutes / 1440) + "d ago";
}
