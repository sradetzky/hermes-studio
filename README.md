# Hermes Studio

Hermes Studio is a fully local, agent-orchestrated creative workspace for
planning, generating, and reviewing MiniMax H3 video and Krea 2 still images.
Hermes profiles handle creative direction and specialist handoffs, ComfyUI owns
GPU execution, and the project filesystem remains the media source of truth.

> **Preview status:** the planning, settings, activity, and media-review workflow
> is operational and locally tested on Linux with an RTX 5060 Ti 16 GB. The web
> UI is localhost-only and has no application auth. Ready clips can be generated
> through an explicit revision-guarded action, and the real web-to-ComfyUI H3
> path is verified end to end. Models and ComfyUI workflows are not bundled.

## What works

- Folder-backed projects with ordered clips; references stay shared while
  execution chat, prompts, settings, immutable takes, and selection are clip-local
- Explicit cross-clip Project chat plus independent per-clip Hermes sessions,
  with serialized global job execution
- Live profile reasoning summaries, tool activity, handoffs, and job status
- Safe multi-file reference uploads with atomic non-overwriting publication
- Typed prompt-bound H3 generation settings with readiness/staleness validation
- Simplified mode, canvas, seed, steps, and exact fused-modulation/ChunkFF
  acceleration settings; prompt text owns clip length and ordered references
- Media/recipe/review filters and a full generation detail viewer
- Promote-to-final and use-as-reference actions with provenance
- Guarded media routes, symlink/path traversal protection, lifecycle cleanup,
  stale-worker recovery, and a single-instance launcher

## Architecture

```text
Browser (FastAPI + vanilla ES modules + local Tailwind CSS)
  → transactional SQLite jobs/chat/activity
  → Hermes profile `studio` and allowlisted specialists
  → pinned comfyui-mcp
  → local ComfyUI
  → studio-root/projects/<project-id>/clips/<clip-id>/ (media source of truth)
```

`PLAN.md` is the architecture decision record. `AGENTS.md` is the concise repo
map and contributor contract; detailed operational docs live under `docs/`.

## Requirements

- Linux (the launcher uses `flock` and `/proc`)
- Python 3.11+ and `venv`
- Hermes Agent with the Studio profiles configured
- The external `minimax-h3-run` Hermes skill installed for legacy/manual H3 runs
- Node.js/npm only when rebuilding the committed CSS bundle
- A running local ComfyUI installation with the required H3/Krea models
- Optional: xAI OAuth for the isolated `studio-grok` backup profile

This repository does **not** download or redistribute model weights.

## Quick start

### 1. Install Python dependencies

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

### 2. Create Hermes profiles

Create `studio`, then create or clone the specialist profiles listed in
`docs/agents.md`:

```bash
hermes profile create studio --clone
hermes profile create studio-storyboarder --clone-from studio
hermes profile create studio-prompt-engineer --clone-from studio
hermes profile create studio-reviewer --clone-from studio
hermes profile create studio-illustrator --clone-from studio
```

The optional cloud backup uses a separate profile:

```bash
hermes profile create studio-grok --clone
```

Review the examples under `hermes/profiles/*/config.yaml.example`. In particular,
replace machine-specific ComfyUI paths and configure your model/provider. Then
deploy the repo-owned SOUL and skill files:

```bash
scripts/sync-profiles.sh
scripts/sync-profiles.sh --check
```

### 3. Configure the studio root

The default is the repo's `studio-root/`. Override it when desired:

```bash
export DESIGN_STUDIO_ROOT="$HOME/design-studio"
```

### 4. Start the web UI

```bash
./webapp/run.sh
./webapp/status.sh
```

Open <http://127.0.0.1:8788>.

Stop it gracefully with:

```bash
./webapp/stop.sh
```

The stop command refuses while a job is queued or running. After deliberately
deciding to cancel active work, use `./webapp/stop.sh --force`; Studio then
terminates its tracked Hermes process, cancels ComfyUI work, and clears VRAM.

The server intentionally remains bound to localhost. For persistent startup and
tailnet-only HTTPS access, install the included user service and allowlist this
machine's exact Tailscale DNS name:

```bash
./webapp/stop.sh
install -Dm644 webapp/hermes-studio.service \
  "$HOME/.config/systemd/user/hermes-studio.service"
install -d -m700 "$HOME/.config/hermes-studio"
printf '%s\n' 'HERMES_STUDIO_TRUSTED_HOSTS=<machine>.<tailnet>.ts.net' \
  > "$HOME/.config/hermes-studio/environment"
chmod 600 "$HOME/.config/hermes-studio/environment"
systemctl --user daemon-reload
systemctl --user enable --now hermes-studio.service
sudo tailscale serve --bg --yes --https=8443 http://127.0.0.1:8788
```

Open `https://<machine>.<tailnet>.ts.net:8443/` from a device permitted by the
tailnet ACL. Standard alternate HTTPS port 8443 avoids replacing any existing
Serve handler on port 443. Do not use Tailscale Funnel: Studio has no separate
application login, so every tailnet identity allowed to reach this device is
trusted with its project, media, and write APIs.

Inspect or remove the exposure with:

```bash
tailscale serve status
sudo tailscale serve --https=8443 off
systemctl --user disable --now hermes-studio.service
```

## Development

Run the complete test suite:

```bash
.venv/bin/python -m unittest discover -s tests
node --test tests/test_frontend_contracts.mjs tests/test_frontend_dom.mjs
```

Compile-check Python and JavaScript:

```bash
python -m compileall -q webapp scripts tests
for file in webapp/static/*.js webapp/static/*.mjs; do node --check "$file"; done
```

Rebuild the committed stylesheet after HTML/JS utility-class changes:

```bash
scripts/build-web-css.sh
git diff --exit-code -- webapp/static/studio.css
```

Runtime data, project media, `.venv/`, bytecode, and `.runtime/` are ignored.
Never commit credentials, live profile configs, model files, project media, or
reference images.

## Repository map

| Path | Purpose |
|---|---|
| `PLAN.md` | Architecture decisions, scope, and milestones |
| `AGENTS.md` | Contributor entry point and operational conventions |
| `docs/` | Agent, frontend, CLI, image/video, MCP, and backup-profile docs |
| `hermes/profiles/` | Repo-owned SOUL files and safe config examples |
| `hermes/skills/` | Studio, H3/Krea, and Grok workflow skills |
| `scripts/design_studio.py` | Project CLI, specialist dispatch, and generation archiving |
| `scripts/krea2_image.py` | Legacy/manual Krea 2 runner |
| `webapp/` | FastAPI application, runtime stores, process manager, and frontend |
| `tests/` | Route, storage, concurrency, lifecycle, and CLI tests |
| `studio-root/` | Ignored local project/media root skeleton |

## Safety boundaries

- Only the `studio` profile owns the ComfyUI queue and GPU execution.
- One job runs globally at a time; specialist handoffs are serialized.
- Generation settings are prompt-hash-bound and must be re-approved after edits.
- **Generate with this prompt** submits a dedicated Studio generation job only
  for an enabled, ready clip; prompt and settings revisions are checked again
  when queued work starts before any ComfyUI submission.
- Upload, media, promotion, and reference paths reject traversal and symlink escapes.
- Trusted-host and origin checks allow localhost plus only explicitly configured
  exact Tailscale DNS names; wildcard tailnet hosts and host:port values are rejected.
- Review actions copy rather than move and never overwrite an existing file.
- Generation workflows must archive output and release VRAM after terminal state.
- The user remains the final judge of generated media; automated deletion is forbidden.
- Prompts are passed to local Hermes/runner processes as command arguments; do not
  include credentials or other secrets in Studio prompts.

## Preview limitations

- No application authentication or multi-user isolation; tailnet access relies
  on Tailscale identity and ACL policy
- No mobile-first replacement for the fixed three-pane workspace
- No packaged installer or model/workflow downloader
- No guarantee outside the documented local Linux/ComfyUI setup

## License

[MIT](LICENSE)
