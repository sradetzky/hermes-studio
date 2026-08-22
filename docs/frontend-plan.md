# Frontend Plan — Studio Web UI

Phase 3 of PLAN.md. Principles: thin read-mostly window onto the filesystem;
no state in the UI that isn't already on disk; minimal dependencies.

## Stack (locked)

- **FastAPI** backend (`.venv/`, deps: fastapi + uvicorn only)
- **Single HTML page**, vanilla JS + Tailwind via CDN — no SPA framework
- Media served by mounting `studio-root/` and `~/ComfyUI/output` read-only
- Chat streams to the Hermes `studio` profile

## Layout (single page, three columns)

```
┌──────────┬──────────────────────────────┬────────────┐
│ PROJECTS │  CHAT (with studio agent)    │ MEDIA      │
│ list     │  streaming responses         │ references │
│ + new    │                              │ generations│
│          │  CURRENT PROMPT panel        │ video/img  │
│          │  (from current_prompt.txt)   │ players    │
└──────────┴──────────────────────────────┴────────────┘
```

- Project switcher = left rail, reads folders; new project button
- Center: chat with the studio agent; below it the current structured prompt
- Right: reference thumbnails, generation gallery (newest first), HTML5 video
  player for clips
- Polling every ~5s for new generations (simple; no websockets needed for v1)

## API surface (v1)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects` | list projects |
| POST | `/api/projects` | create project {name, brief} |
| GET | `/api/project/{id}` | brief, chat tail, current_prompt |
| GET | `/api/project/{id}/generations` | listing w/ meta.json contents |
| GET | `/api/project/{id}/chat?after=N` | poll new chat lines |
| POST | `/api/chat` | send message → stream studio profile reply |
| POST | `/api/upload` | drag-drop file into references/ |
| GET | `/media/...` | static mount of studio-root |
| GET | `/comfy/...` | static mount of ComfyUI/output |

## Chat → Hermes wiring

POST /api/chat spawns `hermes -p studio chat -q "<msg>" --quiet` as subprocess
(v1), captures stdout, appends both sides to the project's chat.jsonl. SSE or
chunked response for streaming feel. v2 candidate: persistent session resume.

## Safety / scope guards

- Backend never writes into projects except: create-project, append chat,
  upload to references/. Generation stays agent-side.
- Path traversal guard on all path params (resolve + prefix check)
- No auth v1 (localhost bind only)

## Milestones

1. M1: backend read APIs + media mounts + static index.html (read-only UI)
2. M2: create-project + upload + prompt viewer
3. M3: chat round-trip through studio profile
4. M4: polish — polling refresh, video poster frames, generation filter

## Out of scope (v1)

Auth/multi-user, editing files from UI, workflow editors, timelines,
model management (use scripts/switch-model.sh).
