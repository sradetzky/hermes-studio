# Runtime event retention

## Decision

Hermes Studio intentionally retains every `job_events` row for as long as its
owning job remains in the runtime database. There is no independent age- or
count-based pruning in the preview release.

This is a deliberate durable-retention policy, not an omitted cleanup task:

- activity rows are the exact per-profile reasoning, commentary, tool, and
  lifecycle record shown by the web UI;
- failed generation and specialist jobs need that record for diagnosis;
- current measured growth is small enough that pruning would add migration,
  cursor, and concurrency risk without solving an observed operational problem;
- project media and chat JSONL exports remain the durable creative source of
  truth, but they do not replace the runtime activity audit.

Deleting a job, if a supported job-deletion operation is added later, may rely on
the existing `ON DELETE CASCADE` relationship to delete its events in the same
transaction. Code must not delete activity independently while retaining a job.

## Measurement — 2026-08-25

The live preview database was opened read-only while the service was running.
Only aggregate lengths and counts were queried; no chat, reasoning, tool, or
prompt content was printed.

| Measure | Observed |
|---|---:|
| Completed or failed jobs | 16 |
| `job_events` rows | 1,348 |
| Projects represented | 2 |
| Event rows per project | 421 / 927 |
| Event rows per finished job, mean | 84.25 |
| Event rows per finished job, median | 79 |
| Event rows per finished job, observed p95 estimate | 161 |
| Event rows per finished job, maximum | 285 |
| Summary + detail characters | 307,381 |
| Largest summary + detail payload | 4,599 characters |
| Database file | 843,776 bytes |
| `job_events` table | 536,576 bytes |
| `job_events` indexes | 135,168 bytes |
| Event table + indexes / database | 79.6% |

Reasoning plus tool start/completion rows accounted for 89.5% of event payload
characters. At the observed physical density, event table and index storage is
about 41,984 bytes per finished job, or approximately 40.0 MiB per 1,000 jobs.
This is a directional projection, not a storage guarantee: SQLite page reuse,
message length, and job behavior will change the actual result.

The sample included eight completed chat jobs, two failed chat jobs, two
completed generation jobs, and four failed generation jobs. Completed chat and
generation jobs averaged 117.0 and 106.5 events respectively, so the measurement
includes the high-activity paths rather than only lifecycle-only failures.

## Revisit triggers

Re-measure and design bounded retention before any of these become true:

- `.runtime/studio.db` exceeds 100 MiB;
- `job_events` exceeds 100,000 rows;
- one project's initial activity response exceeds 10,000 rows or causes measured
  browser/API latency;
- a supported job-deletion or runtime-backup policy is introduced;
- activity details begin storing materially larger payloads.

Crossing a trigger does not authorize ad-hoc deletion. A bounded policy must be a
schema-owned, transaction-safe change that:

1. never prunes queued or running jobs;
2. preserves job lifecycle and failure evidence;
3. keeps cursor semantics monotonic for concurrent readers;
4. does not affect chat JSONL exports or project media;
5. includes migration, rollback, and concurrent-reader tests.
