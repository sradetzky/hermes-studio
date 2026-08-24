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

- `$HERMES_HOME` is the active profile root; use it for Hermes skills and state.
- `$HERMES_REAL_HOME` is the OS account home; use it for repos, ComfyUI, and
  Documents. Never derive either root from `$HOME` or `~`, which Hermes may
  isolate under a profile.
- Repo: `$HERMES_REAL_HOME/repos/hermes-studio/` (see `PLAN.md` + `AGENTS.md`)
- Core tool: `$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py`
  (library + CLI)
- Studio root: `$DESIGN_STUDIO_ROOT` or
  `$HERMES_REAL_HOME/repos/hermes-studio/studio-root/`
- H3 graph builder: `$HERMES_HOME/skills/minimax-h3-run/scripts/run_h3.py`
  (handoffs fall back to `$HERMES_REAL_HOME/Documents/MinimaxH3/`)
- Krea 2 graph builder:
  `$HERMES_REAL_HOME/repos/hermes-studio/scripts/krea2_image.py`
- ComfyUI transport: pinned `comfyui-mcp@0.52.61` on the `studio` profile
- ComfyUI root: `$HERMES_REAL_HOME/ComfyUI`

## Folder contract (source of truth — never invent structure)

```
<root>/projects/YYYY-MM-DD_<name>/
  project.json  brief.md  chat.jsonl  references/  research/  final/
  clips/clip-001/{chat.jsonl,current_prompt.txt,current_generation.json}
  clips/clip-001/generations/NNN/{video.mp4,prompt.txt,settings.json,meta.json}
<root>/shared/{characters,styles,workflows}/   <root>/tmp/
```

## Filesystem CLI

```bash
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" create-project <name> "brief..."
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" list-projects
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" list-clips <project-id>
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" create-clip <project-id> "<title>"
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" write-prompt \
  <project-id> <clip-id> "<structured prompt>"
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" append-chat <project-id> user "..."
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" append-chat \
  <project-id> user "..." --clip <clip-id>
# after an MCP job completes:
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" archive-output \
  <project-id> <clip-id> <comfy-output-file> \
  --prompt-id <id> --kind image --recipe krea2-edit
```

After creation, pass the exact project folder id returned by the command
(`2026-08-22_smoke-test`). Fuzzy/suffix matching is intentionally unsupported
so output can never land in an ambiguously matched project.

## MCP generation transaction (mandatory)

Only the `studio` orchestrator may execute these steps. Never run two GPU jobs
concurrently.

1. Resolve the exact project and clip, write that clip's `current_prompt.txt`,
   and build/inspect the API graph with the relevant
   runner's `--dry-run` mode. Dry-run must not queue a job.
   For a web generation request, the job query already contains the exact
   shell-quoted dry-run command and output JSON path in its mandatory execution
   tail. Keep that tail as the active task after every tool result. Run the
   command exactly once with its stdout suppression intact. Do not read its graph
   JSON into model context. Never read, search, or reverse-engineer `run_h3.py`.
2. For a web generation request, run the exact `submit_h3_graph_mcp.py` command
   from the mandatory tail. Never call upload or batch-submit tools yourself and
   never transcribe a graph/prompt into tool arguments. The helper serially
   uploads refs, revalidates the contract, and submits exact graph bytes through
   pinned MCP tooling. Read only its compact result JSON.
3. For a manual/non-web run only, upload references serially in prompt order,
   patch returned filenames into the graph, and optionally call
   `mcp_comfyui_clear_vram` before switching model families.
4. For a manual/non-web run only, submit exactly one graph through
   `mcp_comfyui_batch` with
   `action:"submit"`, `workflows:[graph]`, and `disable_random_seed:true`.
   Retain the returned `batch_id` and `prompt_id`. The explicit seed guard is
   mandatory because batch submission otherwise randomizes seed widgets.
5. Call `mcp_comfyui_batch` with `action:"wait"`, that `batch_id`, and
   `timeout_s:600`. This is the notification-style wait: it checks status
   internally every two seconds and returns immediately when the prompt is
   terminal. If its bounded ten-minute safety cap expires while the job remains
   pending/running, call the same wait action again. Never approximate waiting
   with `sleep`, terminal timeout calls, or manually spaced queue/history polls.
   Never start another job while one is running.
6. On success, archive output with `design_studio.py archive-output`, the exact
   `prompt_id`, and the same exact project + clip IDs. Web Generate jobs derive
   seed, canvas, frames, FPS, steps, acceleration nodes, ordered references, and
   prompt hash from authoritative ComfyUI history at the archive boundary; an
   incomplete or mismatched archive must fail rather than publish partial metadata.
   If the configured ComfyUI root is itself a
   symlink and the safe-filesystem guard rejects it, do not copy manually:
   import `scripts/design_studio.py` and call `archive_outputs(...)` with the
   canonical real output directory as `source_root` plus a relative output
   filename. This preserves the same descriptor-safe archive transaction.
7. **Finally, always call `mcp_comfyui_clear_vram`** with model unload and
   memory free enabled — after success, error, cancellation, or timeout.
8. On timeout/error, cancel through `mcp_comfyui_queue` first, verify the job
   stopped, then clear VRAM. Killing a wrapper process does not cancel ComfyUI.

Do not use raw REST, curl, `/prompt`, `/history`, `/upload`, or `/free` during
normal Studio work. The web server's sanitized read-only `/queue` projection is
an observability-only exception; agents do not call it and it cannot mutate
ComfyUI. If MCP is unavailable, stop with a clear error; do not silently fall
back to REST.

Each clip's `current_generation.json` is the web UI's compact typed run contract.
It records mode, canvas/MP, seed, steps, accel, and the SHA-256 of that clip's
`current_prompt.txt`.
The prompt itself owns the 4–15 second length and ordered
`<Picture N> (filename.ext)` mapping. Accel means Sol fused modulation + ChunkFF
only—never Sage, sparse Sol attention, or EasyCache. Do not silently rewrite the
manifest from agent prose. A prompt edit intentionally makes the UI show stale
settings; the user must review and save the panel again before the web Generate
action is allowed. A Generate click is explicit render authorization. Its Studio
job carries the validated prompt hash, manifest revision, resolved timing,
references, canvas, and execution knobs. Re-read both clip files immediately
before submission and abort without queueing on any token mismatch; never rewrite
the prompt or settings to make a stale request pass.

The legacy `generate` and `generate-image` CLI commands remain for manual
diagnostics only. They explicitly clean VRAM, and timeout paths interrupt the
ComfyUI job before cleanup.

## Grok backup dispatch

Use `studio-grok` for xAI-backed current web research, read-only X/Twitter
search, or Grok Imagine generation/editing:

```bash
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" dispatch-grok \
  <project-id> "<self-contained research or image task>"
```

The dispatcher maintains one Grok session per project or exact clip scope and exposes only
`web,x_search,image_gen,vision,file,terminal`. Preserve citations and clearly
attribute findings to the Grok backup. It is excluded from fleet model
switching and never has comfyui-mcp/GPU ownership.

If Grok returns an accepted local Imagine cache path, archive it with:

```bash
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" archive-grok \
  <project-id> <clip-id> <absolute-image-path> --meta-json '{"prompt":"..."}'
```

Imagine can consume xAI quota: dispatch image generation only for an explicit
user request. Research tasks must not generate images as a side effect.

## Local specialist dispatch

The `studio` orchestrator can run one serialized, persistent handoff to a local
specialist profile:

```bash
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" dispatch-profile \
  <project-id> studio-storyboarder "<self-contained shot-planning task>"
python3 "$HERMES_REAL_HOME/repos/hermes-studio/scripts/design_studio.py" dispatch-profile \
  <project-id> studio-prompt-engineer "Convert storyboard.md into the official H3 prompt"
```

Allowed profiles are `studio-storyboarder`, `studio-prompt-engineer`,
`studio-reviewer`, and `studio-illustrator`. Handoffs are serialized and keep a
independent session for each project or exact clip scope. The web runtime
projects their reasoning,
tool use, and lifecycle into the parent job's activity feed. Specialists never
queue ComfyUI; only `studio` owns GPU execution. Use the reviewer only when the
user explicitly requests agent review—the human remains the final judge.

Dispatch only for an explicit web profile selection or one exact command:
`/handoff storyboarder ...`, `/handoff prompt-engineer ...`,
`/handoff reviewer ...`, or `/handoff illustrator ...`. Do not infer routing
from ordinary language, auto-chain specialists, or start a render from a
specialist result. Specialist subprocesses receive fixed minimal toolsets;
ComfyUI and unrelated tools are not exposed.

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
- Write the structured prompt to the exact active clip's `current_prompt.txt`
  before generating; never guess a clip from a title or path.

## Web contract

The FastAPI UI has explicit Project and Clip chat scopes. Project chat owns
cross-clip planning; every clip chat has an independent transcript, activity
cursor, Studio session, and specialist sessions. Dispatch and `append-chat`
inherit `HERMES_STUDIO_CHAT_SCOPE`/`HERMES_STUDIO_CLIP` inside web jobs; manual
calls can pass `--clip`. References remain project-shared; prompts, settings,
takes, and take selection remain clip-local. See PLAN.md Phase 3.
