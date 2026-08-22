---
name: design-studio
description: Use when working in the hermes-studio repo — creating studio projects, writing H3 prompts to disk, or running/archiving MiniMax H3 generations via ComfyUI.
---

# design-studio skill

Manages the on-disk project structure for Hermes Studio. Production ComfyUI
execution goes through the `comfyui` MCP server owned by the `studio` profile.
The Python runners build proven graphs and remain explicit legacy fallbacks;
they are not the normal Studio transport.

## Repo & Paths

- Repo: `~/repos/hermes-studio/` (see `PLAN.md` + `AGENTS.md` there)
- Core tool: `~/repos/hermes-studio/scripts/design_studio.py` (library + CLI)
- Studio root: `$DESIGN_STUDIO_ROOT` or `~/repos/hermes-studio/studio-root/`
- H3 graph builder: `~/.hermes/skills/minimax-h3-run/scripts/run_h3.py`
  (handoffs fall back to `~/Documents/MinimaxH3/`)
- Krea 2 graph builder: `~/repos/hermes-studio/scripts/krea2_image.py`
- ComfyUI transport: pinned `comfyui-mcp@0.52.61` on the `studio` profile
- ComfyUI root: `~/ComfyUI`

## Folder contract (source of truth — never invent structure)

```
<root>/projects/YYYY-MM-DD_<name>/
  brief.md  chat.jsonl  current_prompt.txt
  references/   generations/NNN/{video.mp4,prompt.txt,meta.json}   final/
<root>/shared/{characters,styles,workflows}/   <root>/tmp/
```

## Filesystem CLI

```bash
python3 ~/repos/hermes-studio/scripts/design_studio.py create-project <name> "brief..."
python3 ~/repos/hermes-studio/scripts/design_studio.py list-projects
python3 ~/repos/hermes-studio/scripts/design_studio.py write-prompt <project-id> "<structured prompt>"
python3 ~/repos/hermes-studio/scripts/design_studio.py append-chat <project-id> user "..."
# after an MCP job completes:
python3 ~/repos/hermes-studio/scripts/design_studio.py archive-output <project-id> \
  <comfy-output-file> --prompt-id <id> --kind image --recipe krea2-edit
```

After creation, pass the exact project folder id returned by the command
(`2026-08-22_smoke-test`). Fuzzy/suffix matching is intentionally unsupported
so output can never land in an ambiguously matched project.

## MCP generation transaction (mandatory)

Only the `studio` orchestrator may execute these steps. Never run two GPU jobs
concurrently.

1. Write `current_prompt.txt` and build/inspect the API graph with the relevant
   runner's `--dry-run` mode. Dry-run must not queue a job.
2. Upload references through `mcp_comfyui_upload_image`; patch the graph with
   the returned server filenames.
3. Optionally call `mcp_comfyui_clear_vram` before switching model families.
4. Submit with `mcp_comfyui_enqueue_workflow`; retain the `prompt_id`.
5. Wait through `mcp_comfyui_queue` / `mcp_comfyui_get_history` until success,
   error, or timeout. Never start another job while one is running.
6. On success, archive output with `design_studio.py archive-output`.
7. **Finally, always call `mcp_comfyui_clear_vram`** with model unload and
   memory free enabled — after success, error, cancellation, or timeout.
8. On timeout/error, cancel through `mcp_comfyui_queue` first, verify the job
   stopped, then clear VRAM. Killing a wrapper process does not cancel ComfyUI.

Do not use raw REST, curl, `/prompt`, `/history`, `/upload`, or `/free` during
normal Studio work. If MCP is unavailable, stop with a clear error; do not
silently fall back to REST.

The legacy `generate` and `generate-image` CLI commands remain for manual
diagnostics only. They explicitly clean VRAM, and timeout paths interrupt the
ComfyUI job before cleanup.

## Output rules

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
