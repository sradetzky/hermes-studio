# Studio CLI — design_studio.py

Project management + generation archiving. Root resolution: `--root` flag →
`$DESIGN_STUDIO_ROOT` → repo's `studio-root/`.

## Folder contract

```
<root>/projects/YYYY-MM-DD_<name>/
├── brief.md               # created with project
├── chat.jsonl             # {role, content, ts} per line
├── current_prompt.txt     # latest structured prompt
├── references/            # uploaded assets (identity refs etc.)
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

# H3 video generation (blocking; wraps minimax-h3-run runner)
python3 scripts/design_studio.py generate <project-id> \
  --handoff h3_handoff_<slug>.json \
  --arg=--mp --arg=0.9 --arg=--steps --arg=20 [--dry-run]

# Krea 2 still images (blocking; see docs/image-pipeline.md for recipes)
python3 scripts/design_studio.py generate-image <project-id> \
  --recipe t2i --prompt "..." --arg=--aspect --arg=16:9
```

## Behaviour notes

- `generate` / `generate-image` archive automatically into the next numbered
  `generations/NNN/`; meta.json carries seed + prompt_id + params
- No auto frame extraction or preview generation — user reviews renders
- Missing/colliding output files are expected (user deletes broken renders);
  report path + prompt_id and move on
- Always smoke new parameter combos with `--dry-run` first
