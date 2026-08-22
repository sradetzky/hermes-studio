# Hermes Studio — Skeleton

This is a starter skeleton + decision log for a fully local creative studio built around:

- **Hermes Agent** (orchestration + personalities via Profiles)
- **MiniMax H3** (open-weight omni-modal video + native audio) running in local ComfyUI
- Simple self-hosted web UI (chat + media player + folder-backed projects)

See **PLAN.md** for the full architecture and implementation order.

## Quick Start for Implementing Agent

1. Read `PLAN.md` completely.
2. Create the Hermes profile:
   ```bash
   hermes profile create studio --clone
   ```
3. Replace the SOUL in the new profile with the one in `hermes/profiles/studio/SOUL.md`.
4. Implement / expand the skill in `hermes/skills/design-studio/`.
5. Set up the folder root (copy `studio-root/` or point to your preferred location).
6. Verify the pinned comfyui-mcp server and test generation → folder placement.
7. Only then build the minimal FastAPI + single-page UI.

## Key Files

| Path | Purpose |
|------|---------|
| `PLAN.md` | Single source of truth for all decisions |
| `hermes/profiles/studio/SOUL.md` | Starting personality for the studio agent |
| `hermes/skills/design-studio/SKILL.md` | Skill skeleton the agent should expand |
| `studio-root/` | Example on-disk project layout |
| `comfyui/workflows/` | Place for your parameterized H3 API JSONs |
| `scripts/` | Helper scripts (model switch, etc.) |

## Model Switching Strategy

All Hermes profiles (main + studio + any others) should point at the **same** local OpenAI-compatible endpoint.  
Changing models = restart the local server. No need to edit every `config.yaml`.

---

Created 2026-08-22 for local RTX 5060 Ti 16GB setup.