# Factory requirements and acceptance

The lifecycle owner takes each admitted task from requirements to a verified candidate. Independent review precedes Draft publication. Project policies define the allowed repository, revision, tools and file scope. The running factory uses a separate workspace from the code under development.

The identifiers below link requirements, work packages and acceptance tests. They describe the intended behavior. Qualification results belong to the deployment where the checks ran.

### Issue and work summaries

The task desk shows the request, completed changes, verification results and the next action on the main task view. Each new Draft PR shows the same work and check results above the publication evidence. Report outcomes for the current candidate. Keep the issue author's wording distinct from factory findings. Keep file names, values and check names exact.

Use ASD-STE100 descriptive writing guidance for factory-authored text. Use one term for each concept. Write one topic per paragraph. Give each sentence one subject and use at most 25 words in a descriptive sentence. Use direct instructions for the operator's next action. Preserve source quotations and identifiers as evidence. [ASD-STE100, Issue 9, sections 5 and 6](https://www.asd-ste100.org/assets/files/ASD-STE100_ISSUE9.pdf)

For an open GitHub issue with no assigned scope, the operator can discuss the issue with its lifecycle owner before admitting any coding work (FR02, FR03, FR10). The owner inspects the project's permitted files at the pinned revision, collects missing requirements and proposes files and checks. The controller can accept a proposal within the issue's existing authorization. Broader permissions or new executable checks require project setup. Keep the conversation and owner when the operator starts coding.

A desk operator may authorize a follow-up on a delivered GitHub task. Record the revised objective and exact base under the installed project permissions. Keep the same lifecycle owner, prior briefs, delivery evidence and PR links. Require fresh verification and independent review for the next delivery. After the operator accepts the merged work, publish the work and check summary, close the issue and reconcile safe branch cleanup. Repository issue writes require explicit opt-in.

## Functional requirements

| ID | Requirement | Behavior |
| --- | --- | --- |
| FR01 | Standalone operation | Manage unrelated sample repositories and the factory repository without special-case code |
| FR02 | Persistent lifecycle owner | Same task identity, decisions and next action after restart |
| FR03 | Conversation before issue creation | Capture requirements locally, then link an issue when appropriate |
| FR04 | Evidence-based planning | Testable criteria, scope, risks and dependencies; no mandatory manual form repair |
| FR05 | Fresh phase workers | Distinct run and session identities across planning, implementation and review |
| FR06 | Structured handoffs | Objective, permissions, source and decision versions, outputs and limits are explicit |
| FR07 | Enforced gates | Worker completion alone cannot advance the lifecycle |
| FR08 | Owner-first question routing | Existing decision answers reach the worker without operator intervention |
| FR09 | Scoped expert advice | Sources, uncertainty and recommendation appear in the task record |
| FR10 | Human discussion and decisions | Human operator can discuss, decide and resume from the dashboard |
| FR11 | Wait and continue | A waiting task releases capacity and another eligible task runs |
| FR12 | Independent verification and review | Evidence applies to the actual candidate; writer cannot satisfy its own independent review |
| FR13 | Bounded remediation | Repairs have specific findings and cumulative limits |
| FR14 | Recovery and cancellation | No overlapping writers, duplicate publication or silent lost questions |
| FR15 | Project policy | Different repositories select different gates without core code changes |
| FR16 | Runtime isolation | Worker permissions are enforced outside the prompt |
| FR17 | Usage and funding control | Explicit model/auth mode, quotas and allocations; no automatic paid fallback |
| FR18 | Draft delivery | Reviewable scope, changes, checks, decisions and limitations |
| FR19 | Useful activity | Phase, worker, meaningful progress, waiting reason and next action visible |
| FR20 | Audit and export | Durable context, decisions, receipts and provenance can be exported |
| FR21 | Eligible intake | Configured bugs can enter automatically; duplicates and explicit holds are respected |
| FR22 | Low overhead | No idle model polling, unnecessary full tests or unbounded expert fan-out |
| FR23 | Optional notifications | Configured notifications deep-link to the authoritative task; duplicate-safe |
| FR24 | Measured reflection | Produce a read-only pilot retrospective and a reviewable improvement proposal |
| FR25 | Isolated execution lease | Every coding task gets a bounded disposable environment and unique lease; unrelated files, credentials and processes cannot cross tasks |
| FR26 | Environment provisioning modes | Support a restricted container backend initially and an explicit VM backend interface; no silent weakening of requested isolation |
| FR27 | Environment readiness and pre-warming | Immutable templates, preflight checks and optional sanitized warm pool; report acquisition time and resource use |
| FR28 | Workspace snapshot and handoff | Implementer/checker/reviewer consume the exact immutable candidate as permitted; isolation never substitutes for a fresh review session |
| FR29 | Context hydration and tool curation | Resolve issue links and project instructions before execution; pass a scoped evidence packet and limited authorized tools |
| FR30 | Early, bounded feedback | Run fast deterministic local checks before expensive CI, with controlled remediation attempts and explicit unresolved evidence |
| FR31 | Self-development safety | Manage own source like any project without altering active running controllers, policies, credentials or deployment without human-authorized promotion |
| FR32 | Entry-point flexibility | Accept authorized dashboard, repository and optional message-triggered tasks without losing original conversational context |

## Work packages

| ID | Work package | Dependencies | Deliverable |
| --- | --- | --- | --- |
| FCT001 | Reuse inventory and architecture baseline | None | Inventory, decision register and working repository |
| FCT002 | Runtime and authentication qualification | FCT001 | M0 report, real/fake capability distinction and environment isolation evidence |
| FCT003 | Schemas and project-policy contracts | FCT001 | Valid/invalid examples and typed contract use |
| FCT004 | Factory persistence and durable command dispatch | FCT003 | Transaction/outbox/inbox recovery tests |
| FCT005 | Workspace supervisor and constrained execution | FCT002, FCT003 | Ownership, sandbox and cancellation qualification |
| FCT006 | Lifecycle owner and context packets | FCT004 | Requirement/decision recovery and grounded answers |
| FCT007 | Phase controller and gate evaluator | FCT003–FCT006 | Missing/false/stale evidence cannot pass |
| FCT008 | Conversational task dashboard | FCT004, FCT006 | Browser discussion, status and decision journey |
| FCT009 | Expert advice and human resolution | FCT006–FCT008 | Scoped advice and correct authority routing |
| FCT010 | Waiting queue and no-progress recovery | FCT005, FCT007, FCT009 | Two tasks interleave safely; no idle model loop |
| FCT011 | GitHub intake and Draft publisher | FCT004, FCT005, FCT007 | Deduplication and lost-response reconciliation |
| FCT012 | Generic project policy and CI adapters | FCT007, FCT011 | Separate sample policies and real source-bound CI receipts |
| FCT013 | Usage budgets and funding adapter | FCT002–FCT004 | Quota waits, reservations and no silent fallback |
| FCT014 | Deployment, backup and operator runbooks | FCT005, FCT008, FCT010 | Private deployment and isolated restore drill |
| FCT015 | Optional Slack transport | FCT008, FCT009, configured destination | Signed/scoped callbacks and duplicate-safe notices |
| FCT016 | Five-task evaluation and reflection | FCT010–FCT014 | Honest comparison and one reviewed improvement proposal |
| FCT017 | Isolated environment provider and lease lifecycle | FCT003, FCT005 | Container/task isolation, stale lease fencing, safe cleanup, self-source boundary |
| FCT018 | Environment templates, caching and warm-pool investigation | FCT017 | Measured cold/warm starts, sanitization tests, resource/cost decision |
| FCT019 | Blueprint nodes, early deterministic checks and curated tools | FCT007, FCT017 | Local lint/test gates, bounded CI retries and policy-scoped tool registration |
| FCT020 | Self-development and promotion policy | FCT011, FCT012, FCT017 | Factory-source Draft PR, isolated review and explicit manual promotion/rollback evidence |

## Acceptance tests

| ID | Case | Acceptance |
| --- | --- | --- |
| AT01 | Task starts in conversation without issue | Goal, criteria and owner persist; issue linkage is optional |
| AT02 | Owner restarts | Correct phase, decisions, questions and next action reconstructed |
| AT03 | Fresh phase identity | Planner, implementer and reviewer use distinct recorded sessions |
| AT04 | Structured handoff | Invalid assignment rejected; SDK yield registers new questions once without MCP |
| AT05 | False completion claim | Model claims tests passed without valid receipts; gate rejects |
| AT06 | Stale or foreign evidence | Wrong task, revision, profile or hash cannot satisfy the gate |
| AT07 | Required check fails | No next-phase admission; specific finding and bounded action recorded |
| AT08 | Known question | Implementation/test conflict resolved from source, acceptance examples and an applicable decision. Owner cites the decision and proceeds without operator input |
| AT09 | Expert question within authority | Scoped advice visible; routine resolution resumes automatically |
| AT10 | Reserved decision | Material change investigated with expert advice. Options, consequences and recommendation persist until an explicit authorized human decision. Reuse an applicable recorded decision |
| AT11 | Decision/wait race | Decision in the read/register gap with lost wake still resumes once through reconciliation |
| AT12 | Conflicting task update | Metadata conflicts do not restart valid work; material scope revocation rejects stale results |
| AT13 | Duplicate dispatch | Replayed command starts no second writer or phase attempt |
| AT14 | Waiting releases capacity | Task B runs while task A waits; A resumes in correct context |
| AT15 | Worker/controller interruption | State and checkpoint retained; safe continuation or precise blocker |
| AT16 | Cancellation and stale lease | Owned children stop or workspace quarantined; stale results rejected |
| AT17 | Lost publication response | Existing intended PR reconciled; no duplicate PR or branch |
| AT18 | Intake and duplicate/out-of-order events | Eligible bug admitted once; held, unauthorized, covered or materially unclear work is deferred with reason |
| AT19 | Quota/auth/budget failure | Durable wait, no paid fallback, no busy model retry loop |
| AT20 | Independent review | Writer session or renamed identity cannot satisfy review |
| AT21 | Scope and source update | Material scope change escalates; unrelated source advance preserves applicable decisions |
| AT22 | Repair limit | Cumulative substantive count retained; no unauthorized retry beyond configured cumulative repair limit |
| AT23 | Project verification | Two distinct project profiles enforce their own trusted rules; candidate policy substitution rejected |
| AT24 | Permission isolation | Read-only roles cannot edit source; workers cannot access control secrets or publish arbitrary refs |
| AT25 | Backup restore | Tasks/decisions/artifacts usable after restore; dispatch paused until reconciliation |
| AT26 | Phone/browser operation | Discuss, decide, inspect, cancel and resume without CLI; reconnect loses no durable data |
| AT27 | Metrics and export | Evidence package exposes outcomes, waits, interventions and known/unknown costs |
| AT28 | Reflection | Suggestion cites observed events and is proposed, not self-applied |
| AT29 | Isolated environments | Two concurrent tasks cannot read each other's repositories, runtime data, toolchain changes, processes or credentials |
| AT30 | Environment lease and cleanup | Lost worker/quarantine/expired lease cannot be reused unsafely; owned cleanup completes or is reported as blocked |
| AT31 | Warm-pool sanitization | A pre-warmed environment contains no previous task data/secrets; startup metrics justify warming before enabling a standby pool |
| AT32 | Cold recovery and snapshot fidelity | Candidate snapshot is restored into a fresh environment and produces the same source fingerprint and declared environment profile |
| AT33 | Blueprint enforcement | Worker prompt asserting checks passed cannot substitute for mandatory checkout/lint/test/gate deterministic steps |
| AT34 | Curated tools and context hydration | Scoped role sees only allowed tools; relevant source links are fetched with provenance; untrusted text cannot grant authority |
| AT35 | Self-development | Factory-code change from a sandbox cannot alter live controller, DB, policies or release; candidate requires human-approved promotion |
| AT36 | Untrusted repository execution | Build/test code cannot read host secrets, Docker socket or privileged integration credentials; denied egress is enforced |
