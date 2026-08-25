# Video Pipeline — MiniMax H3

The proven runner builds the graph; production execution goes through
comfyui-mcp.

## Graph builder

`$HERMES_HOME/skills/minimax-h3-run/scripts/run_h3.py`
- Modes: t2va / i2va / fl2va / r2v
- `--handoff` resolves against
  `$HERMES_REAL_HOME/Documents/MinimaxH3/` archive fallback
- Builds the ComfyUI graph itself; `comfyui/workflows/` here is optional

Hermes profile tools may isolate `$HOME`. Studio therefore uses `$HERMES_HOME`
for active-profile skills and `$HERMES_REAL_HOME` for the account's ComfyUI,
Documents, and repository paths; raw `~` is not a stable root.

Studio web jobs decode the immutable generation package into frozen typed values.
The supervised deterministic worker derives one exact shell-quoted
`run_h3.py --dry-run` command, keeps graph JSON out of model context, serially
uploads ordered references, patches returned loader filenames in memory, and
passes the exact graph through pinned `mcporter@0.13.7` →
`comfyui-mcp@0.52.61`. Web Hermes profiles receive explicit non-ComfyUI
toolsets and cannot execute this transaction.
`scripts/check_tool_versions.py`, invoked by the canonical release gate, verifies
that both pinned npm package versions remain available and that the installed
Hermes CLI still exposes the subprocess options this integration requires.
The worker waits through MCP, archives against exact project + clip IDs and
authoritative history, and always clears VRAM. Only a typed `generate` job owns
queue cancellation; chat or specialist failures never interrupt ComfyUI.
`design_studio.py generate` remains a manual direct diagnostic fallback.

## Proven knobs (RTX 5060 Ti 16GB)

- Canvas ceiling ~1MP: 1280x704 / 736x1344 fine; 1088x1920 OOMs at sampler
  even with int8 UNET
- Chapter pairing: previews mp 0.5 / 8 steps; finals mp 0.9 / 20 steps, accel
- `--accel` is exact Sol fused modulation + ChunkFF only; do not attach Sage,
  sparse Sol attention, or EasyCache
- LightX2V turbo LoRAs: Ref2VA v0.1 4-step (R2V); FL2VA v1.1 768p 4-step
  (T2VA/I2VA/FL2VA), strength 1.0, res_multistep/simple, explicit 1344x768
- Never run two H3 jobs concurrently (ComfyUI/input upload races corrupt refs)
- `submit_h3_graph_mcp.py` owns ordered reference uploads and batch submission;
  agents never hand-transcribe graphs into MCP tool arguments
- Cancelling: killing run_h3.py does NOT stop the queued job — use the MCP
  queue cancel action, verify stopped, then clear VRAM
- Empty prompt bodies are rejected — pass real content ('.' placeholder works)

## Prompting

Official structure enforced by `minimax-h3-prompt` skill:
- T2VA/I2VA/FL2VA/L2VA: integrated_multimodal_description +
  overall_soundscape + non_diegetic_music (+ frame alignment)
- Ref2VA: six-section format with explicit reference roles

Camera doctrine lives in docs/agents.md.
