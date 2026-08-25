# Changelog

All notable changes to Hermes Studio are documented here.

The project follows [Semantic Versioning](https://semver.org/) for tagged
preview and production releases.

## [Unreleased]

### Changed

- Made MCP graph-submission tests visible to standard Python discovery, expanded
  them across ordered multi-reference and deterministic failure paths, and added
  one clean-commit local non-GPU release-check entry point
- Made user-service installation checkout-independent through a verified stable
  launcher and added a complete Python dependency lock, vulnerability audit,
  supported Hermes/MCP contract checks, and incremental correctness linting to
  the canonical gate
- Established measured durable retention for runtime job activity: the preview
  database keeps events with their owning jobs and defines concrete size, row,
  latency, and lifecycle triggers for a future transaction-safe bounded policy
- Isolated the legacy clip-migration state machine in a lazily imported
  `studio_core.migration` module, made each migration suite directly discoverable,
  and moved cohesive safe-file primitives into the dependency-neutral core
- Established one-way dependency-neutral ownership for project/clip operations,
  generation contracts and archives, profile dispatch, runtime persistence, and
  event projection; static checks now reject reverse core and script imports
- Replaced the web generation `hermes chat` proxy with a supervised deterministic
  worker that builds the exact H3 graph, serially uploads ordered references,
  submits and waits through pinned MCP tooling, archives authoritative output,
  and performs direct queue/VRAM cleanup without consuming model tokens
- Split FIFO scheduling from process, agent, generation, and movie execution:
  one typed dispatch table now selects cohesive runners, while one process owner
  handles supervised groups, PID-safe recovery, termination, and timeouts
- Added discriminated job kinds, chat scopes, activity phases/event types, and
  per-kind payload codecs at the SQLite boundary; invalid persisted contracts
  now fail closed, schema migration 6 enforces kind/scope combinations, and the
  manager protocol covers every route submission
- Extracted scoped chat, job, and activity ownership into a conversation
  controller that incrementally reconciles stable job/event nodes instead of
  replacing activity subtrees
- Extracted reference refresh/uploads into a controller and moved media,
  generation-settings, project-dialog, refresh, and queue-request state out of
  the shared browser singleton
- Split the monolithic Chromium regression into five independently named
  playback, dialog, chat, queue-ordering, and conversation-scroll scenarios on
  one reusable disposable-app/CDP harness

### Fixed

- Unified account, Hermes fleet/profile, and ComfyUI path resolution across the
  webapp, CLI, profile sync, and model switcher so profile-isolated `$HOME` values
  cannot create nested fleet paths or redirect account-owned resources
- Prevented immutable web generation from reaching any LLM context: exact prompt,
  settings, graph bytes, MCP identifiers, and output provenance now stay within
  validated filesystem/process boundaries and fail closed on drift or mismatch
- Made Studio profile jobs and legacy H3 runner paths robust to Hermes profile
  HOME isolation: active-profile skills use `$HERMES_HOME`, account files use
  `$HERMES_REAL_HOME`, and the Studio profile forces real-home terminal mode
- Fixed transcript and nested activity scrolling so new conversations and users
  already following output remain pinned to the end, while intentional
  scroll-away, open details, focus, selection, and nested scroll are preserved
- Preserved exact pre-contract generation metadata for completed/failed legacy
  jobs so historical job lists remain readable, while active and malformed
  payloads continue to fail closed at the typed persistence boundary

## [0.1.0-preview.3] - 2026-08-24

### Changed

- Completed the blocking fresh-eyes remediation gate before project movie
  assembly, covering process/session/generation ownership, output-bound
  provenance, filesystem transactions, stale browser responses, playback
  stability, and behavioral browser coverage
- Added ordered project movie readiness with exact blocker reporting for every
  enabled clip that lacks a safe selected video take
- Added explicit asynchronous selected-take movie jobs with immutable ordered
  SHA-256/media-spec contracts, supervised FFmpeg hard-cut assembly, compatible
  stream copy, deterministic mismatch normalization, and artifact-bound success
- Added atomic no-overwrite `final/movie-NNN/` publication containing `movie.mp4`
  and exact ordered-source/output provenance; changed sources, tampered filter
  contracts, collisions, or incomplete artifacts fail closed
- Restricted ComfyUI cancellation/cleanup to Studio jobs that can own GPU work;
  CPU-only movie exports cannot interrupt rendering
- Added project-wide Media readiness/blockers, an explicit export control, and
  stable completed-movie playback plus MP4 download; unchanged movie nodes survive
  polling so active playback is not reset
- Completed Phase 5 release verification across 211 Python tests, 18 frontend/
  real-Chromium tests, clean-archive execution, dependency/profile/CSS checks, and
  live service, API, desktop, and narrow-browser readback

### Fixed

- Closed the detached-process registration crash window with parent-death
  supervision and exact job-token orphan discovery; recovery now retains the
  global execution lease unless process termination is proven
- Coupled worker liveness to scheduler health and contained unexpected execution
  failures so a dead scheduler cannot keep renewing an unrecoverable job lease
- Replaced timestamp-shaped Hermes session discovery with exact per-job source
  correlation and retryable resumed-session baselines that cannot replay old
  reasoning or tool activity after a transient SQLite failure
- Made web generation jobs persist immutable prompt/settings/execution/archive
  contracts; archival reads the exact running job snapshot, and subprocess exit
  zero no longer completes a job without a matching artifact and prompt ID
- Bound authoritative H3 metadata to the exact executed `SaveVideo` output branch
  instead of global first-class-name matches; disconnected or ambiguous graph
  nodes and output producers now fail closed
- Serialized project job enqueue against project metadata, clip creation/order/
  metadata, generation-settings writes, and take deletion; active-job mutations
  now return `409` without a check/enqueue race window
- Made selected-take publication atomic with descriptor-safe source validation
  under the project lock, preventing concurrent deletion from dangling selection
- Bound promote/reference retry idempotency to recorded source and target SHA-256
  identities; changed archive or destination bytes now produce a new safe copy
- Bound project-metadata and take-detail asynchronous operations to revisioned
  dialog instances so navigation, close/reopen, and take changes reject stale work
- Bound chat completion to an exact conversation/request revision and made ComfyUI
  queue polling latest-request-wins, preventing stale workspace or queue updates
- Preserved active take media elements across review refreshes and added real
  Chromium coverage for close/reopen, navigation, stale chat/queue, and playback
- Fixed generation-media descriptor contexts so exceptions from caller-owned
  publication work are no longer misclassified as unsafe source media
- Web-triggered H3 archives now recover actual seed, canvas, timing, steps,
  acceleration nodes, ordered references, and prompt hash from authoritative
  ComfyUI history; incomplete or mismatched execution metadata fails closed
  instead of publishing a partial take record

### Preview limitations

- No application authentication or multi-user isolation; tailnet access relies
  on Tailscale identity and ACL policy
- No side-by-side take comparison, trimming, transitions, or timeline editor
- Linux, Hermes Agent, FFmpeg, ComfyUI, models, and workflows must be installed
  separately

## [0.1.0-preview.2] - 2026-08-24

### Added

- Ordered Project → Clips → Takes hierarchy with immutable clip IDs, enabled
  state, selected video-take provenance, and an explicit resumable legacy migration
- Clip navigation and Add/Rename/Up/Down/Enable controls in the web UI
- Exact clip context on every new Studio job and nested clip/take APIs/media URLs
- Verified exact-clip web → Studio → comfyui-mcp H3 generation, graph parameter
  read-back, clip-local archival, and mandatory queue/VRAM cleanup
- Revision-guarded **Generate with this prompt** action for ready enabled clips,
  with dedicated Studio jobs, worker-start revalidation, and exact typed run packages
- Compact ComfyUI header status with expandable sanitized render specifications,
  elapsed/waiting time, queue order, and exact last-completed execution duration
- Explicit Clip/Project chat selector with independent transcripts, activity
  cursors, Hermes profile sessions, specialist sessions, and clip-local exports
- Editable project display titles and Markdown briefs with immutable visible IDs,
  validated serialized publication, active-job guards, and a project-details dialog
- Responsive Projects / Chat / Media workspace navigation below 1100px, with the
  desktop three-pane layout preserved, keyboard controls, narrow dialog/composer
  layouts, and pane switches that retain state and media playback

### Changed

- Synchronized roadmap/status documents around M4.4 and the next metadata and
  responsive-workspace milestones; superseded root, chat, archive-numbering,
  model-provider, and upscale assumptions are now recorded as resolved
- Studio guidance now prefers the proven clean single-pass H3 path at no more
  than 1.1MP instead of recommending implicit quantization or upscale chains
- Simplified H3 settings to mode, canvas, seed, steps, and acceleration; clip
  length and ordered reference filenames are now parsed from the prompt
- Removed SeedVR2, turbo, W4A8, model, and reference controls from the dialog
- Acceleration now uses only Sol fused modulation and ChunkFF, without Sage,
  sparse Sol attention, or EasyCache
- Prompt, generation settings, take archives, and execution chat moved into
  `clips/<clip-id>/`; Project chat remains available for cross-clip direction
- Existing shared transcript/session/activity state migrates transactionally and
  losslessly into Project history; clip conversations start clean

### Fixed

- Kept the ComfyUI queue popover inside phone viewports and rebalanced narrow Chat
  with a collapsed-by-default, independently scrollable Prompt & generation panel;
  the same panel can now be collapsed on desktop for additional transcript space.
  Reduced its header to 24px and restored direct scrolling inside long prompts

### Preview limitations

- No application authentication or multi-user isolation; tailnet access relies
  on Tailscale identity and ACL policy
- No side-by-side take comparison, selected-take assembly, or timeline editor
- Linux, Hermes Agent, ComfyUI, models, and workflows must be installed separately

## [0.1.0-preview.1] - 2026-08-23

### Added

- Local FastAPI/vanilla-JS Studio workspace with folder-backed projects
- Persistent asynchronous Hermes chat jobs and per-project profile sessions
- Live profile reasoning summaries, tool activity, handoffs, and job status
- Serialized global Studio/ComfyUI execution with worker leases and recovery
- Safe reference uploads and guarded media serving
- Prompt-bound typed H3 generation settings and readiness validation
- Editable mode, duration, MP/canvas, steps, accel, turbo/model, ordered
  reference, W4A8, and SeedVR2 controls
- Generation filters, detail viewer, archived prompt/metadata display, and
  promote-to-final/use-as-reference review actions
- Confirmed exact-take deletion with selected-take cleanup, active-job blocking,
  symlink rejection, and preservation of promoted final/reference copies
- Studio specialist profiles, Krea 2 tooling, and optional Grok backup profile
- Single-instance launcher, status command, and graceful process cleanup

### Security

- Exact project/media path validation and symlink escape rejection
- Trusted-host validation, cross-origin write rejection, and fixed loopback launcher arguments
- Atomic non-overwriting upload/review and hidden generation publication
- Private runtime directory/database permissions and safer secret ignore defaults
- Transactional SQLite job/chat/session/activity state
- Updated `python-multipart` to 0.0.32 for the 2026 multipart parser advisories
- Stale specialist recovery cannot trigger Studio-owned ComfyUI cancellation

### Fixed

- Scheduler retries transient store errors instead of silently stopping
- Parallel same-tool results retain their Hermes tool-call association
- Non-finite generation settings and missing selected model files block readiness
- Failed settings loads cannot submit stale form values
- Generation runners no longer publish incomplete or empty archives
- Custom orchestrator profile defaults and optional Grok profile setup work as documented

### Preview limitations

- Localhost only; no authentication or multi-user mode
- No direct typed Generate button yet (generation is requested through chat)
- Fixed desktop-first three-pane layout
- Linux, Hermes Agent, ComfyUI, models, and workflows must be installed separately

[Unreleased]: https://github.com/sradetzky/hermes-studio/compare/v0.1.0-preview.3...HEAD
[0.1.0-preview.3]: https://github.com/sradetzky/hermes-studio/compare/v0.1.0-preview.2...v0.1.0-preview.3
[0.1.0-preview.2]: https://github.com/sradetzky/hermes-studio/compare/v0.1.0-preview.1...v0.1.0-preview.2
[0.1.0-preview.1]: https://github.com/sradetzky/hermes-studio/releases/tag/v0.1.0-preview.1
