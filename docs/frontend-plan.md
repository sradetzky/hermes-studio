# Frontend Plan — Studio Web UI

Phase 3 of PLAN.md. Principles: thin read-mostly window onto the filesystem;
no state in the UI that isn't already on disk; minimal dependencies.

## Stack (locked)

- **FastAPI** backend (`.venv/`; production/dev dependencies pinned separately)
- **Vanilla ES module + locally compiled Tailwind CSS** — no runtime CDN and
  no SPA framework
- Media served only through guarded shared references/final routes and exact
  project/clip/take routes
- Chat jobs resume one persistent Hermes session per project/profile and run
  asynchronously behind a transactional SQLite runtime store

## Backend ownership

- `app.py` — inert app factory + lifespan wiring
- `job_store.py` — typed SQLite jobs, profile sessions, chat and activity events
- `hermes_events.py` — read-only projection of structured Hermes session rows
  into safe per-job reasoning/tool activity
- `studio_manager.py` — FIFO scheduler, worker lease, tracked Hermes process
- `job_store.py` / `runtime_schema.py` — transactional job/chat/event state and
  ordered SQLite migrations; every active job has a database-enforced clip id
- `reference_store.py` — synchronous staging + atomic no-overwrite publication
- `clip_store.py` — canonical project manifest, exact clip resolution, ordering,
  enabled state, and selected-take provenance
- `media_review_store.py` — guarded generation detail, idempotent promotion and
  generation-to-reference publication with filesystem provenance
- `generation_settings_store.py` — typed `current_generation.json`, strict H3
  knob validation, prompt-hash staleness, and prompt-derived length/references
- `comfy_queue.py` — read-only sanitized ComfyUI running/pending projection;
  workflow payloads never cross the backend boundary
- `routes.py` — thin HTTP boundary and guarded media serving
- `run.sh` / `stop.sh` / `status.sh` — single-instance lock, graceful stop,
  stale-PID cleanup and process status

## Layout (single page, three columns)

```
┌──────────┬──────────────────────────────┬────────────┐
│ PROJECTS │  CHAT (with studio agent)    │ MEDIA      │
│ + clips  │  persistent project session  │ references │
│ + new    │  exact active clip context   │ takes      │
│          │  ACTIVE CLIP PROMPT panel    │ video/img  │
│          │  (clip/current_prompt.txt)   │ players    │
└──────────┴──────────────────────────────┴────────────┘
```

- Project + ordered clip switcher = left rail; add/rename/reorder/enable controls
- Center: chat with the studio agent; below it the current structured prompt
- Prompt panel: readiness badge and compact H3 run contract (mode, MP or
  explicit canvas, seed, steps, and fused-modulation/ChunkFF acceleration);
  clip length and ordered references come from the prompt itself
- Right: shared reference thumbnails, active-clip take gallery (newest first), HTML5 video
  player for clips, media/recipe/review filters, and a keyboard-accessible detail
  dialog with every archived asset, prompt, metadata and review action
- Header: global ComfyUI queue summary with an expandable ordered prompt-id view
- Polling every 2s runs project navigation, chat/jobs/activity, references, and
  clip/generation requests as independently failing planes. Project and clip revision
  tokens reject stale responses; media DOM rebuilds only when listing signatures change,
  so active video playback is stable.

## API surface (v1)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects` | list projects |
| GET | `/api/profiles` | dispatchable Studio profiles |
| GET | `/api/comfyui/queue` | sanitized global running/pending ComfyUI queue |
| POST | `/api/projects` | create project {name, brief} |
| GET | `/api/project/{id}` | brief, chat count, ordered clip manifest |
| GET/POST | `/api/project/{id}/clips` | list/create clips |
| GET/PATCH | `/api/project/{id}/clips/{clip}` | clip metadata, prompt, readiness / update title or enabled state |
| PUT | `/api/project/{id}/clips/order` | atomically replace exact clip order |
| GET/PUT | `/api/project/{id}/clips/{clip}/generation-settings` | inspect/save the clip's prompt-bound run contract |
| GET | `/api/project/{id}/clips/{clip}/generations` | active clip take listing w/ metadata |
| GET | `/api/project/{id}/clips/{clip}/generations/{gen}` | complete take media/prompt/meta/review detail |
| PUT | `/api/project/{id}/clips/{clip}/selected-take` | select/clear one exact video take |
| POST | `/api/project/{id}/clips/{clip}/generations/{gen}/promote` | copy selected media to shared `final/` |
| POST | `/api/project/{id}/clips/{clip}/generations/{gen}/use-as-reference` | copy selected media to shared `references/` |
| GET | `/api/project/{id}/chat?after=N` | poll new chat lines |
| POST | `/api/project/{id}/clips/{clip}/chat` | queue exact-clip Studio work → HTTP 202 + job record |
| GET | `/api/jobs/{id}` | one job's queued/running/completed/failed state |
| GET | `/api/project/{id}/jobs` | recent project activity |
| GET | `/api/project/{id}/events?after=N` | incremental profile/tool/reasoning activity |
| GET | `/api/project/{id}/references` | list current project references |
| POST | `/api/project/{id}/references` | multi-file drag/drop upload |
| GET | `/media/projects/{id}/{area}/{path}` | guarded shared references/final media |
| GET | `/media/projects/{id}/clips/{clip}/generations/{gen}/{file}` | guarded exact take media |

## Chat → Hermes wiring

The nested clip chat endpoint accepts an explicit allowlisted profile, validates
the exact clip, rejects a second active project job, returns HTTP 202 immediately,
and atomically inserts the user turn, clip-scoped job and queued activity event.
A lifespan-owned scheduler atomically claims
the oldest global job and spawns
`hermes -p PROFILE [-r SESSION] chat -Q -t TOOLSETS --source studio-web -q "<msg>"`.
The command carries exact project/clip IDs and paths in its query and environment.
The orchestrator receives `all`; specialists receive fixed minimal toolsets.
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
- Queue observability is a read-only `GET /queue` exception to MCP-only control;
  responses expose prompt IDs and order only, never workflow payloads or controls.
- Uvicorn stays loopback-only. Optional remote access uses Tailscale Serve HTTPS
  on standard alternate port 8443 and an exact `HERMES_STUDIO_TRUSTED_HOSTS`
  DNS allowlist; Funnel,
  wildcard tailnet hosts, and direct LAN binding remain out of scope.
- Uploads are restricted to image/video/audio extensions, 20 files/request,
  256MB/file; path components are rejected, batches stage before publication,
  and lock + hard-link publication makes name collisions non-overwriting
- Exact project ids only; the core resolver rejects separators, symlink
  escapes, fuzzy suffix matching and paths outside `projects/`
- `/media` exposes only shared references/final and exact nested take media—not
  briefs, chats, live prompts/settings, research, shared assets, or temporary files
- Review actions accept one exact allowlisted media filename from one exact
  project/clip/take, copy rather than move, serialize through a project lock, publish
  atomically without overwrite, and record idempotent provenance in the hidden
  generation `.review.json`. Symlink/path escapes are rejected.
- Generation settings enforce 0.1–1.1 MP or a ≤1.1MP explicit 32px-grid canvas
  and 1–50 steps. Readiness parses a 4–15 second length and ordered image-only
  `<Picture N> (filename.ext)` references from the prompt, then validates files
  and mode-specific counts. Seeds are limited to JavaScript's exact integer
  range (`0..9007199254740991`), round-trip as decimal strings in the API, and
  remain integers on disk. Any prompt edit invalidates readiness until
  settings are deliberately re-saved against the new hash.
- No auth v1 (localhost bind only)

## Milestones

- M1 (done): backend read APIs + media mounts + static index.html
- M2 (done): create-project + prompt viewer + drag/drop references
- M3 (done): persistent asynchronous chat round-trip + activity status
- M3.5 (done): live per-profile reasoning/tool timeline + specialist dispatch
- M4 (done): media detail viewer, filters, promote-to-final and use-as-reference
- M4.1 (done): typed generation manifest, readiness summary and settings editor
- M4.2 (done): clip-local prompts/settings/takes, exact clip jobs and nested media,
  ordered clip controls, selected-take provenance, and clip-safe polling
- Real E2E checkpoint (done): exact-clip web job → Studio → comfyui-mcp H3
  submission → parameter read-back → clip-local archive → VRAM cleanup

## Out of scope (v1)

Auth/multi-user, editing files from UI, workflow editors, timelines,
model management (use scripts/switch-model.sh).
