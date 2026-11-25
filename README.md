# AI Coding Factory

AI Coding Factory runs repository tasks through a persistent lifecycle owner, scoped implementation workers, independent verification and fresh review. The owner retains requirements and decisions across phases. A reviewed candidate can be submitted as a Draft PR for a human to consider.

The [project requirements and acceptance tests](docs/project-plan.md) define the scope of the factory.

## Run the local task desk

On Windows, install the .NET 10 SDK, Node.js 22 with npm, Python 3 and PowerShell. In PowerShell from the repository root, run the following command.

```powershell
.\scripts\Start-Factory.ps1
```

Open `http://127.0.0.1:5187` and use the local token printed by the controller. Choose **Resolve a boundary conflict** to follow a controlled task through implementation, verification and repair. Ctrl+C stops the controller.

## Verify the source

Run `.\scripts\Test-Factory.ps1` for the local regression suite. Docker is needed for container-backed verification. The optional browser and Hatchet suites have their own switches in the test script.

The public fixture history contains a synthetic task export and pinned source inputs for local self-development exercises. The renderer case file under `docs/` is sample data. To run the historical readiness audit with fresh measurements, first run `python scripts/benchmark_readiness.py --output docs/fct018-readiness.json` on a prepared Docker host. Production sign-in, private deployment records and account usage belong to the deployment operator.

## Components

| Location | Responsibility |
| --- | --- |
| `src/Factory.Api` and `src/Factory.Web` | Task ownership, decisions, gates, publication journal and operator desk |
| `src/Factory.Dispatch` | Hatchet dispatch and worker claims |
| `src/Factory.Runtime` and `scripts` | Codex sessions, workspace leases and verification |
| `fixtures` and `project-profiles` | Approved project scope, pinned source and test definitions |

GitHub intake requires an explicitly approved project and issue. The included factory self-development profiles use loopback GitHub fixtures. Hosted self-development requires separate admission and qualification. Merge and deployment are operator actions.

### Connect a hosted factory project

Install a private GitHub App on the selected repository. Grant Contents and Pull requests read/write, Issues read-only, and the default Metadata read access. New hosted projects use a deployment-installed project policy and a separate intake file with matching repository identity, branch and issue scope. GitHub permissions apply to the repository. The controller enforces the task branch, verified candidate and Draft-only publication.

In the hosted project's task desk, choose **Start GitHub issue**, select the approved repository and issue, then choose **Begin**. The controller checks current issue eligibility and the pinned source before handing the task to the lifecycle owner and Hatchet. Opening the desk or syncing an unstarted issue leaves it idle. Repeating Begin opens the same task. After admission the controller polls the issue and reconciles delivery, so webhooks can stay disabled. Loopback qualification projects can select the same operator workflow with `"operatorStart": true` in the intake file.

With the dashboard, controller and dispatch bridge built, run `python tests/qualify_github_start.py` to exercise Begin in Chromium against a disposable Git repository, PostgreSQL and Hatchet. It stops after confirming the planning run in Hatchet.

Set `FACTORY_GITHUB_APP_CLIENT_ID` and `FACTORY_GITHUB_APP_INSTALLATION_ID` for the trusted controller. With systemd, provide the signing key through `LoadCredential=github-app-key.pem:/absolute/protected/path/to/key.pem`. An absolute `FACTORY_GITHUB_APP_KEY_FILE` is also supported. Store the key outside the checkout and task environments. The controller needs outbound HTTPS to `api.github.com`.

On demand, the controller signs a short-lived App JWT, requests a one-repository installation token and checks the returned permissions and visible repository ID. It renews before expiry and mints a fresh token after restart. A rejected repository write returns to publication reconciliation. Initial anonymous reads of a public repository are supported while the installation is being prepared. [GitHub installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)

### Follow up or finish a delivered issue

Issue actions are an opt-in setting for each repository. To enable them, the installation administrator grants **Issues read/write** for the selected repository. Set `issueActionBot` in its intake JSON to the installed GitHub App's bot login, including `[bot]`. The controller checks the installation token permissions. Omitting the setting keeps issue access read-only.

Configure `FACTORY_FOLLOWUP_APPROVALS` with an absolute path to a persistent directory. Give the controller write access. Give the trusted dispatch and CI adapters read access to the same files. Keep the directory outside the checkout and model workspaces. Back up the directory together with the controller database. Restore the matching approvals before resuming a follow-up.

* Choose **Request follow-up** under **Repository delivery**. Enter the revised request or choose **Use last desk message**. Check the approved file and exact base commit. **Approve scope and start follow-up** posts the request to the issue and starts planning on the same task. A desk message or GitHub comment alone does not start work. The installed project still controls files, tools, required tests and review. A merged delivery starts from the displayed base. An open delivery can carry its verified changes forward while its original base is current. If the base has moved, the desk shows that the new round starts from the selected base. Each round has a separate Draft PR and retains links to earlier deliveries.
* After merging the accepted PR, choose **Finish issue**. Review the public summary and the exact branch names, PR numbers and revisions. **Post summary and close issue** records all delivered rounds and closes the issue as completed. Cleanup covers merged task branches and merged PR branches that GitHub links to this issue in the same repository, including branches created by people. The factory keeps a branch if its head changed, it is protected, it is the default or project base branch, or an open PR uses it. The activity history records each branch outcome.
* If GitHub interrupts an action, choose **Reconcile follow-up** or **Reconcile finish**. The controller checks for the original bot comment or closure before continuing. If the base changed during admission, withdraw the pending follow-up and approve the new base. A comment that was already published stays on the issue. An uncertain write with no authoritative receipt needs operator inspection.
