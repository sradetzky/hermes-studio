---
name: design-studio
description: Use when working in the hermes-studio repo — creating studio projects, writing H3 prompts to disk, or running/archiving MiniMax H3 generations via ComfyUI.
---

# design-studio skill

Manages the on-disk project structure for the Hermes Studio and
runs H3 generations through the proven `minimax-h3-run` runner.

## Repo & Paths

- Repo: `~/repos/hermes-studio/` (see `PLAN.md` + `AGENTS.md` there)
- Core tool: `~/repos/hermes-studio/scripts/design_studio.py` (library + CLI)
- Studio root: `$DESIGN_STUDIO_ROOT` or `~/repos/hermes-studio/studio-root/`
- H3 runner: `~/.hermes/skills/minimax-h3-run/scripts/run_h3.py`
  (handoff JSONs resolve with fallback to `~/Documents/MinimaxH3/`)
- ComfyUI root: `~/ComfyUI` (API at `http://127.0.0.1:8188`)

## Folder contract (source of truth — never invent structure)

```
<root>/projects/YYYY-MM-DD_<name>/
  brief.md  chat.jsonl  current_prompt.txt
  references/   generations/NNN/{video.mp4,prompt.txt,meta.json}   final/
<root>/shared/{characters,styles,workflows}/   <root>/tmp/
```

## CLI (test each step before wiring the web UI)

```bash
python3 ~/repos/hermes-studio/scripts/design_studio.py create-project <name> "brief..."
python3 ~/repos/hermes-studio/scripts/design_studio.py list-projects
python3 ~/repos/hermes-studio/scripts/design_studio.py write-prompt <name> "<structured prompt>"
python3 ~/repos/hermes-studio/scripts/design_studio.py append-chat <name> user "..."
# generation (blocking; archives to generations/NNN/ automatically):
python3 ~/repos/hermes-studio/scripts/design_studio.py generate <name> \
  --handoff h3_handoff_<slug>.json --arg --mp --arg 0.9 --arg --steps --arg 20
# always smoke new param combos with --dry-run first
```

Project resolution accepts exact folder name, bare name, or unique suffix
(`smoke-test` → `2026-08-22_smoke-test`).

## Generation rules

- `generate` calls `run_h3.py` with the project's handoff, then archives the
  produced video + prompt + `meta.json` into the next `generations/NNN/`.
- Do NOT auto-extract `preview.jpg`; the user reviews renders themselves.
- User deletes broken renders himself — missing/colliding outputs are expected;
  report output path + prompt_id and move on.
- Proven canvas: ~1MP max (e.g. 736x1344 / 1280x704). 1088x1920 OOMs on the
  RTX 5060 Ti 16GB even with int8 UNET.
- Long multi-clip stories: run as sequential background chain jobs, each clip
  anchored on the previous last frame (see `minimax-h3-run` skill).

## Prompting

Always official H3 structure (see `minimax-h3-prompt` skill):
- T2VA/I2VA/FL2VA/L2VA: `integrated_multimodal_description` +
  `overall_soundscape` + `non_diegetic_music` (+ frame alignment when refs).
- Ref2VA: six-section format with explicit reference roles.
- Write the structured prompt to `current_prompt.txt` before generating.

## Future (Phase 3+)

FastAPI web UI reads this same tree; UI never writes. See PLAN.md Phase 3.
