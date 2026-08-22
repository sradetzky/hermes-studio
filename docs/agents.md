# Agent Fleet

MiniMax Design-style role split as full Hermes profiles. All cloned from
`studio` (so they inherit its skills incl. minimax-h3-*), all point at the same
model config.

| Profile | Role | Produces | Never does |
|---|---|---|---|
| `studio` | Orchestrator + creative director | project mgmt, generation runs | — |
| `studio-storyboarder` | Shot planning | `storyboard.md` per project | final prompts, renders |
| `studio-prompt-engineer` | H3 prompt writing | structured prompts, handoff params | renders, shot redesign |
| `studio-reviewer` | Quality gate | PASS/REVISE/REJECT verdicts in chat.jsonl | deletes media, rewrites prompts |
| `studio-illustrator` | Still images (Krea 2) | character sheets, style refs, refines | video/H3 work |

## Dataflow

```
brief → studio-storyboarder → storyboard.md
      → studio-prompt-engineer → current_prompt.txt (+ handoff params)
      → studio (orchestrator)  → design_studio.py generate / generate-image
      → studio-reviewer        → PASS/REVISE/REJECT appended to chat.jsonl
```

Handoffs travel through the filesystem (project folders), never through chat
context. One role per step; escalate problems sideways, not around.

## Spawning

```bash
# one-shot
hermes -p studio-storyboarder chat -q "Plan shots for project X: ..."
# long work — background via terminal tool, or tmux for interactive
```

## Model switching

All profiles share one model/provider so the fleet switches together:

```bash
scripts/switch-model.sh show                      # fleet status
scripts/switch-model.sh openrouter <model>        # apply to all
scripts/switch-model.sh <provider> <model> studio # single profile
```

The FLEET array in switch-model.sh must be kept in sync when adding profiles.

## Adding a role

1. `hermes profile create studio-<role> --clone-from studio --no-alias`
2. Author SOUL at `hermes/profiles/studio-<role>/SOUL.md`, deploy to the
   profile dir
3. Add to switch-model.sh FLEET, AGENTS.md table, docs/agents.md
4. Verify: `hermes -p studio-<role> chat -q "Which SOUL role are you?"`

## Camera doctrine (video roles)

Dynamic camera moves are welcome — H3 executes them well. Discipline is the
rule: ONE clear move per shot with explicit path + speed + start/end framing,
stable subject blocking, gradual escalation (push-in → pan/crane → orbit).
Experimental moves get flagged in the storyboard so review knows what to judge.
