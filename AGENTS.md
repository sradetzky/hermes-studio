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
│   ├── profiles/studio-storyboarder/SOUL.md    # deployed to ~/.hermes/profiles/studio-storyboarder/
│   ├── profiles/studio-prompt-engineer/SOUL.md # deployed to ~/.hermes/profiles/studio-prompt-engineer/
│   ├── profiles/studio-reviewer/SOUL.md        # deployed to ~/.hermes/profiles/studio-reviewer/
│   ├── profiles/studio-illustrator/SOUL.md     # deployed to ~/.hermes/profiles/studio-illustrator/
│   ├── skills/design-studio/SKILL.md      # deployed to ~/.hermes/profiles/studio/skills/
│   └── skills/krea2-images/SKILL.md       # deployed to ~/.hermes/profiles/studio-illustrator/skills/
├── scripts/
│   ├── design_studio.py       # core library + CLI (projects, prompts, chat, generation archiving)
│   ├── krea2_image.py         # Krea 2 image runner (t2i / t2i-nvfp4 / style-ref / upscale via ComfyUI API)
│   └── switch-model.sh        # placeholder reminder for local-server model switching
├── comfyui/workflows/         # parameterized H3 API-format workflow JSONs (empty yet)
├── studio-root/               # DEFAULT_STUDIO_ROOT: projects/, shared/{characters,styles,workflows}/, tmp/
└── .gitignore
```

## External systems (do NOT commit their contents)

- `~/.hermes/profiles/` — live profiles: `studio` (orchestrator) + subagent
  fleet `studio-storyboarder`, `studio-prompt-engineer`, `studio-reviewer`
  (all cloned from studio 2026-08-22; SOULs deployed from `hermes/` copies).
  Fleet model switching: `scripts/switch-model.sh <provider> <model>` (all) or
  `... [profile]` (one); `show` lists current state.
- `~/ComfyUI` — ComfyUI install, RTX 5060 Ti 16GB, API :8188
- `~/Documents/MinimaxH3/` — durable archive of H3 prompts/handoffs
- `~/.hermes/skills/minimax-h3-{prompt,run}/` — proven H3 skills; the studio
  reuses their runner instead of reimplementing generation

## Agent fleet (MiniMax Design-style role split)

| Profile | Role | Produces | Never does |
|---|---|---|---|
| `studio` | Orchestrator + creative director | project mgmt, generation runs | — |
| `studio-storyboarder` | Shot planning | `storyboard.md` per project | final prompts, renders |
| `studio-prompt-engineer` | H3 prompt writing | structured prompts, handoff params | renders, shot redesign |
| `studio-reviewer` | Quality gate | PASS/REVISE/REJECT verdicts in chat.jsonl | deletes media, rewrites prompts |
| `studio-illustrator` | Still images (Krea 2) | character sheets, style refs, refines | video/H3 work |

All share one model config (switch together via switch-model.sh). Spawned by
the studio orchestrator via `hermes -p <profile> chat -q ...` (background for
long work). More roles can be added when a need is proven.

## Image pipeline (Krea 2, local ComfyUI)

- Runner: `scripts/krea2_image.py`; projects integrate via
  `design_studio.py generate-image <project> --recipe <r>` → archives into the
  same `generations/NNN/` tree with meta.json + prompt.txt.
- Recipes: `t2i`, `t2i-nvfp4` (darkBeast checkpoints + qwen3-vl-4b-heretic +
  qwen_image_vae + Krea-2-unlocked LoRA), `style-ref` (+ krea2_style_reference),
  `upscale` (grounded img2img @ denoise ~0.35).
- NOT available yet: identity/multi-image edit — needs downloads:
  `krea2_turbo_fp8_scaled.safetensors`, `qwen3vl_4b_fp8_scaled.safetensors`,
  LoRA `Krea2/krea2_identity_edit_v1_2.safetensors`. UI workflows referencing
  them live in ~/ComfyUI/user/default/workflows/krea2/.
- Krea2ImageNode/Krea2StyleReferenceNode are Comfy cloud API nodes (paid) —
  not used by the studio; everything runs local.

## Conventions

- Studio root override: `$DESIGN_STUDIO_ROOT` env var (defaults to repo's
  `studio-root/`)
- Project folders: `projects/YYYY-MM-DD_<name>/`; resolve by bare name or
  suffix — never hardcode dates
- Generation archiving: `generations/NNN/{video.mp4,prompt.txt,meta.json}`;
  no auto frame extraction (user reviews renders personally)
- Test new param combos with `--dry-run` before real GPU runs

## Progress log

### 2026-08-22 — Phase 0 + Phase 1 complete; subagent fleet created
- git init on `main`; skeleton from grok committed as baseline
  (author: Sven Radetzky <sven.radetzky@gmx.de>)
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
- Created subagent fleet (full profiles cloned from studio): studio-storyboarder,
  studio-prompt-engineer, studio-reviewer — role SOULs written in-repo and
  deployed; each verified live via `hermes -p <profile> chat -q`
- switch-model.sh rewritten: manages whole fleet's model/provider at once

### 2026-08-22 — Image pipeline (Krea 2) + illustrator agent
- Audited local Krea 2 assets: darkBeast int8 + nvfp4 checkpoints, heretic
  qwen3-vl-4b CLIP, qwen_image_vae, LoRAs (unlocked/style-ref/nsfw) all present;
  identity-edit models NOT downloaded yet; native Krea2ImageNode = cloud API (unused)
- Implemented `scripts/krea2_image.py` (t2i / t2i-nvfp4 / style-ref / upscale);
  verified live end-to-end: t2i-nvfp4 render + upscale refine both completed on GPU
- Added `generate-image` to design_studio.py — archives into the same
  generations/NNN/ tree (meta.json + prompt.txt + files); verified with a real
  render archived into project image-test/generations/001/
- New profile `studio-illustrator` (SOUL in repo, krea2-images skill deployed);
  verified live via one-shot chat

## Next steps

- [ ] Phase 1 final check: run a real generation through `design_studio.py
      generate` end-to-end (blocking GPU job)
- [ ] Wire the fleet: teach the studio orchestrator (design-studio skill) to
      dispatch storyboard/prompt/review steps to subagent profiles via
      `hermes -p <profile> chat -q ...`
- [ ] Phase 2: export clean parameterized API workflows into
      `comfyui/workflows/` (T2VA / FL2VA / R2V) — currently runner builds
      graphs itself, so decide whether workflows dir stays canonical or optional
- [ ] Phase 3: FastAPI web UI (read-only over studio-root, `/api/chat` streams
      to studio profile)
