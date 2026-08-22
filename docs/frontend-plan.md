# Frontend Plan — Studio Web UI

Phase 3 of PLAN.md. Principles: thin read-mostly window onto the filesystem;
no state in the UI that isn't already on disk; minimal dependencies.

## Stack (locked)

- **FastAPI** backend (`.venv/`, deps: fastapi + uvicorn only)
- **Single HTML page**, vanilla JS + Tailwind via CDN — no SPA framework
- Media served by mounting `studio-root/` and `~/ComfyUI/output` read-only
- Chat resumes one persistent Hermes `studio` session per project

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
| POST | `/api/chat` | send message → resume studio session and return reply |
| POST | `/api/upload` | drag-drop file into references/ |
| GET | `/media/...` | static mount of studio-root |
| GET | `/comfy/...` | static mount of ComfyUI/output |

## Chat → Hermes wiring

POST /api/chat serializes requests per project and spawns
`hermes -p studio [-r SESSION] chat -Q -t all -q "<msg>"`. The session id is
stored under `.runtime/sessions/`; stdout is the reply and stderr carries only
CLI metadata. Both sides are appended atomically to the project's chat.jsonl.

## Safety / scope guards

- Backend never writes into projects except: create-project, append chat,
  upload to references/. Generation stays agent-side.
- Exact project ids only; the core resolver rejects separators, symlink
  escapes, fuzzy suffix matching and paths outside `projects/`
- No auth v1 (localhost bind only)

## Milestones

1. M1 (done): backend read APIs + media mounts + static index.html
2. M2 (partial): create-project + prompt viewer done; upload pending
3. M3 (done): persistent chat round-trip through studio profile
4. M4: polish — polling refresh, video poster frames, generation filter

## Out of scope (v1)

Auth/multi-user, editing files from UI, workflow editors, timelines,
model management (use scripts/switch-model.sh).
