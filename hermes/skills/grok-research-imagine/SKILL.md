---
name: grok-research-imagine
description: Use when studio-grok handles current web/X research or xAI Imagine image creation/editing for Hermes Studio.
---

# Grok Research + Imagine

This skill belongs to the `studio-grok` backup profile (Grok 4.6 / xAI OAuth).

## Tools

- `web_search`: xAI agentic Web Search; use for web/current research.
- `x_search`: read-only X/Twitter post, account and thread discovery.
- `image_generate`: xAI Imagine quality model, text-to-image and edits.
- `vision`: inspect provided or generated images when the task needs it.

## Research workflow

1. Restate the question and freshness requirement.
2. Search primary web sources; use X for current statements/reactions.
3. Cross-check material claims. X posts alone are not proof of general facts.
4. Return findings with direct citations, disagreements/uncertainty, and a
   concise recommendation for the main Studio orchestrator.
5. If a project path is supplied and durable notes are requested, write them
   under `<project>/research/<slug>.md`.

## Imagine workflow

1. Generate/edit only when explicitly requested (xAI quota may be consumed).
2. Use the configured `grok-imagine-image-quality` model; do not switch models.
3. Return absolute cached image path, exact prompt, aspect ratio and whether it
   was generation or edit.
4. Main Studio archives accepted output with:

```bash
python3 ~/repos/hermes-studio/scripts/design_studio.py archive-grok \
  <project-id> <clip-id> <absolute-image-path> --meta-json '{"prompt":"..."}'
```

## Boundaries

- No ComfyUI, comfyui-mcp, local GPU, posting to X, or profile dispatch.
- Never fabricate URLs, quotations, image paths, or tool results.
