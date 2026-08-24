# PLAN.md — Hermes Studio

**Status**: `v0.1.0-preview.3` release candidate prepared with Phase 4.7
remediation and Phase 5 project movie assembly complete.
**Owner**: Sven (local setup on RTX 5060 Ti 16GB)  
**Updated**: 2026-08-24
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
Hermes Profile "studio"  ←→  Configured model/provider
    ↓ (comfyui-mcp)
ComfyUI (native MiniMax H3 nodes)
    ↓
Folder structure on disk (source of truth for projects & media)
```

- **Hermes** is the orchestration brain (planning, H3 prompt writing, tool calling).
- **ComfyUI** is the heavy GPU worker for H3 generation.
- **Filesystem** is the durable source of truth for projects, media, prompts,
  settings, and scoped chat exports; SQLite coordinates runtime jobs, sessions,
  and activity.
- **Web UI** is a thin window onto the chat + media. No complex state in the UI.

---

## 2. Key Decisions (Locked)

### 2.1 Hermes Profiles for Personalities
- Use official Hermes **Profiles** feature.
- Main profile remains everyday use.
- New profile `studio` for the Design Studio agent (own SOUL.md, own memory, own sessions).
- Studio uses Hermes `terminal.home_mode: real`; profile skills/state resolve
  from `$HERMES_HOME`, while account repos, ComfyUI, and Documents resolve from
  `$HERMES_REAL_HOME` rather than the potentially isolated `$HOME`.
- Specialist profile `studio-grok`: Grok 4.6 backup for xAI web/X research and
  Grok Imagine; excluded from fleet model switching and local GPU ownership.
- **Model switching strategy**: The local Studio fleet switches together through
  `scripts/switch-model.sh`. The cloud backup `studio-grok` is the one explicit
  exception and remains pinned to xAI OAuth / `grok-4.6`.

### 2.2 MiniMax H3
- Open-weight T2VA / I2VA / FL2VA / Ref2VA modes through native ComfyUI nodes.
- The proven 16GB path uses a clean single pass at no more than 1.1MP; current
  preview/final defaults are 0.5MP/8 steps and 0.9MP/20 steps.
- Acceleration means only Sol fused modulation + ChunkFF. Quantization, model,
  Turbo, SeedVR2, and upscale controls are not part of the web generation contract.
- Always generate structured official prompts (see 2.4).
- Output: 4–15s video with native stereo audio, archived at the generated canvas.

### 2.3 Folder Structure (Source of Truth)
```
<studio-root>/   (repo `studio-root/` or `$DESIGN_STUDIO_ROOT`)
├── projects/
│   └── YYYY-MM-DD_name/
│       ├── project.json          # display title, ordered clip ids + selected takes
│       ├── brief.md              # editable project brief
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
- Stack: FastAPI backend + vanilla ES modules + locally compiled Tailwind CSS.
  No heavy SPA or runtime CDN.

### 2.6 Integration Points
- ComfyUI ↔ Hermes: pinned `comfyui-mcp`; the `studio` profile is the sole GPU
  queue owner. No silent raw-REST fallback in normal operation.
- Build API-format H3 graphs with the proven `run_h3.py --dry-run` builder,
  upload references through MCP, and submit the resulting graph through the
  pinned batch tools. Repo workflow JSON files remain optional.
- Hermes skill must be able to:
  - Create new project folder
  - Write/update one exact clip's `current_prompt.txt` and append to the explicit
    project or clip `chat.jsonl`
  - Call ComfyUI workflow
  - Archive finished outputs into `clips/<clip-id>/generations/NNN/`
    with prompt/settings/metadata snapshots

---

## 3. Implementation Status and Order

### Phase 0 – Preparation (complete)
- [x] Native H3 ComfyUI stack and proven 16GB recipes are available.
- [x] A real H3 workflow has been submitted and parameter-verified through
  `comfyui-mcp`.
- [x] The Studio Hermes profile and model/provider configuration are operational.

### Phase 1 – Hermes Profile & Skill (complete)
- [x] `studio` orchestrator and specialist profiles have repo-owned SOULs and
  synchronized skills.
- [x] `design-studio` manages exact projects/clips, scoped prompts/chat, profile
  handoffs, and generation archival.
- [x] Fleet model/provider switching and profile drift checks are operational.
- [x] CLI and web paths are covered by filesystem, route, concurrency, lifecycle,
  migration, and real integration checks.

### Phase 2 – ComfyUI Wiring (complete)
- [x] Connect the `studio` profile to pinned `comfyui-mcp` and verify tools.
- [x] Verify a real H3 API-format workflow submission through MCP; always archive output and call
  `clear_vram` after every terminal success/error/cancel/timeout.
- [x] Verify parameter injection against the real workflow.
- [x] Verify completed media is archived into the selected clip's `generations/`
  before release.

### Phase 3 – Minimal Web UI (complete)
- [x] FastAPI app that:
  - Serves static index.html
  - independently scoped project/clip chat, settings, take, and media APIs
  - project-scoped reference/job APIs plus scope-bound chat/activity APIs
  - guarded media from shared references/final and exact clip take archives
- [x] Single page with project + clip navigation, chat, take player, prompt viewer,
  settings, references, and selected-take controls.
- [x] Stable polling for shared state and the exact active clip.
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

### Phase 4 – Polish (complete)
- [x] Media detail/filter/review actions
- [x] Typed generation settings manifest + prompt readiness/editor panel
- [x] “Generate with this prompt” button enabled for a ready, revision-matched
  prompt/settings contract, with worker-start revalidation and exact Studio job dispatch
- [x] Promote to `final/` and copy selected generation media into references
- [x] Create/rename/reorder/enable clips and select one video take per enabled clip
- [x] Explicit Project/Clip conversations with isolated transcripts, activity,
  profile sessions, and filesystem exports
- [x] Basic project metadata: edit display title and brief while keeping the
  filesystem project ID immutable
- [x] Responsive workspace: retain the desktop three-pane layout and provide
  explicit Projects / Chat / Media navigation on narrow screens
- [x] Re-run desktop and narrow-browser release gates, synchronize current docs,
  and cut `v0.1.0-preview.2` only after both slices are verified

### Phase 4.7 – Fresh-eyes remediation gate (complete)
- [x] Make detached Hermes process ownership crash-safe and never release the
  global GPU lease while a possible job process remains alive.
- [x] Couple worker lease renewal to scheduler health and recover unexpected
  execution-loop failures instead of leaving permanent running jobs.
- [x] Correlate Hermes sessions with exact job identity and retry event baselines
  without replaying prior-session activity.
- [x] Persist an immutable generation execution contract per job and require one
  exact contract-bound archive before a generation job can complete.
- [x] Traverse the exact output-producing ComfyUI graph branch when deriving
  authoritative execution metadata; reject disconnected or ambiguous nodes.
- [x] Prevent active project jobs from racing clip/settings mutations.
- [x] Validate and publish selected-take provenance under one project lock.
- [x] Bind review-action idempotency to source and target content identity rather
  than filenames alone.
- [x] Give project/take dialogs and chat/queue requests immutable request context;
  stale responses must not mutate the current workspace.
  - [x] Project metadata and take dialog instances.
  - [x] Chat submissions and ComfyUI queue refreshes.
- [x] Preserve media element identity and playback state across review actions.
- [x] Add behavioral Chromium coverage for navigation, dialogs, stale responses,
  queue sequencing, and playback; source-text regex checks remain supplementary.

### Phase 5 – Project movie assembly (complete)
- [x] Add project-level assembly readiness for all enabled clips in manifest order;
  block and identify every enabled clip without one valid selected video take
- [x] Add an explicit **Export selected takes as movie** project action that runs
  as one visible asynchronous job and never starts implicitly
- [x] Join selected takes with hard cuts into one MP4, preserving compatible
  streams and applying deterministic normalization only when source media differs
- [x] Publish every export without overwrite under the project's `final/` area,
  together with a provenance manifest containing exact ordered clip/take sources
- [x] Expose completed project movies in Media for playback and download
- [x] Keep trimming, transitions, timeline editing, and side-by-side take
  comparison outside this phase

---

## 4. Non-Goals (for now)
- Parallel agent or GPU workers; specialist handoffs and generation remain serialized.
- Automatic 2K regeneration or an implicit upscale chain.
- Complex database or user accounts.
- Fancy frontend frameworks or design systems.
- Cloud deployment.

---

## 5. Resolved Decisions and Roadmap

Resolved; these are no longer open questions:

- Studio root defaults to the repo's `studio-root/` and can be overridden with
  `$DESIGN_STUDIO_ROOT`.
- SQLite transactionally coordinates scoped web chat/session state; project and
  clip `chat.jsonl` files remain durable filesystem exports.
- The archive path allocates the next exact clip-local `generations/NNN/`
  directory. Agents do not choose or guess generation numbers.

Later candidates, not current commitments:

1. Shared character-library tooling.
2. Side-by-side take comparison, deferred until real projects routinely need to
   compare several viable takes.
3. Timeline editing, trimming, and transitions.

No unresolved architecture question blocks preview release closure.

---

## 6. Success Criteria

Current criteria (met):

- [x] From the web UI, chat with the Studio agent, produce a structured H3 prompt,
  trigger a generation, and play the archived result while the folder contract
  remains clean and exact.
- [x] Switch the configured model/provider across the local Studio fleet while
  preserving the deliberately pinned `studio-grok` exception.
- [x] Keep profile personalities and project/clip conversation scopes isolated.

`v0.1.0-preview.2` criteria (met):

- [x] Edit a project's display title and brief without renaming its immutable ID.
- [x] Navigate Projects, Chat, and Media comfortably at desktop and narrow
  viewports without losing the active project, clip, conversation, or playback.
- [x] Pass the local correctness, trust-boundary, dependency, profile-drift,
  browser, clean-archive, and checksum release gates.

`v0.1.0-preview.3` criteria (met):

- [x] Reject unsafe/stale generation work and preserve exact process, session,
  output-graph, filesystem, browser-request, and playback ownership.
- [x] Export all enabled clips' exact selected video takes in manifest order as
  one explicit versioned hard-cut movie with immutable provenance.
- [x] Pass 211 Python tests, 18 frontend/real-Chromium tests, compilation,
  dependency, profile-drift, CSS, clean-archive, service/API, desktop, and narrow
  live gates.

---

**End of PLAN.md**  
Implementing agent: follow the phases in order. Prefer small, testable increments. Update this PLAN.md with any important deviations.