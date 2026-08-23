# Changelog

All notable changes to Hermes Studio are documented here.

The project follows [Semantic Versioning](https://semver.org/) for tagged
preview and production releases.

## [Unreleased]

### Changed

- Simplified H3 settings to mode, canvas, seed, steps, and acceleration; clip
  length and ordered reference filenames are now parsed from the prompt
- Removed SeedVR2, turbo, W4A8, model, and reference controls from the dialog
- Acceleration now uses only Sol fused modulation and ChunkFF, without Sage,
  sparse Sol attention, or EasyCache

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
- Real web-to-ComfyUI H3 generation has not yet been verified end to end
- Fixed desktop-first three-pane layout
- Linux, Hermes Agent, ComfyUI, models, and workflows must be installed separately

[Unreleased]: https://github.com/sradetzky/hermes-studio/compare/v0.1.0-preview.1...HEAD
[0.1.0-preview.1]: https://github.com/sradetzky/hermes-studio/releases/tag/v0.1.0-preview.1
