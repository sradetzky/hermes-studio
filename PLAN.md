# PLAN.md — Hermes Studio

**Status**: Skeleton ready for agent implementation  
**Owner**: Sven (local setup on RTX 5060 Ti 16GB)  
**Date**: 2026-08-22  
**Goal**: Fully local, agent-orchestrated creative studio centered on MiniMax H3 + Hermes, with a simple self-hosted web UI.

This document is the single source of truth for decisions made so far. A cheaper/local agent should follow this plan to implement the system.

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
│       ├── brief.md
│       ├── chat.jsonl
│       ├── current_prompt.txt
│       ├── references/          # uploaded assets (image1_..., video1_..., audio1_...)
│       ├── generations/
│       │   └── 001/
│       │       ├── video.mp4
│       │       ├── preview.jpg
│       │       ├── prompt.txt
│       │       └── meta.json
│       └── final/
├── shared/
│   ├── characters/
│   ├── styles/
│   └── workflows/               # pre-parameterized H3 API-format JSONs
└── tmp/
```

Hermes (via design-studio skill) is responsible for creating projects and writing into the correct locations. UI only reads.

### 2.4 Prompting
- Use official MiniMax H3 structure strictly.
- Base modes (T2VA / I2VA / FL2VA / L2VA):  
  `integrated_multimodal_description` + `overall_soundscape` + `non_diegetic_music`  
  (+ frame alignment instruction when applicable)
- Ref2VA: six-section format with explicit reference roles.
- Hermes must have a skill that forces this structure (copy from MiniMax-AI/MiniMax-H3 `h3-prompt-writing` skill or re-implement).

### 2.5 Web UI Requirements (Simple)
- Self-hosted, minimal dependencies.
- Chat interface with Hermes `studio` profile.
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
  - Write/update `current_prompt.txt` and append to `chat.jsonl`
  - Call ComfyUI workflow
  - Move finished outputs into `generations/NNN/` and write `meta.json`

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
   - `write_prompt(project, structured_prompt)`
   - `append_chat(project, role, content)`
   - `run_generation(project, workflow_name, params)` → moves outputs correctly
4. Point the studio profile’s model config at the shared local endpoint.
5. Test from CLI: create project → write prompt → (manual ComfyUI for now) → verify folder layout.

### Phase 2 – ComfyUI Wiring
- [x] Connect the `studio` profile to pinned `comfyui-mcp` and verify tools.
- Submit API-format workflows via MCP; always archive output and call
  `clear_vram` after every terminal success/error/cancel/timeout.
- Parameter injection must be reliable.
- After generation, skill must cleanly archive into `generations/`.

### Phase 3 – Minimal Web UI
- FastAPI app that:
  - Serves static index.html
  - `/api/chat` → asynchronous persisted jobs on Hermes studio profile
  - `/api/projects` + `/api/project/{id}/media`
  - Mounts the studio-root safely for media serving
- Single page with: project list, chat, video player, prompt viewer, references.
- Auto-refresh or simple polling for new generations.
- [x] Async per-project job state + visible queued/running/completed/failed status.
- [x] Multi-file drag/drop reference upload with safe non-overwriting storage.
- [x] Transactional SQLite runtime coordination, lifecycle-managed Hermes
  children, guarded media routes and fully local frontend assets.

### Phase 4 – Polish
- Media detail/filter/review actions
- “Generate with this prompt” button that appears when Hermes outputs a structured prompt
- Promote to `final/`
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
- Later: side-by-side comparison, simple multi-clip timeline, shared character library tooling.

---

## 6. Success Criteria
- From the web UI I can chat with the studio agent, get a correct structured H3 prompt, trigger a generation, and play the resulting video — all while the folder structure stays clean and consistent.
- I can switch the underlying local model and every profile (including studio) immediately uses the new one.
- Different SOUL.md files give clearly different agent personalities without contaminating each other.

---

**End of PLAN.md**  
Implementing agent: follow the phases in order. Prefer small, testable increments. Update this PLAN.md with any important deviations.