# AGENTS.md — hermes-studio

Entry point for any agent (human or LLM) working in this repo. **Keep updated**
as work proceeds. Detailed docs live in `docs/` — this file stays a lean map.

## What this is

Hermes Studio: fully local, agent-orchestrated creative studio.
Hermes profiles orchestrate → ComfyUI (`~/ComfyUI`, RTX 5060 Ti 16GB) renders
video (MiniMax H3) and stills (Krea 2) → filesystem (`studio-root/`) is the
project/media source of truth → SQLite only coordinates web jobs/chat sessions
→ thin FastAPI web UI (M1–M4.6 complete; preview closure next).

## Repo map

| Path | Purpose |
|---|---|
| `PLAN.md` | Single source of truth for architecture decisions |
| `docs/` | Detailed per-topic docs (see below) |
| `hermes/profiles/*/SOUL.md` | Agent personalities; deployed to `~/.hermes/profiles/<name>/` |
| `hermes/skills/*/SKILL.md` | Skills; deployed into profile skill dirs |
| `scripts/design_studio.py` | Project/prompt/chat CLI + generation archiving |
| `scripts/krea2_image.py` | Krea 2 image runner (t2i / style-ref / upscale) |
| `scripts/sync-profiles.sh` | Deploy/check repo SOULs + skills against live profiles |
| `scripts/switch-model.sh` | Fleet-wide model/provider switching |
| `scripts/build-web-css.sh` | Rebuild pinned local Tailwind CSS bundle |
| `webapp/` | App factory, routes, SQLite jobs, process manager, uploads, local UI |
| `requirements*.txt` | Pinned runtime and development dependencies |
| `comfyui/workflows/` | Parameterized H3 API-format workflow JSONs (empty) |
| `studio-root/` | Default studio root: projects/, shared/, tmp/ |

## Docs index

- `docs/agents.md` — fleet roles, spawning, model switching
- `docs/studio-cli.md` — design_studio.py commands & folder contract
- `docs/image-pipeline.md` — Krea 2 recipes, models, capabilities
- `docs/video-pipeline.md` — H3 runner integration, proven knobs
- `docs/comfyui-mcp.md` — production transport, queue/cleanup transaction
- `docs/grok-backup.md` — Grok 4.6 web/X/Imagine backup profile + dispatch
- `docs/frontend-plan.md` — web UI stack, layout, API surface, milestones

## Conventions

- Studio root override: `$DESIGN_STUDIO_ROOT` (defaults to repo's `studio-root/`)
- Git author: Sven Radetzky <sven.radetzky@gmx.de>
- Sequential GPU jobs only — never two ComfyUI jobs at once
- Automatically commit every completed, verified feature slice; never push
- When a slice changes scope or status, update `PLAN.md`, this file's **Next
  steps**, the relevant detailed doc, and `CHANGELOG.md` when user-visible in the
  same verified commit. Keep completed work in the progress log, not in the
  active checklist.

## Progress log

### 2026-08-22
- Phase 0+1: repo init, `studio` profile, design_studio.py, design-studio skill
- Subagent fleet created + verified: storyboarder, prompt-engineer, reviewer,
  illustrator (all cloned from studio; SOULs authored in-repo)
- Camera doctrine revised: dynamic moves welcome with disciplined specs
- Image pipeline: krea2_image.py (5 recipes incl. identity edit, GPU-verified),
  generate-image archiving, studio-illustrator live
- Docs split out of AGENTS.md into docs/
- Phase 3 M1–M3: webapp/ FastAPI + single-page UI; chat round-trip through
  the studio profile verified live (see docs/frontend-plan.md)
- Quality pass: exact project ids, atomic chat records, persistent per-project
  Hermes sessions, stable incremental UI polling, profile drift checks
- Phase 2 transport: studio owns pinned comfyui-mcp; MCP clear_vram verified;
  every terminal job must unload models/free memory
- Backup specialist: studio-grok on xAI OAuth/Grok 4.6; xAI web + X search
  verified, Imagine quality configured; persistent project dispatch available
- Web M4 foundation: asynchronous project jobs, visible activity state, and
  safe multi-file drag/drop references
- Thermo-nuclear refactor: app factory + SQLite transactional jobs/chat/session,
  lifespan-owned scheduler/processes, atomic upload store, guarded media routes,
  local CSS/JS modules, continuous stale-peer recovery, locked CLI chat exports,
  and route/process/concurrency/lifecycle tests
- Web launcher hardened: single-instance flock/PID ownership plus explicit
  status and graceful stop scripts; duplicate starts are rejected

### 2026-08-23
- Web profile observability: immediate user turns, persistent per-job activity,
  live Hermes reasoning/tool projection, manual profile targeting, serialized
  specialist handoffs, and a 3h Studio timeout that no longer interrupts valid
  H3 renders at the old 10-minute boundary
- Web M4 media review: media/recipe/review filters, full generation detail
  dialog, archived prompt/metadata/action history, and guarded idempotent copies
  to `final/` or `references/`
- Web M4.1 generation contract: typed prompt-bound `current_generation.json`,
  readiness/staleness display, safe MP/explicit-canvas validation, and editable
  mode/seed/steps/exact-accel knobs; prompts own duration and ordered references
- Preview-release hardening: public setup/release docs, corrected custom-provider
  example, local dependency/security/browser gates, and specialist lease recovery
  that cannot cancel Studio-owned ComfyUI work
- Release-candidate audit: trusted localhost boundary, private runtime state,
  symlink-safe metadata, atomic generation publication, resilient scheduling,
  exact tool-call projection, and stale settings-form protection
- Generation controls simplified: prompt-owned length/references, no SeedVR2 or
  model overrides, and accel now means Sol fused modulation + ChunkFF only
- Clip/take publication and archival reads hardened against no-replace and
  symlink-swap races with descriptor-based filesystem operations
- Project → Clips → Takes transition completed end to end: exact clip-scoped
  jobs, nested settings/media APIs, ordered clip web controls, selected-take
  provenance, explicit verified legacy migration, and synchronized Studio docs
- Real clip-bound H3 E2E verified through the web-owned Studio session and
  comfyui-mcp: exact 1280x704 R2V graph parameters, clip-local archive read-back,
  identical source/archive hashes, empty queue, and mandatory VRAM cleanup
- ComfyUI queue observability added to the web header with a compact live render
  summary and expandable sanitized recipe, mode, canvas, approximate clip
  length, frames, steps, accel, seed, elapsed/waiting time, and last-completed
  duration; Studio render waits use comfyui-mcp's two-second batch status loop
  instead of fixed three-minute terminal waits
- Persistent user-systemd startup and tailnet-only Tailscale Serve HTTPS added
  without widening Uvicorn beyond loopback; exact tailnet host/origin checks
  preserve the trusted-access boundary and coexist with the existing port 443 app

### 2026-08-24
- Take management now supports confirmed whole-take deletion with selected-take
  cleanup, active-job and symlink guards, identity-checked filesystem removal,
  and preservation of shared final/reference copies
- Prompt-ready enabled clips now expose **Generate with this prompt**. The typed
  request is revision-guarded at enqueue and worker start, creates a dedicated
  Studio generation job, and uses the verified comfyui-mcp archive/cleanup path
- Project and clip conversations are now explicitly selectable and isolated end
  to end: transcript/activity cursors, Studio and specialist Hermes sessions,
  and filesystem exports. Legacy shared history migrates intact to Project chat.
- Project display titles and Markdown briefs are editable through a validated
  project-details dialog; immutable filesystem IDs remain visible and unchanged,
  active jobs block writes, and serialized descriptor-safe publication is tested.
- Wide screens retain the Projects/Chat/Media three-pane workspace; viewports at
  1099px and below use explicit keyboard-accessible pane navigation without DOM
  replacement, preserving project/clip/chat state and active media playback.
- Mobile queue details are viewport-bounded, and Prompt & generation is a
  desktop-and-mobile collapsible panel that defaults closed on narrow screens;
  expanded narrow prompts are capped so Chat remains the larger workspace.

## Next steps

- [ ] Desktop/narrow browser release gate, synchronized docs, and next preview
