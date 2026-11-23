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
