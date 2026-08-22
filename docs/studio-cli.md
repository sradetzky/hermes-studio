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

Projects resolve by exact folder name, bare name, or unique suffix
(`smoke-test` finds `2026-08-22_smoke-test`).

## Commands

```bash
python3 scripts/design_studio.py create-project <name> "brief..."
python3 scripts/design_studio.py list-projects
python3 scripts/design_studio.py write-prompt <name> "<structured prompt>"
python3 scripts/design_studio.py append-chat <name> <role> "content"

# H3 video generation (blocking; wraps minimax-h3-run runner)
python3 scripts/design_studio.py generate <name> \
  --handoff h3_handoff_<slug>.json \
  --arg=--mp --arg=0.9 --arg=--steps --arg=20 [--dry-run]

# Krea 2 still images (blocking; see docs/image-pipeline.md for recipes)
python3 scripts/design_studio.py generate-image <name> \
  --recipe t2i --prompt "..." --arg=--aspect --arg=16:9
```

## Behaviour notes

- `generate` / `generate-image` archive automatically into the next numbered
  `generations/NNN/`; meta.json carries seed + prompt_id + params
- No auto frame extraction or preview generation — user reviews renders
- Missing/colliding output files are expected (user deletes broken renders);
  report path + prompt_id and move on
- Always smoke new parameter combos with `--dry-run` first
