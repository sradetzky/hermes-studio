# AGENTS.md — hermes-studio

Working notes for any agent (human or LLM) touching this repo. **Keep this file
updated** with structure and progress as work proceeds. PLAN.md holds decisions;
this file holds current state.

## What this is

DIY MiniMax Design Studio: fully local, agent-orchestrated creative studio.
Hermes profile `studio` orchestrates → ComfyUI (native MiniMax H3 nodes,
`~/ComfyUI`) renders video+audio → filesystem (`studio-root/`) is the database
→ thin FastAPI web UI (Phase 3, not started).

## Structure

```
hermes-studio/
├── PLAN.md                    # single source of truth for decisions
├── AGENTS.md                  # this file — current state & conventions
├── README.md                  # skeleton readme (update as phases land)
├── hermes/                    # copies of live Hermes profile assets (source of truth for them)
│   ├── profiles/studio/SOUL.md            # studio agent personality (deployed to ~/.hermes/profiles/studio/)
│   ├── profiles/studio/config.yaml.example
│   └── skills/design-studio/SKILL.md      # deployed to ~/.hermes/profiles/studio/skills/
├── scripts/
│   ├── design_studio.py       # core library + CLI (projects, prompts, chat, generation archiving)
│   └── switch-model.sh        # placeholder reminder for local-server model switching
├── comfyui/workflows/         # parameterized H3 API-format workflow JSONs (empty yet)
├── studio-root/               # DEFAULT_STUDIO_ROOT: projects/, shared/{characters,styles,workflows}/, tmp/
└── .gitignore
```

## External systems (do NOT commit their contents)

- `~/.hermes/profiles/studio/` — live studio profile (cloned from default
  2026-08-22; SOUL.md + design-studio skill deployed from `hermes/` copies)
- `~/ComfyUI` — ComfyUI install, RTX 5060 Ti 16GB, API :8188
- `~/Documents/MinimaxH3/` — durable archive of H3 prompts/handoffs
- `~/.hermes/skills/minimax-h3-{prompt,run}/` — proven H3 skills; the studio
  reuses their runner instead of reimplementing generation

## Conventions

- Studio root override: `$DESIGN_STUDIO_ROOT` env var (defaults to repo's
  `studio-root/`)
- Project folders: `projects/YYYY-MM-DD_<name>/`; resolve by bare name or
  suffix — never hardcode dates
- Generation archiving: `generations/NNN/{video.mp4,prompt.txt,meta.json}`;
  no auto frame extraction (user reviews renders personally)
- Test new param combos with `--dry-run` before real GPU runs

## Progress log

### 2026-08-22 — Phase 0 + Phase 1 complete
- git init on `main`; skeleton from grok committed as baseline
- Verified environment: ComfyUI up (:8188), run_h3.py working, MinimaxH3
  archive present
- Created Hermes profile `studio` (`hermes profile create studio --clone`),
  deployed SOUL.md + config example from repo copies
- Implemented `scripts/design_studio.py`: create-project / list / write-prompt /
  append-chat / generate (wraps run_h3.py, archives to generations/NNN/,
  handoff resolves via ~/Documents/MinimaxH3 fallback). Smoke-tested:
  project create/prompt/chat OK; generate --dry-run builds full ComfyUI graph
- Rewrote `hermes/skills/design-studio/SKILL.md` with real implementation and
  installed into studio profile

## Next steps

- [ ] Phase 1 final check: run a real generation through `design_studio.py
      generate` end-to-end (blocking GPU job)
- [ ] Phase 2: export clean parameterized API workflows into
      `comfyui/workflows/` (T2VA / FL2VA / R2V) — currently runner builds
      graphs itself, so decide whether workflows dir stays canonical or optional
- [ ] Phase 3: FastAPI web UI (read-only over studio-root, `/api/chat` streams
      to studio profile)
