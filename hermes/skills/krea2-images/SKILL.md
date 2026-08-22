---
name: krea2-images
description: Use when generating or editing images with local Krea 2 in ComfyUI — t2i, style-ref, upscale recipes via scripts/krea2_image.py.
---

# krea2-images skill

Local Krea 2 graph recipes. The illustrator prepares prompts/parameters; the
`studio` orchestrator queues graphs through comfyui-mcp.

## Tool

```bash
python3 ~/repos/hermes-studio/scripts/krea2_image.py --recipe <r> [opts]
```

Recipes (all verified working; use `--dry-run` to emit the graph):

| Recipe | Purpose | Models |
|---|---|---|
| `t2i` | text→image | darkBeast_v1.1_int8_convrot + qwen3-vl-4b-heretic + qwen_image_vae + Krea-2-unlocked LoRA 0.8 |
| `t2i-nvfp4` | text→image, less VRAM | darkBeastKREA2nvfp4 |
| `style-ref` | image-grounded gen w/ style ref | + krea2_style_reference LoRA (runs as high-denoise img2img grounding) |
| `upscale` | refine existing image | img2img @ denoise 0.35, grounded encode |
| `edit` | instruction-based identity edit | krea2_turbo_fp8 + qwen3vl_4b_fp8 + identity_edit_v1_2 LoRA; `--prompt` = edit instruction, `--ref-boost` tunes identity lock |

Key flags: `--prompt`, `--image` (style-ref/upscale/edit), `--aspect`
(1:1,4:3,3:2,16:9,9:16,3:4,2:3,4:5 — all ≤~1MP), `--steps` (default 8,
turbo-range), `--seed`, `--lora name:strength` (repeatable), `--denoise`,
`--prefix`, `--dry-run`.

## Behaviour

- `studio-illustrator` must not queue ComfyUI. It returns recipe, prompt,
  reference paths, aspect, steps, seed/ref-boost and any extra LoRAs to the
  `studio` orchestrator.
- `studio` builds with `--dry-run`, uploads refs and enqueues through MCP,
  archives with `design_studio.py archive-output`, then always calls
  `mcp_comfyui_clear_vram`.
- Direct execution without `--dry-run` is a manual diagnostic fallback only;
  it unloads models/frees memory after terminal success or failure and
  interrupts before cleanup on timeout.

## Identity edit

Available as `--recipe edit` (verified live). Write the instruction in
`--prompt` ("Change her outfit to…", "Replace the background with…").
`--ref-boost` >1 strengthens identity preservation, <1 lets edits act more
freely. Sequential GPU jobs only.

## Prompting tips

Krea 2 responds well to: subject → action/pose → composition/camera → lighting
→ style anchor. Keep style anchors identical across a character-sheet set.
