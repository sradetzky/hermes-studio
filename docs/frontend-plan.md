# Frontend Plan — Studio Web UI

Phase 3 of PLAN.md. Principles: thin read-mostly window onto the filesystem;
no state in the UI that isn't already on disk; minimal dependencies.

## Stack (locked)

- **FastAPI** backend (`.venv/`; production/dev dependencies pinned separately)
- **Vanilla ES module + locally compiled Tailwind CSS** — no runtime CDN and
  no SPA framework
- Media served only through guarded references/generations/final routes
- Chat jobs resume one persistent Hermes `studio` session per project and run
  asynchronously behind a transactional SQLite runtime store

## Backend ownership

- `app.py` — inert app factory + lifespan wiring
- `job_store.py` — typed SQLite jobs, sessions and chat events
- `studio_manager.py` — FIFO scheduler, worker lease, tracked Hermes process
- `reference_store.py` — synchronous staging + atomic no-overwrite publication
- `routes.py` — thin HTTP boundary and guarded media serving

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
- Right: reference thumbnails, generation gallery (newest first), HTML5 video
  player for clips
- Polling every 5s uses `chat?after=N` and only rebuilds media DOM when the
  generation/reference listing changes, so active video playback is stable

## API surface (v1)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects` | list projects |
| POST | `/api/projects` | create project {name, brief} |
| GET | `/api/project/{id}` | brief, chat tail, current_prompt |
| GET | `/api/project/{id}/generations` | listing w/ meta.json contents |
| GET | `/api/project/{id}/chat?after=N` | poll new chat lines |
| POST | `/api/chat` | queue Studio work → HTTP 202 + job record |
| GET | `/api/jobs/{id}` | one job's queued/running/completed/failed state |
| GET | `/api/project/{id}/jobs` | recent project activity |
| POST | `/api/project/{id}/references` | multi-file drag/drop upload |
| GET | `/media/...` | static mount of studio-root |
| GET | `/comfy/...` | static mount of ComfyUI/output |

## Chat → Hermes wiring

POST /api/chat rejects a second active project job, returns HTTP 202 immediately,
and inserts a queued transaction. A lifespan-owned scheduler atomically claims
the oldest global job and spawns
`hermes -p studio [-r SESSION] chat -Q -t all -q "<msg>"`. Session IDs are
stored transactionally with job/chat state in `.runtime/studio.db`. A global
SQLite running-job invariant keeps Studio/ComfyUI execution sequential across
multiple web workers. Lifespan shutdown tracks and terminates child processes;
worker leases prevent one live worker from recovering another worker's job,
while surviving schedulers continuously take over expired peer leases before
advancing the global queue.

## Safety / scope guards

- Backend writes only through project creation, transactional chat events plus
  derived `chat.jsonl` export, and the validated reference-upload endpoint.
  Generation stays agent-side.
- Uploads are restricted to image/video/audio extensions, 20 files/request,
  256MB/file; path components are rejected, batches stage before publication,
  and lock + hard-link publication makes name collisions non-overwriting
- Exact project ids only; the core resolver rejects separators, symlink
  escapes, fuzzy suffix matching and paths outside `projects/`
- `/media` exposes only references, generations and final—not briefs, chats,
  prompts, research, shared or temporary files
- No auth v1 (localhost bind only)

## Milestones

1. M1 (done): backend read APIs + media mounts + static index.html
2. M2 (done): create-project + prompt viewer + drag/drop references
3. M3 (done): persistent asynchronous chat round-trip + activity status
4. M4: media review — detail viewer, promote/use-as-ref, generation filter

## Out of scope (v1)

Auth/multi-user, editing files from UI, workflow editors, timelines,
model management (use scripts/switch-model.sh).
