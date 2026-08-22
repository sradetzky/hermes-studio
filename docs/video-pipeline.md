# Video Pipeline — MiniMax H3

The proven runner builds the graph; production execution goes through
comfyui-mcp.

## Graph builder

`~/.hermes/skills/minimax-h3-run/scripts/run_h3.py`
- Modes: t2va / i2va / fl2va / r2v
- `--handoff` resolves against `~/Documents/MinimaxH3/` archive fallback
- Builds the ComfyUI graph itself; `comfyui/workflows/` here is optional

Studio builds with `run_h3.py --dry-run`, uploads refs and enqueues through
MCP, archives with `design_studio.py archive-output`, then always clears VRAM.
`design_studio.py generate` remains a manual direct diagnostic fallback.

## Proven knobs (RTX 5060 Ti 16GB)

- Canvas ceiling ~1MP: 1280x704 / 736x1344 fine; 1088x1920 OOMs at sampler
  even with int8 UNET
- Chapter pairing: previews mp 0.5 / 8 steps; finals mp 0.9 / 20 steps, accel
- LightX2V turbo LoRAs: Ref2VA v0.1 4-step (R2V); FL2VA v1.1 768p 4-step
  (T2VA/I2VA/FL2VA), strength 1.0, res_multistep/simple, explicit 1344x768
- Never run two H3 jobs concurrently (ComfyUI/input upload races corrupt refs)
- Cancelling: killing run_h3.py does NOT stop the queued job — use the MCP
  queue cancel action, verify stopped, then clear VRAM
- Empty prompt bodies are rejected — pass real content ('.' placeholder works)

## Prompting

Official structure enforced by `minimax-h3-prompt` skill:
- T2VA/I2VA/FL2VA/L2VA: integrated_multimodal_description +
  overall_soundscape + non_diegetic_music (+ frame alignment)
- Ref2VA: six-section format with explicit reference roles

Camera doctrine lives in docs/agents.md.
