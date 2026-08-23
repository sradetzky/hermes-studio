# Studio CLI — design_studio.py

Project management + generation archiving. Root resolution: `--root` flag →
`$DESIGN_STUDIO_ROOT` → repo's `studio-root/`.

## Folder contract

```
<root>/projects/YYYY-MM-DD_<name>/
├── project.json           # ordered clip ids, titles, enabled/selected take state
├── brief.md               # project-shared
├── chat.jsonl             # project-shared {role, content, ts} per line
├── references/            # project-shared uploaded assets
├── research/              # project-shared specialist notes
├── clips/clip-001/
│   ├── current_prompt.txt
│   ├── current_generation.json # settings + prompt SHA-256
│   └── generations/NNN/   # media + prompt/settings/meta snapshots
└── final/
```

After creation, all commands require the exact folder id returned by
`create-project` (for example `2026-08-22_smoke-test`). There is no fuzzy
suffix matching: ambiguity must never send output into the wrong project.

## Commands

```bash
python3 scripts/design_studio.py create-project <name> "brief..."
python3 scripts/design_studio.py list-projects
python3 scripts/design_studio.py list-clips <project-id>
python3 scripts/design_studio.py create-clip <project-id> "Closing scene"
python3 scripts/design_studio.py update-clip <project-id> <clip-id> \
  --title "Finale" --disable
python3 scripts/design_studio.py reorder-clips <project-id> clip-002 clip-001
python3 scripts/design_studio.py select-take <project-id> <clip-id> \
  <generation-id> <video-file>
python3 scripts/design_studio.py write-prompt <project-id> <clip-id> \
  "<structured prompt>"
python3 scripts/design_studio.py append-chat <project-id> <role> "content"

# Explicit legacy project-layout transition
python3 scripts/design_studio.py migrate-clips --dry-run [project-id]
python3 scripts/design_studio.py migrate-clips --apply [project-id]

# Grok 4.6 backup research / X search / explicitly requested Imagine work
python3 scripts/design_studio.py dispatch-grok <project-id> "<task>"

# Archive an accepted Grok Imagine cache result
python3 scripts/design_studio.py archive-grok <project-id> <clip-id> <image-path> \
  --meta-json '{"prompt":"..."}'

# Archive completed comfyui-mcp output (production path)
python3 scripts/design_studio.py archive-output <project-id> <clip-id> \
  <comfy-output-file> --prompt-id <id> --kind image --recipe krea2-edit

# Legacy direct H3 generation (manual diagnostics only)
python3 scripts/design_studio.py generate <project-id> <clip-id> \
  --handoff h3_handoff_<slug>.json \
  --arg=--mp --arg=0.9 --arg=--steps --arg=20 [--dry-run]

# Legacy direct Krea 2 execution (manual diagnostics only)
python3 scripts/design_studio.py generate-image <project-id> <clip-id> \
  --recipe t2i --prompt "..." --arg=--aspect --arg=16:9
```

## Behaviour notes

- For the configured Studio root, `append-chat` writes the transactional SQLite
  chat store first and atomically refreshes derived `chat.jsonl`; ad-hoc project
  roots retain the standalone locked JSONL fallback
- Production jobs run through comfyui-mcp; `archive-output` copies one or more
  completed files into the exact clip's next `generations/NNN/` and snapshots
  its prompt, settings, and MCP metadata
- Legacy `generate` / `generate-image` remain explicit diagnostics; they clean
  VRAM after completion and interrupt before cleanup on timeout
- No auto frame extraction or preview generation — user reviews renders
- Missing/colliding output files are expected (user deletes broken renders);
  report path + prompt_id and move on
- Always smoke new parameter combos with `--dry-run` first
- The web settings editor owns each clip's `current_generation.json`; editing
  that clip's `current_prompt.txt` makes its readiness stale until the user reviews and
  saves the settings again. Prompt text owns the 4–15 second clip length and
  ordered `<Picture N> (filename.ext)` reference mapping.
