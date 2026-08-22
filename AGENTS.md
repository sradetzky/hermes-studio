# AGENTS.md — hermes-studio

Entry point for any agent (human or LLM) working in this repo. **Keep updated**
as work proceeds. Detailed docs live in `docs/` — this file stays a lean map.

## What this is

Hermes Studio: fully local, agent-orchestrated creative studio.
Hermes profiles orchestrate → ComfyUI (`~/ComfyUI`, RTX 5060 Ti 16GB) renders
video (MiniMax H3) and stills (Krea 2) → filesystem (`studio-root/`) is the
database → thin FastAPI web UI (Phase 3 M1–M3 working).

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

## Next steps

- [ ] Real end-to-end H3 video generation through comfyui-mcp
- [ ] Web UI M4 polish: upload endpoint (drag-drop), generation filters
- [ ] Wire orchestrator → subagent dispatch (design-studio skill)
