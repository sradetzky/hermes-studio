---
name: design-studio
description: Use when working in the hermes-studio repo — creating studio projects, writing H3 prompts to disk, or running/archiving MiniMax H3 generations via ComfyUI.
---

# design-studio skill

Manages the on-disk project structure for Hermes Studio. The supervised web
generation worker exclusively owns production H3 execution through pinned MCP
tooling. Hermes profiles prepare creative inputs and never invoke or duplicate
that transaction. Python render commands remain explicit operator diagnostics
outside web chat; they are not normal Studio transport.

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

## Generation ownership guard

- A web chat request is never render authorization. Profiles may create or edit
  prompts and settings, then direct the user to **Generate with this prompt**.
- The typed Generate action snapshots the exact prompt, settings, timing,
  references, canvas, and archive sequence. The supervised worker alone builds
  the graph, uploads references, submits, waits, archives authoritative history,
  and performs queue/VRAM cleanup.
- Web profile toolsets intentionally exclude ComfyUI/MCP. Never bypass that
  boundary through terminal render scripts, raw REST, curl, another profile, or
  hand-transcribed graph/tool arguments.
- Prompt edits intentionally stale saved settings. Never rewrite the manifest to
  make a request pass; the user must review and save it again.
- Specialist output never starts a render. Only the explicit typed Generate
  action can enqueue the worker, and the worker serializes all web GPU work.
- Manual `generate`/`generate-image`, runner `--dry-run`, MCP submission, and
  archive commands are operator diagnostics outside web chat. Run them only when
  the user explicitly requests that manual path; they are not a fallback for a
  failed web job.

The web server's `/queue` projection is sanitized and read-only. Profiles do not
call it or any mutating ComfyUI endpoint.

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
references, canvas, and execution knobs. The deterministic worker revalidates
both clip files immediately before submission and aborts without queueing on any
token mismatch.

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
tool use, and lifecycle into the parent job's activity feed. Profiles never
queue ComfyUI; only the deterministic worker owns web H3 execution. Use the reviewer only when the
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
