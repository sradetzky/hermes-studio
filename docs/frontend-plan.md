# Frontend Plan — Studio Web UI

Phase 3 of PLAN.md. Principles: thin read-mostly window onto the filesystem;
no state in the UI that isn't already on disk; minimal dependencies.

## Stack (locked)

- **FastAPI** backend (`.venv/`; pinned in `requirements.txt`, including
  python-multipart for reference uploads)
- **Single HTML page**, vanilla JS + Tailwind via CDN — no SPA framework
- Media served by mounting `studio-root/` and `~/ComfyUI/output` read-only
- Chat jobs resume one persistent Hermes `studio` session per project and run
  asynchronously behind durable `.runtime/jobs/*.json` records

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
and starts a background worker. The worker serializes by project and spawns
`hermes -p studio [-r SESSION] chat -Q -t all -q "<msg>"`. Session ids live
under `.runtime/sessions/`; job state lives under `.runtime/jobs/`. Startup
marks orphaned queued/running jobs failed rather than leaving them stuck.

## Safety / scope guards

- Backend writes only through project creation, atomic chat append, and the
  validated reference-upload endpoint. Generation stays agent-side.
- Uploads are restricted to image/video/audio extensions, 20 files/request,
  256MB/file; path components are rejected and name collisions never overwrite
- Exact project ids only; the core resolver rejects separators, symlink
  escapes, fuzzy suffix matching and paths outside `projects/`
- No auth v1 (localhost bind only)

## Milestones

1. M1 (done): backend read APIs + media mounts + static index.html
2. M2 (done): create-project + prompt viewer + drag/drop references
3. M3 (done): persistent asynchronous chat round-trip + activity status
4. M4: media review — detail viewer, promote/use-as-ref, generation filter

## Out of scope (v1)

Auth/multi-user, editing files from UI, workflow editors, timelines,
model management (use scripts/switch-model.sh).
