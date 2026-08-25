# AGENTS.md — hermes-studio

Entry point for any agent (human or LLM) working in this repo. **Keep updated**
as work proceeds. Detailed docs live in `docs/` — this file stays a lean map.

## What this is

Hermes Studio: fully local, agent-orchestrated creative studio.
Hermes profiles orchestrate → ComfyUI (`~/ComfyUI`, RTX 5060 Ti 16GB) renders
video (MiniMax H3) and stills (Krea 2) → filesystem (`studio-root/`) is the
project/media source of truth → SQLite only coordinates web jobs/chat sessions
→ thin FastAPI web UI (`v0.1.0-preview.3`; M1–M5 complete).

## Repo map

| Path | Purpose |
|---|---|
| `PLAN.md` | Single source of truth for architecture decisions |
| `docs/` | Detailed per-topic docs (see below) |
| `hermes/profiles/*/SOUL.md` | Agent personalities; deployed to `~/.hermes/profiles/<name>/` |
| `hermes/skills/*/SKILL.md` | Skills; deployed into profile skill dirs |
| `scripts/design_studio.py` | Project/prompt/chat CLI + generation archiving |
| `scripts/krea2_image.py` | Krea 2 image runner (t2i / style-ref / upscale) |
| `scripts/submit_h3_graph_mcp.py` | Exact graph/reference submission through pinned MCP tooling |
| `scripts/sync-profiles.sh` | Deploy/check repo SOULs + skills against live profiles |
| `scripts/switch-model.sh` | Fleet-wide model/provider switching |
| `scripts/build-web-css.sh` | Rebuild pinned local Tailwind CSS bundle |
| `webapp/` | App factory, routes, SQLite jobs, process manager, uploads, local UI |
| `requirements*.txt` | Pinned runtime and development dependencies |
| `studio_core/` | Dependency-neutral paths, safe files, migration, and domain ownership |
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
- `docs/runtime-event-retention.md` — measured `job_events` retention policy
- `docs/plans/2026-08-24-code-quality-remediation.md` — blocking remediation
  sequence, acceptance gates, and deferred Phase 6 additions

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
  expanded narrow prompts are capped so Chat remains the larger workspace, with
  a compact 24px header and direct scrolling for long prompt text.
- `v0.1.0-preview.2` passed local correctness, dependency, trust-boundary,
  profile-drift, desktop/narrow Chromium, clean-archive, and checksum gates.
- Web-triggered H3 archives now derive execution metadata from authoritative
  ComfyUI history, fail closed on incomplete/mismatched history, and preserve
  actual seed, canvas, timing, steps, acceleration, references, and prompt hash.
- Hermes children now enter a parent-death-supervised launcher before execution;
  stale-job recovery finds exact unrecorded job processes through `/proc`, proves
  termination, and keeps the global running-job lease when ownership is uncertain.
- Scheduler-level fault containment now terminates owned work and persists a
  failure without killing the queue loop; a dead scheduler unregisters its worker
  lease instead of receiving false liveness from the heartbeat thread.
- New Hermes sessions carry an exact `studio-web:<job-id>` source correlation;
  resumed-session activity waits for a successfully captured message baseline,
  preventing previous-job session binding and historical event replay.
- Generation jobs now own immutable prompt, settings, resolved execution, and
  expected archive snapshots in SQLite. Web archives read that exact running-job
  contract, and agent exit zero is rejected unless the matching artifact,
  authoritative prompt ID, prompt, settings, and metadata all exist.
- ComfyUI metadata recovery now binds archived files to one executed `SaveVideo`
  node and traverses only that node's upstream graph, rejecting disconnected
  class-name decoys, ambiguous producers, and duplicate execution nodes.
- Project job enqueue and contract-affecting web mutations now share a descriptor-
  safe coordination lock. Clip/settings changes and take deletion cannot race a
  job into existence between the active-job check and filesystem publication.
- Selected-take validation and manifest publication now share the canonical
  project lock, preventing concurrent deletion from leaving dangling provenance.
- Media review idempotency now records and verifies source/target SHA-256 content
  identities; retries republish changed content without overwriting prior copies.
- Project metadata and take dialogs now bind asynchronous loads/saves/actions to
  revisioned dialog instances, including same-project or same-clip close/reopen.
- Chat submissions and ComfyUI queue refreshes now reject out-of-order completion
  through conversation-bound and latest-request-wins revision checks.
- Stable take media nodes now preserve playback through review updates. Real
  Chromium/CDP tests force deferred dialog/chat/queue responses and close/reopen.
- Phase 5 assembly readiness now validates every enabled clip's exact selected
  video in manifest order and reports all blockers before export is available.
- Project movie export is an explicit project-scoped asynchronous job with an
  immutable ordered source/hash/spec contract. Compatible streams use hard-cut
  stream copy; mismatches normalize deterministically, and versioned movie plus
  exact provenance publish together as one no-overwrite `final/movie-NNN/`
  directory. CPU-only export jobs never invoke ComfyUI cleanup.
- Media now shows project-wide readiness/blockers, requires an explicit export
  click, and exposes every completed version with stable video playback and an
  MP4 download. Polling reuses unchanged movie media nodes.
- Phase 5 passed the complete 211-test Python and 18-test frontend/Chromium suites,
  compilation, dependency, profile-drift, CSS, clean-archive, live service/API,
  desktop, and narrow-browser gates.
- Studio path resolution now survives Hermes profile HOME isolation: profile
  skills use `HERMES_HOME`, user resources use `HERMES_REAL_HOME`, and Studio
  terminal jobs use explicit real-home mode.
- Web generation jobs now use a minimal render toolset and provide an exact,
  compact tail-pinned H3 dry-run command plus deterministic MCP submission
  helper, preventing task loss, runner-source loops, and model-transcribed graph
  or prompt corruption.
- Thermo-nuclear maintainability review consolidated every verified blocker and
  recommendation into a mandatory correctness, runtime-boundary, and browser-
  ownership remediation gate before Phase 6 additions.

### 2026-08-25
- Remediation P0.1 made MCP submission tests part of standard Python discovery,
  added ordered two-reference and failure-path coverage, and established
  `scripts/check.sh` as the clean-commit local non-GPU release entry point.
- Remediation P0.2 established `studio_core.paths` as the canonical account,
  Hermes fleet/profile, and ComfyUI resolver across Python and fleet shell tools,
  including profile-isolated matrices and exact model-switch reporting/failure.
- Remediation P0.3 made user-service installation checkout-independent through a
  verified stable launcher and added locked Python dependencies, `pip-audit`,
  supported Hermes/MCP contract checks, and incremental Ruff correctness linting.
- Remediation P0.4 measured 1,348 activity events across 16 finished jobs and
  established intentional durable retention with explicit size/latency revisit
  triggers instead of speculative pruning.
- Remediation P1.1 moved the 2,077-line legacy migration engine behind a lazy CLI
  import, made all migration suites directly discoverable, and moved cohesive
  safe-file primitives into dependency-neutral `studio_core` ownership.
- Remediation P1.2 established canonical dependency-neutral ownership for
  projects, archival, dispatch, runtime persistence, identifiers, and events;
  static tests prohibit reverse imports from `studio_core` or `scripts`.
- Remediation P1.3 removed Hermes from deterministic generation execution:
  a supervised worker now builds, submits, waits, archives, validates, and
  cleans up the immutable render contract directly through pinned MCP tooling.
- Remediation P1.4 reduced `StudioJobManager` to FIFO, leases, lifecycle, and one
  typed dispatch table; cohesive process, agent, generation, and movie runners
  now own execution, timeout, recovery, event, and cleanup behavior.
- Remediation P1.5 added discriminated job kinds/scopes/activity phases and
  per-kind payload codecs at SQLite boundaries; schema migration 6 rejects
  invalid contracts and the route/manager protocol is statically complete.
- Remediation P2.1 extracted scoped conversation ownership, reconciles activity
  cards/events by stable identity, and preserves follow-latest, deliberate
  scroll-away, nested details/scroll, focus, selection, and DOM identity.
- Remediation P2.2 established explicit reference-controller ownership and made
  media review, generation settings, project dialog, refresh, and queue request
  flags module-local; shared browser state now contains workspace context only.
- Remediation P2.3 extracted reusable fixture-app/Chromium/CDP lifecycle and
  interception helpers; five independent scenarios now identify playback,
  dialog, chat, queue-ordering, and conversation-scroll failures directly.

## Next steps

- [ ] Run and document the complete remediation release gate from clean,
  committed source.
- [ ] Only after the remediation gate passes, add scoped Hermes `clarify`
  responses and typed previous-selected-take last-frame generation inputs.
