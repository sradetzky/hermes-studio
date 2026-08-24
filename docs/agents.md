# Agent Fleet

MiniMax Design-style role split as full Hermes profiles. The local production
fleet is cloned from `studio`; `studio-grok` is a deliberately separate cloud
specialist.

| Profile | Role | Produces | Never does |
|---|---|---|---|
| `studio` | Orchestrator + creative director | project mgmt, generation runs | — |
| `studio-storyboarder` | Shot planning | `storyboard.md` per project | final prompts, renders |
| `studio-prompt-engineer` | H3 prompt writing | structured prompts, handoff params | renders, shot redesign |
| `studio-reviewer` | Quality gate | PASS/REVISE/REJECT verdicts in chat.jsonl | deletes media, rewrites prompts |
| `studio-illustrator` | Still images (Krea 2) | character sheets, style refs, refines | video/H3 work |
| `studio-grok` | Cloud backup (Grok 4.6) | cited web/X research, Grok Imagine images | ComfyUI/GPU, X account actions |

## Dataflow

```
brief → studio-storyboarder → project-shared storyboard.md
      → studio-prompt-engineer → clip/current_prompt.txt (+ length/reference mapping)
      → web settings editor    → clip/current_generation.json (compact render knobs)
      → studio (orchestrator)  → comfyui-mcp → clip take archive → clear_vram
      → studio-grok (optional) → xAI web/X/Imagine → research/ or archive-grok
      → studio-reviewer        → PASS/REVISE/REJECT appended to chat.jsonl
```

Handoffs carry exact project + clip IDs and travel through the filesystem,
never through chat context. One role per step; escalate problems sideways, not
around. Chat/references stay project-shared; prompt/settings/takes stay clip-local.

## Spawning

```bash
# one-shot
hermes -p studio-storyboarder chat -q "Plan shots for project X: ..."
# long work — background via terminal tool, or tmux for interactive
```

The local Studio orchestrator is configured with `terminal.home_mode: real` so
host paths remain stable even when container detection sees unrelated Docker
mounts. Scripts still use `$HERMES_HOME` for profile data/skills and
`$HERMES_REAL_HOME` for account files; do not construct either from `~`.

## Model switching

The local production fleet shares one model/provider and switches together:

```bash
scripts/switch-model.sh show                      # fleet status
scripts/switch-model.sh openrouter <model>        # apply to all
scripts/switch-model.sh <provider> <model> studio # single profile
```

The FLEET array in switch-model.sh must be kept in sync when adding profiles.
`studio-grok` is deliberately excluded: it stays fixed on Grok 4.6.

## Deploying repo-owned profile files

```bash
scripts/sync-profiles.sh          # deploy changed SOULs + skills
scripts/sync-profiles.sh --check  # CI/read-only drift check (nonzero on drift)
```

The script deploys every local-fleet SOUL plus `design-studio`, deploys
`krea2-images` only to `studio-illustrator`, and deploys the dedicated Grok
SOUL/skill only to `studio-grok`. Never hand-copy these files: the drift check
is the contract.

## Adding a role

1. Create the profile from `studio` for local-fleet roles, or from `default`
   for isolated cloud specialists.
2. Author SOUL at `hermes/profiles/studio-<role>/SOUL.md`, deploy to the
   profile dir
3. Add local-fleet roles to switch-model.sh; keep fixed-model specialists out.
   Add every role to sync-profiles.sh and this table.
4. Run `scripts/sync-profiles.sh` and verify with
   `hermes -p studio-<role> chat -q "Which SOUL role are you?"`

## Camera doctrine (video roles)

Dynamic camera moves are welcome — H3 executes them well. Discipline is the
rule: ONE clear move per shot with explicit path + speed + start/end framing,
stable subject blocking, gradual escalation (push-in → pan/crane → orbit).
Experimental moves get flagged in the storyboard so review knows what to judge.
