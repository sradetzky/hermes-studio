# PLAN.md — Hermes Studio

**Status**: v0.1 preview candidate; web M1–M4.1 + clip/take hierarchy and
real H3 E2E complete
**Owner**: Sven (local setup on RTX 5060 Ti 16GB)  
**Date**: 2026-08-22  
**Goal**: Fully local, agent-orchestrated creative studio centered on MiniMax H3 + Hermes, with a simple self-hosted web UI.

This document is the single source of truth for architecture decisions and
implementation status.

---

## 1. High-Level Architecture

```
User (browser)
    ↓
Simple Web UI (FastAPI + minimal frontend)
    ↓
Hermes Profile "studio"  ←→  Local LLM endpoint (shared across profiles)
    ↓ (comfyui-mcp)
ComfyUI (native MiniMax H3 nodes)
    ↓
Folder structure on disk (source of truth for projects & media)
```

- **Hermes** is the orchestration brain (planning, H3 prompt writing, tool calling).
- **ComfyUI** is the heavy GPU worker for H3 generation.
- **Filesystem** is the database (projects, references, generations, chat history).
- **Web UI** is a thin window onto the chat + media. No complex state in the UI.

---

## 2. Key Decisions (Locked)

### 2.1 Hermes Profiles for Personalities
- Use official Hermes **Profiles** feature.
- Main profile remains everyday use.
- New profile `studio` for the Design Studio agent (own SOUL.md, own memory, own sessions).
- Specialist profile `studio-grok`: Grok 4.6 backup for xAI web/X research and
  Grok Imagine; excluded from fleet model switching and local GPU ownership.
- **Model switching strategy**: The local Studio fleet switches together through
  `scripts/switch-model.sh`. The cloud backup `studio-grok` is the one explicit
  exception and remains pinned to xAI OAuth / `grok-4.6`.

### 2.2 MiniMax H3
- Open weights (FL2VA + Ref2VA).
- Native ComfyUI support since ~2026-08-03.
- Target: 16GB VRAM → pruned INT8 / NVFP4 / Turbo LoRA + latent upscaler path.
- Always generate structured official prompts (see 2.4).
- Output: 4–15s video + native stereo audio at short-edge ~768p, then optional latent upscale.

### 2.3 Folder Structure (Source of Truth)
```
~/design-studio/   (or path of choice, set in Hermes skill)
├── projects/
│   └── YYYY-MM-DD_name/
│       ├── project.json          # ordered immutable clip ids + selected takes
│       ├── brief.md
│       ├── chat.jsonl
│       ├── references/          # uploaded assets (image1_..., video1_..., audio1_...)
│       ├── research/
│       ├── clips/
│       │   └── clip-001/
│       │       ├── chat.jsonl
│       │       ├── current_prompt.txt
│       │       ├── current_generation.json # typed settings + prompt hash
│       │       └── generations/
│       │           └── 001/
│       │               ├── video.mp4
│       │               ├── prompt.txt
│       │               ├── settings.json
│       │               └── meta.json
│       └── final/
├── shared/
│   ├── characters/
│   ├── styles/
│   └── workflows/               # pre-parameterized H3 API-format JSONs
└── tmp/
```

Project chat owns cross-clip planning and direction; each clip owns an
independent execution chat. References, research, and final exports stay
shared. Each clip also owns its prompt, generation contract, immutable takes,
and optional selected take. Hermes and the web UI always carry an explicit
project/clip chat scope and exact IDs.

### 2.4 Prompting
- Use official MiniMax H3 structure strictly.
- Base modes (T2VA / I2VA / FL2VA / L2VA):  
  `integrated_multimodal_description` + `overall_soundscape` + `non_diegetic_music`  
  (+ frame alignment instruction when applicable)
- Ref2VA: six-section format with explicit reference roles.
- Hermes must have a skill that forces this structure (copy from MiniMax-AI/MiniMax-H3 `h3-prompt-writing` skill or re-implement).

### 2.5 Web UI Requirements (Simple)
- Self-hosted, minimal dependencies.
- Explicit Project/Clip chat selector with independent Hermes `studio` sessions.
- Display current structured prompt.
- Play generated videos (HTML5).
- Show reference thumbnails + previous generations.
- Project switcher (reads folders).
- Drag-drop upload into current project’s `references/`.
- Organization stays on disk; UI does not invent structure.
- Preferred stack: FastAPI backend + single HTML page (Alpine.js or vanilla + Tailwind CDN). No heavy SPA.

### 2.6 Integration Points
- ComfyUI ↔ Hermes: pinned `comfyui-mcp`; the `studio` profile is the sole GPU
  queue owner. No silent raw-REST fallback in normal operation.
- Pre-export clean API-format workflows for T2VA, FL2VA, Ref2VA with injectable parameters (prompt, duration, seed, refs, turbo strength, etc.).
- Hermes skill must be able to:
  - Create new project folder
  - Write/update one exact clip's `current_prompt.txt` and append to the explicit
    project or clip `chat.jsonl`
  - Call ComfyUI workflow
  - Archive finished outputs into `clips/<clip-id>/generations/NNN/`
    with prompt/settings/metadata snapshots

---

## 3. Implementation Order (for the implementing agent)

### Phase 0 – Preparation (human or agent)
- [ ] Ensure ComfyUI ≥ 0.30 with native H3 nodes and suitable quants/Turbo LoRA + latent upscaler.
- [ ] Have at least one working H3 workflow in API format.
- [ ] Local LLM server running (OpenAI-compatible).

### Phase 1 – Hermes Profile & Skill (highest priority)
1. Create profile: `hermes profile create studio --clone`
2. Write solid `SOUL.md` for the studio agent (orchestrator + strict H3 prompt engineer).
3. Implement `design-studio` skill that understands the folder root and can:
   - `create_project(name, brief)`
   - `write_prompt(project, clip, structured_prompt)`
   - `append_chat(project, role, content)`
   - `run_generation(project, clip, workflow_name, params)` → archives outputs correctly
4. Point the studio profile’s model config at the shared local endpoint.
5. Test from CLI: create project → write prompt → (manual ComfyUI for now) → verify folder layout.

### Phase 2 – ComfyUI Wiring
- [x] Connect the `studio` profile to pinned `comfyui-mcp` and verify tools.
- [x] Verify a real H3 API-format workflow submission through MCP; always archive output and call
  `clear_vram` after every terminal success/error/cancel/timeout.
- [x] Verify parameter injection against the real workflow.
- [x] Verify completed media is archived into the selected clip's `generations/`
  before release.

### Phase 3 – Minimal Web UI
- FastAPI app that:
  - Serves static index.html
  - independently scoped project/clip chat, settings, take, and media APIs
  - project-scoped reference/job APIs plus scope-bound chat/activity APIs
  - guarded media from shared references/final and exact clip take archives
- Single page with project + clip navigation, chat, take player, prompt viewer,
  settings, references, and selected-take controls.
- Stable polling for shared state and the exact active clip.
- [x] Async per-project job state + visible queued/running/completed/failed status.
- [x] Multi-file drag/drop reference upload with safe non-overwriting storage.
- [x] Transactional SQLite runtime coordination, lifecycle-managed Hermes
  children, guarded media routes and fully local frontend assets.
- [x] Per-profile live activity timeline with reasoning/tool events, explicit
  specialist targeting, persistent profile sessions and serialized orchestrator
  handoffs.
- [x] Ordered clip hierarchy with clip-local prompts/settings/takes, exact
  clip-scoped jobs and APIs, selected-take provenance, migration, and web controls.
- [x] Explicit Project/Clip chat scope with isolated transcripts, activity
  cursors, profile sessions, specialist continuity, and lossless project-history migration.

### Phase 4 – Polish
- [x] Media detail/filter/review actions
- [x] Typed generation settings manifest + prompt readiness/editor panel
- [x] “Generate with this prompt” button enabled for a ready, revision-matched
  prompt/settings contract, with worker-start revalidation and exact Studio job dispatch
- [x] Promote to `final/` and copy selected generation media into references
- [x] Create/rename/reorder/enable clips and select one video take per enabled clip
- Basic project metadata

---

## 4. Non-Goals (for now)
- Full multi-agent parallel workers inside Hermes (keep single studio agent for simplicity).
- Automatic 2K regeneration (H3-Regenerate-2K is closed; use latent upscaler instead).
- Complex database or user accounts.
- Fancy frontend frameworks or design systems.
- Cloud deployment.

---

## 5. Open Questions / Future
- Exact path for studio-root (suggest `~/design-studio` or configurable via env).
- Whether to keep chat history only in `chat.jsonl` or also mirror into Hermes session DB.
- How aggressively to auto-create generation folders vs let Hermes decide numbering.
- Later: side-by-side comparison, selected-take assembly timeline, shared
  character library tooling.

---

## 6. Success Criteria
- From the web UI I can chat with the studio agent, get a correct structured H3 prompt, trigger a generation, and play the resulting video — all while the folder structure stays clean and consistent.
- I can switch the underlying local model and every profile (including studio) immediately uses the new one.
- Different SOUL.md files give clearly different agent personalities without contaminating each other.

---

**End of PLAN.md**  
Implementing agent: follow the phases in order. Prefer small, testable increments. Update this PLAN.md with any important deviations.