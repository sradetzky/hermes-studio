# Studio CLI — design_studio.py

Project management + generation archiving. Root resolution: `--root` flag →
`$DESIGN_STUDIO_ROOT` → repo's `studio-root/`.

## Folder contract

```
<root>/projects/YYYY-MM-DD_<name>/
├── brief.md               # created with project
├── chat.jsonl             # {role, content, ts} per line
├── current_prompt.txt     # latest structured prompt
├── current_generation.json # UI-validated H3 run settings + prompt SHA-256
├── references/            # uploaded assets (identity refs etc.)
├── research/              # durable specialist research notes
├── generations/NNN/       # video.mp4|*.png + prompt.txt + meta.json
└── final/
```

After creation, all commands require the exact folder id returned by
`create-project` (for example `2026-08-22_smoke-test`). There is no fuzzy
suffix matching: ambiguity must never send output into the wrong project.

## Commands

```bash
python3 scripts/design_studio.py create-project <name> "brief..."
python3 scripts/design_studio.py list-projects
python3 scripts/design_studio.py write-prompt <project-id> "<structured prompt>"
python3 scripts/design_studio.py append-chat <project-id> <role> "content"

# Grok 4.6 backup research / X search / explicitly requested Imagine work
python3 scripts/design_studio.py dispatch-grok <project-id> "<task>"

# Archive an accepted Grok Imagine cache result
python3 scripts/design_studio.py archive-grok <project-id> <image-path> \
  --meta-json '{"prompt":"..."}'

# Archive completed comfyui-mcp output (production path)
python3 scripts/design_studio.py archive-output <project-id> \
  <comfy-output-file> --prompt-id <id> --kind image --recipe krea2-edit

# Legacy direct H3 generation (manual diagnostics only)
python3 scripts/design_studio.py generate <project-id> \
  --handoff h3_handoff_<slug>.json \
  --arg=--mp --arg=0.9 --arg=--steps --arg=20 [--dry-run]

# Legacy direct Krea 2 execution (manual diagnostics only)
python3 scripts/design_studio.py generate-image <project-id> \
  --recipe t2i --prompt "..." --arg=--aspect --arg=16:9
```

## Behaviour notes

- For the configured Studio root, `append-chat` writes the transactional SQLite
  chat store first and atomically refreshes derived `chat.jsonl`; ad-hoc project
  roots retain the standalone locked JSONL fallback
- Production jobs run through comfyui-mcp; `archive-output` copies one or more
  completed files into the next `generations/NNN/` and writes MCP metadata
- Legacy `generate` / `generate-image` remain explicit diagnostics; they clean
  VRAM after completion and interrupt before cleanup on timeout
- No auto frame extraction or preview generation — user reviews renders
- Missing/colliding output files are expected (user deletes broken renders);
  report path + prompt_id and move on
- Always smoke new parameter combos with `--dry-run` first
- The web settings editor owns `current_generation.json`; editing
  `current_prompt.txt` makes its readiness stale until the user reviews and
  saves the settings again
