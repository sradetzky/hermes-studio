# Grok Backup Profile

`studio-grok` is Hermes Studio's cloud backup for current research, read-only X
search and xAI Imagine generation/editing. It never owns local ComfyUI/GPU work.

## Configuration

- Model/provider: `grok-4.6` via `xai-oauth`
- Context: 500k (reported by provider)
- Web: xAI agentic Web Search (`web.search_backend: xai`)
- X/Twitter: native `x_search`, model `grok-4.6`, high reasoning
- Images: `grok-imagine-image-quality` at 1K
- Auth: existing Hermes xAI OAuth (SuperGrok/Premium+); no API key required
- MCP: none

Source-controlled files:

- `hermes/profiles/studio-grok/SOUL.md`
- `hermes/profiles/studio-grok/config.yaml.example`
- `hermes/skills/grok-research-imagine/SKILL.md`

The profile is intentionally excluded from `scripts/switch-model.sh`, so
fleet-wide local model changes cannot move it off Grok 4.6.

## Studio dispatch

```bash
python3 scripts/design_studio.py dispatch-grok <project-id> \
  "Research the latest primary sources and X statements about ..."
```

The command maintains a persistent per-project Grok session under the ignored
`studio-root/tmp/profile-sessions/` directory. The task receives the exact
project id/path and only these toolsets:

```text
web,x_search,image_gen,vision,file,terminal
```

For durable research, ask it to write `<project>/research/<slug>.md`.

## Imagine output

Imagine returns an absolute cache path under:

```text
~/.hermes/profiles/studio-grok/cache/images/
```

Archive an accepted image safely into the project:

```bash
python3 scripts/design_studio.py archive-grok <project-id> <image-path> \
  --meta-json '{"prompt":"exact prompt","aspect_ratio":"landscape"}'
```

`archive-grok` rejects paths outside the profile's image cache and records
`transport: xai-imagine`. Image generation must be explicitly requested
because it can consume xAI quota.

## Verification

```bash
hermes profile show studio-grok
hermes -p studio-grok tools list
hermes -p studio-grok chat -Q \
  -t web,x_search,image_gen,vision,file,terminal \
  -q "Reply exactly: GROK-4.6 READY"
```

Verified live: Grok 4.6 inference, xAI Web Search, and OAuth-backed `x_search`
returning public X status URLs. Imagine is configured and tool-ready; no paid
image was generated during setup.
