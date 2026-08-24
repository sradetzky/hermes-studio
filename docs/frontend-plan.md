# Frontend Plan — Studio Web UI

Phase 3 of PLAN.md. Principles: thin read-mostly window onto the filesystem;
no state in the UI that isn't already on disk; minimal dependencies.

## Stack (locked)

- **FastAPI** backend (`.venv/`; production/dev dependencies pinned separately)
- **Vanilla ES module + locally compiled Tailwind CSS** — no runtime CDN and
  no SPA framework
- Media served only through guarded shared references/final routes and exact
  project/clip/take routes
- Chat jobs resume an explicit project-level or clip-level Hermes session per
  profile and run asynchronously behind a transactional SQLite runtime store

## Backend ownership

- `app.py` — inert app factory + lifespan wiring
- `job_store.py` — typed SQLite jobs, profile sessions, chat and activity events
- `hermes_events.py` — exact job-source correlation plus baseline-guarded read-only
  projection of structured Hermes session rows into safe per-job activity
- `studio_manager.py` — FIFO scheduler, worker lease, parent-death-supervised
  Hermes process, exact job-token orphan recovery before lease release, and
  scheduler-coupled liveness with top-level execution fault containment
- `job_store.py` / `runtime_schema.py` — transactional scoped job/chat/event
  state and ordered SQLite migrations; clip work has a database-enforced exact
  clip id while project chat has an explicit project scope
- `reference_store.py` — synchronous staging + atomic no-overwrite publication
- `clip_store.py` — canonical project title/brief publication, exact clip
  resolution, ordering, enabled state, and selected-take provenance
- `media_review_store.py` — guarded generation detail, idempotent promotion and
  generation-to-reference publication with filesystem provenance
- `generation_settings_store.py` — typed `current_generation.json`, strict H3
  knob validation, prompt-hash staleness, and prompt-derived length/references
- `comfy_queue.py` — read-only sanitized ComfyUI running/pending projection plus
  native completed-job timing; only allowlisted render metadata crosses the
  backend boundary and workflow payloads remain private
- `routes.py` — thin HTTP boundary and guarded media serving
- `run.sh` / `stop.sh` / `status.sh` — single-instance lock, graceful stop,
  stale-PID cleanup and process status

## Layout (single page)

Wide screens use the desktop three-column workspace below. At 1099px and below,
an explicit Projects / Chat / Media selector presents one full-width pane at a
time, with Chat as the default workspace. Switching panes preserves the active
project, clip, chat scope, job activity, open dialogs, and media element identity
instead of rebuilding the DOM or resetting playback.

```
┌──────────┬──────────────────────────────┬────────────┐
│ PROJECTS │  CLIP CHAT / PROJECT CHAT    │ MEDIA      │
│ + clips  │  independent scoped sessions │ references │
│ + new    │  explicit visible scope      │ takes      │
│          │  ACTIVE CLIP PROMPT panel    │ video/img  │
│          │  (clip/current_prompt.txt)   │ players    │
└──────────┴──────────────────────────────┴────────────┘
```

- Project + ordered clip switcher = left rail; editable project title/brief plus
  clip add/rename/reorder/enable controls
- Center: explicit Clip/Project chat selector, scoped chat with the studio
  agent, and below it the current structured prompt. Clip chat is the default
  after selecting a clip; Project chat owns cross-clip direction and history.
- Prompt panel: readiness badge and compact H3 run contract (mode, MP or
  explicit canvas, seed, steps, and fused-modulation/ChunkFF acceleration);
  clip length and ordered references come from the prompt itself
- Right: shared reference thumbnails, active-clip take gallery (newest first), HTML5 video
  player for clips, media/recipe/review filters, and a keyboard-accessible detail
  dialog with every archived asset, prompt, metadata, review action, and confirmed
  whole-take deletion
- Header: compact global ComfyUI queue summary with an expandable ordered view
  of sanitized recipe/mode, canvas, approximate media length, frames, steps,
  accel, seed, elapsed/waiting time, prompt IDs, and the last exact completed
  duration
- Narrow workspace selector: pointer and Left/Right/Home/End keyboard navigation,
  accurate pressed/hidden/inert state, touch-sized controls, and automatic return
  to Chat after selecting a project or clip
- Prompt & generation uses one collapsible panel: open by default on desktop,
  closed by default on narrow page loads, and capped at 28% of the narrow Chat
  pane when expanded so the transcript stays larger. Its header is 24px high;
  the panel body and bounded long-prompt view scroll independently.
- On phones, the ComfyUI queue popover anchors to both header edges and uses a
  viewport-relative maximum height instead of overflowing from the status label.
- Polling every 2s runs project navigation, scoped chat/jobs/activity,
  references, and clip/generation requests as independently failing planes.
  Project, clip, and chat-scope revision tokens reject stale responses; media
  DOM rebuilds only when listing signatures change,
  so active video playback is stable.

## API surface (v1)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects` | list projects |
| GET | `/api/profiles` | dispatchable Studio profiles |
| GET | `/api/comfyui/queue` | sanitized global queue details and recent completion timing |
| POST | `/api/projects` | create project {name, brief} |
| GET/PATCH | `/api/project/{id}` | title, brief, ordered clip manifest / update editable metadata |
| GET/POST | `/api/project/{id}/clips` | list/create clips |
| GET/PATCH | `/api/project/{id}/clips/{clip}` | clip metadata, prompt, readiness / update title or enabled state |
| PUT | `/api/project/{id}/clips/order` | atomically replace exact clip order |
| GET/PUT | `/api/project/{id}/clips/{clip}/generation-settings` | inspect/save the clip's prompt-bound run contract |
| GET | `/api/project/{id}/clips/{clip}/generations` | active clip take listing w/ metadata |
| GET | `/api/project/{id}/clips/{clip}/generations/{gen}` | complete take media/prompt/meta/review detail |
| DELETE | `/api/project/{id}/clips/{clip}/generations/{gen}` | delete one exact archived take and clear its selection |
| PUT | `/api/project/{id}/clips/{clip}/selected-take` | select/clear one exact video take |
| POST | `/api/project/{id}/clips/{clip}/generations/{gen}/promote` | copy selected media to shared `final/` |
| POST | `/api/project/{id}/clips/{clip}/generations/{gen}/use-as-reference` | copy selected media to shared `references/` |
| GET | `/api/project/{id}/movie` | ordered enabled-clip assembly readiness and completed project movies |
| GET/POST | `/api/project/{id}/chat?after=N` | project-level chat/session for cross-clip direction |
| GET/POST | `/api/project/{id}/clips/{clip}/chat?after=N` | independent exact-clip chat/session |
| GET | `/api/jobs/{id}` | one job's queued/running/completed/failed state |
| GET | `/api/project/{id}/jobs` | recent project activity |
| GET | `/api/project/{id}/events?after=N` | project-chat profile/tool/reasoning activity |
| GET | `/api/project/{id}/clips/{clip}/events?after=N` | exact-clip activity |
| GET | `/api/project/{id}/references` | list current project references |
| POST | `/api/project/{id}/references` | multi-file drag/drop upload |
| GET | `/media/projects/{id}/{area}/{path}` | guarded shared references/final media |
| GET | `/media/projects/{id}/clips/{clip}/generations/{gen}/{file}` | guarded exact take media |

## Chat → Hermes wiring

The project and nested clip chat endpoints accept an explicit allowlisted
profile, reject a second active project job, return HTTP 202 immediately, and
atomically insert the user turn, scope-bound job, and queued activity event.
Clip chat additionally validates the exact clip.
A lifespan-owned scheduler atomically claims
the oldest global job and spawns
`hermes -p PROFILE [-r SESSION] chat -Q -t TOOLSETS --source studio-web -q "<msg>"`.
The command carries the exact project/chat scope and, for clip work, exact clip
ID and path in its query and environment. Runtime sessions are keyed by
project + scope + profile, so one clip never resumes another clip's conversation.
Schema-v4 migration keeps every existing transcript, job, activity row, and
Hermes session in Project history; it does not guess or duplicate clip history.
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
session for the parent project or exact clip scope and projects its activity
into the parent web job. The
specialists receive fixed minimal toolsets and remain prohibited from queueing
GPU work. Dispatch requires an explicit profile selection or `/handoff <role>`
command; ordinary language is never auto-routed, specialists are not
auto-chained, and their result never starts a render without a separate request.

## Safety / scope guards

- Backend writes only through project creation, transactional scoped chat
  events plus derived project/clip `chat.jsonl` exports, validated reference
  uploads, and explicit M4
  promote/use-as-reference/delete actions. Generation stays agent-side.
- Queue observability is a read-only native ComfyUI exception to MCP-only control.
  It reads `GET /queue` plus, only while expanded, completed timing from
  `GET /api/jobs`, but returns
  only allowlisted render metadata, prompt IDs, and order—never workflow payloads,
  prompt/reference/model values, or mutation controls. Phase 1 deliberately does
  not patch or extend ComfyUI and does not claim whole-generation percentage/ETA.
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
- Take deletion requires an explicit irreversible browser confirmation, rejects
  symlinked/unsafe generation directories and active project jobs, quarantines and
  removes only the verified directory identity, and clears selected-take provenance.
  Existing shared `final/` and `references/` copies are deliberately preserved.
- Project metadata updates require a validated 1–120 character display title and
  bounded Markdown brief, reject active project jobs and unknown fields (including
  attempts to change the ID), and serialize readers/writers through the project
  lock. The brief publishes descriptor-safely before the manifest title; a failed
  manifest publication restores the exact previous brief before returning failure.
- Generation settings enforce 0.1–1.1 MP or a ≤1.1MP explicit 32px-grid canvas
  and 1–50 steps. Readiness parses a 4–15 second length and ordered image-only
  `<Picture N> (filename.ext)` references from the prompt, then validates files
  and mode-specific counts. Seeds are limited to JavaScript's exact integer
  range (`0..9007199254740991`), round-trip as decimal strings in the API, and
  remain integers on disk. Any prompt edit invalidates readiness until
  settings are deliberately re-saved against the new hash.
- **Generate with this prompt** is enabled only for an enabled clip with a ready
  prompt-bound manifest and no active project job. The request carries the exact
  prompt hash and settings revision, is rejected if stale at the API boundary,
  and is expanded into an immutable prompt/settings/execution/archive contract in
  the dedicated SQLite job. Worker start revalidates current state against that
  snapshot; archival reads the snapshot back from the exact running job, and a
  zero-exit agent cannot complete without one matching artifact and prompt ID.
  Authoritative metadata follows the exact executed `SaveVideo` producer branch;
  disconnected decoys and ambiguous output producers fail closed.
- Project metadata, clip creation/order/title/enabled state, generation settings,
  and take deletion share a project job-coordination lock with enqueue. Mutations
  return `409` once a project job exists and cannot pass a check/enqueue TOCTOU gap.
- Selected-take media validation and manifest publication run under the same
  project lock, so a concurrent take deletion cannot publish a dangling selection.
- Promote/reference actions record descriptor-derived SHA-256 identities for the
  archive source and published target. Retries reuse a target only while both
  still match; changed content is republished without overwriting the old copy.
- Project metadata and take detail dialogs own revisioned request contexts.
  Navigation, close/reopen, and same-clip take changes invalidate older loads,
  saves, and actions before they can mutate the current dialog or busy state.
- Chat submissions include a monotonic request revision in their immutable scope;
  stale success/failure cannot update another conversation. ComfyUI queue polling
  is latest-request-wins, preventing slower old snapshots from replacing new state.
- Take detail rendering reuses the active media node while kind and URL are stable,
  preserving playback through select/promote/reference refreshes. A raw-CDP
  Chromium regression now forces close/reopen and out-of-order dialog, chat, and
  queue completion and asserts media-node identity through a review action.
- Project movie readiness follows manifest order, ignores disabled clips, and
  identifies every enabled clip whose selected video is absent or unsafe.
- No auth v1; Uvicorn remains loopback-only, while optional tailnet access relies
  on Tailscale identity/ACLs plus the exact-host and same-origin guards above.

## Milestones

- M1 (done): backend read APIs + media mounts + static index.html
- M2 (done): create-project + prompt viewer + drag/drop references
- M3 (done): persistent asynchronous chat round-trip + activity status
- M3.5 (done): live per-profile reasoning/tool timeline + specialist dispatch
- M4 (done): media detail viewer, filters, promote-to-final, use-as-reference,
  and safe confirmed take deletion
- M4.1 (done): typed generation manifest, readiness summary and settings editor
- M4.2 (done): clip-local prompts/settings/takes, exact clip jobs and nested media,
  ordered clip controls, selected-take provenance, and clip-safe polling
- M4.3 (done): revision-guarded **Generate with this prompt** action, dedicated
  generation jobs, worker-start revalidation, and queued/active browser feedback
- M4.4 (done): explicit Project/Clip chat selector, independently persisted
  transcripts/activity/profile sessions, and lossless project-history migration
- Real E2E checkpoint (done): exact-clip web job → Studio → comfyui-mcp H3
  submission → parameter read-back → clip-local archive → VRAM cleanup
- M4.5 (done): editable project display title and brief with an immutable
  filesystem project ID, validated serialized publication, API/storage coverage,
  and real-browser read-back
- M4.6 (done): desktop three-pane preservation plus responsive Projects / Chat /
  Media navigation, compact header/composer/dialog layouts, keyboard and inert
  state, and playback-preserving real-browser checks at desktop, tablet, and phone
  viewports
- Preview.2 release gate (done): synchronized public scope, complete local
  correctness/security/dependency/profile-drift gates, real Chromium desktop and
  narrow checks, and a clean extracted-archive verification
- M4.7 (done): closed the fresh-eyes process/session/generation ownership,
  authoritative graph traversal, filesystem transaction, stale-response, stable
  media DOM, and behavioral-browser coverage findings before adding more features
- M5 (current): project-level **Export selected takes as movie** readiness and one
  explicit asynchronous hard-cut MP4 assembly from enabled clips in manifest
  order, with versioned `final/` publication, exact clip/take provenance, and
  Media playback/download. No trimming, transitions, timeline, or take comparison.

## Out of scope (v1)

Auth/multi-user, arbitrary file editing from the UI, workflow editors, timelines,
model management (use scripts/switch-model.sh).
