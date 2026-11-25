The release rehearsal uses this directory as its candidate destination.

## Save a task usage report

Run the Linux worker launcher with Node 22 on its PATH and point
`FACTORY_LEASE_DB` at the existing deployment lease journal.

```sh
FACTORY_LEASE_DB=/path/to/lease.db scripts/linux-worker.sh usage TASK_UUID > task-usage.md
```

Replace `TASK_UUID` with the task's UUID. The command opens the journal
read-only and reports subscription attempts for that task, grouped by
phase. Input, cached input, and output tokens are summed separately.
A partial subtotal shows the sum of known counts and how many attempts
have an unknown count for that metric.

The current journal has no executed model or task-attributable cost
measurements, so those fields are marked `unknown`. The token counts
describe subscription usage. API billing is outside this report.
