# Frontend Plan — Studio Web UI

Phase 3 of PLAN.md. Principles: thin read-mostly window onto the filesystem;
no state in the UI that isn't already on disk; minimal dependencies.

## Stack (locked)

- **FastAPI** backend (`.venv/`; production/dev dependencies pinned separately)
- **Vanilla ES module + locally compiled Tailwind CSS** — no runtime CDN and
  no SPA framework
- Media served only through guarded references/generations/final routes
- Chat jobs resume one persistent Hermes session per project/profile and run
  asynchronously behind a transactional SQLite runtime store

## Backend ownership

- `app.py` — inert app factory + lifespan wiring
- `job_store.py` — typed SQLite jobs, profile sessions, chat and activity events
- `hermes_events.py` — read-only projection of structured Hermes session rows
  into safe per-job reasoning/tool activity
- `studio_manager.py` — FIFO scheduler, worker lease, tracked Hermes process
- `reference_store.py` — synchronous staging + atomic no-overwrite publication
- `media_review_store.py` — guarded generation detail, idempotent promotion and
  generation-to-reference publication with filesystem provenance
- `generation_settings_store.py` — typed `current_generation.json`, strict H3
  knob validation, prompt-hash staleness and mode/reference readiness
- `routes.py` — thin HTTP boundary and guarded media serving
- `run.sh` / `stop.sh` / `status.sh` — single-instance lock, graceful stop,
  stale-PID cleanup and process status

## Layout (single page, three columns)

```
┌──────────┬──────────────────────────────┬────────────┐
│ PROJECTS │  CHAT (with studio agent)    │ MEDIA      │
│ list     │  persistent project session  │ references │
│ + new    │                              │ generations│
│          │  CURRENT PROMPT panel        │ video/img  │
│          │  (from current_prompt.txt)   │ players    │
└──────────┴──────────────────────────────┴────────────┘
```

- Project switcher = left rail, reads folders; new project button
- Center: chat with the studio agent; below it the current structured prompt
- Prompt panel: readiness badge and editable H3 run contract (mode, duration,
  MP or explicit canvas, steps, accel, turbo/model overrides, ordered refs,
  W4A8 and optional SeedVR2 settings)
- Right: reference thumbnails, generation gallery (newest first), HTML5 video
  player for clips, media/recipe/review filters, and a keyboard-accessible detail
  dialog with every archived asset, prompt, metadata and review action
- Polling every 2s uses incremental chat/activity cursors and only rebuilds media DOM when the
  generation/reference listing changes, so active video playback is stable

## API surface (v1)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects` | list projects |
| GET | `/api/profiles` | dispatchable Studio profiles |
| POST | `/api/projects` | create project {name, brief} |
| GET | `/api/project/{id}` | brief, chat tail, current_prompt |
| GET | `/api/project/{id}/generation-settings` | settings, readiness and installed options |
| PUT | `/api/project/{id}/generation-settings` | validate and atomically save current run contract |
| GET | `/api/project/{id}/generations` | listing w/ meta.json contents |
| GET | `/api/project/{id}/generations/{gen}` | complete media/prompt/meta/review detail |
| POST | `/api/project/{id}/generations/{gen}/promote` | copy selected media to `final/` |
| POST | `/api/project/{id}/generations/{gen}/use-as-reference` | copy selected media to `references/` |
| GET | `/api/project/{id}/chat?after=N` | poll new chat lines |
| POST | `/api/chat` | queue Studio work → HTTP 202 + job record |
| GET | `/api/jobs/{id}` | one job's queued/running/completed/failed state |
| GET | `/api/project/{id}/jobs` | recent project activity |
| GET | `/api/project/{id}/events?after=N` | incremental profile/tool/reasoning activity |
| POST | `/api/project/{id}/references` | multi-file drag/drop upload |
| GET | `/media/...` | static mount of studio-root |
| GET | `/comfy/...` | static mount of ComfyUI/output |

## Chat → Hermes wiring

POST /api/chat accepts an explicit allowlisted profile, rejects a second active
project job, returns HTTP 202 immediately, and atomically inserts the user turn,
job and queued activity event. A lifespan-owned scheduler atomically claims
the oldest global job and spawns
`hermes -p PROFILE [-r SESSION] chat -Q -t all --source studio-web -q "<msg>"`.
While the subprocess remains isolated, the manager reads that profile's
structured SQLite session messages to project model reasoning, commentary and
tool start/completion records into `job_events`; formatted terminal output is
never scraped. Session IDs are
stored transactionally with job/chat state in `.runtime/studio.db`. A global
SQLite running-job invariant keeps Studio/ComfyUI execution sequential across
multiple web workers. Lifespan shutdown tracks and terminates child processes;
worker leases prevent one live worker from recovering another worker's job,
while surviving schedulers continuously take over expired peer leases before
advancing the global queue.

The Studio wall-clock limit defaults to 10,800 seconds (override with
`HERMES_STUDIO_JOB_TIMEOUT_SECONDS`). This deliberately exceeds the H3 runner's
7,200-second render wait; the old 600-second limit could kill the agent after it
had queued a valid render and then trigger the mandatory global ComfyUI cleanup.
Only failures of the GPU-owning `studio` profile invoke that cleanup—specialist
profile failures never interrupt ComfyUI.

`design_studio.py dispatch-profile` provides serialized orchestrator handoffs to
storyboarder, prompt-engineer, reviewer and illustrator profiles. Each keeps a
per-project session and projects its activity into the parent web job. The
specialists receive fixed minimal toolsets and remain prohibited from queueing
GPU work. Dispatch requires an explicit profile selection or `/handoff <role>`
command; ordinary language is never auto-routed, specialists are not
auto-chained, and their result never starts a render without a separate request.

## Safety / scope guards

- Backend writes only through project creation, transactional chat events plus
  derived `chat.jsonl` export, validated reference uploads, and explicit M4
  promote/use-as-reference actions. Generation stays agent-side.
- Uploads are restricted to image/video/audio extensions, 20 files/request,
  256MB/file; path components are rejected, batches stage before publication,
  and lock + hard-link publication makes name collisions non-overwriting
- Exact project ids only; the core resolver rejects separators, symlink
  escapes, fuzzy suffix matching and paths outside `projects/`
- `/media` exposes only references, generations and final—not briefs, chats,
  prompts, research, shared or temporary files
- Review actions accept one exact allowlisted media filename from one exact
  generation, copy rather than move, serialize through a project lock, publish
  atomically without overwrite, and record idempotent provenance in the hidden
  generation `.review.json`. Symlink/path escapes are rejected.
- Generation settings enforce 4–15 whole seconds, 0.1–1.1 MP or a ≤1.1MP
  explicit 32px-grid canvas, 1–50 steps, safe filenames, ordered image-only
  references and mode-specific reference counts. Large integer seeds round-trip
  as decimal strings in the API and integers on disk. Any prompt edit invalidates
  readiness until settings are deliberately re-saved against the new hash.
- No auth v1 (localhost bind only)

## Milestones

- M1 (done): backend read APIs + media mounts + static index.html
- M2 (done): create-project + prompt viewer + drag/drop references
- M3 (done): persistent asynchronous chat round-trip + activity status
- M3.5 (done): live per-profile reasoning/tool timeline + specialist dispatch
- M4 (done): media detail viewer, filters, promote-to-final and use-as-reference
- M4.1 (done): typed generation manifest, readiness summary and settings editor

## Out of scope (v1)

Auth/multi-user, editing files from UI, workflow editors, timelines,
model management (use scripts/switch-model.sh).
