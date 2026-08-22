---
name: krea2-images
description: Use when generating or editing images with local Krea 2 in ComfyUI — t2i, style-ref, upscale recipes via scripts/krea2_image.py.
---

# krea2-images skill

Local Krea 2 (darkBeast checkpoints) image generation/editing through the
studio's runner.

## Tool

```bash
python3 ~/repos/hermes-studio/scripts/krea2_image.py --recipe <r> [opts]
```

Recipes (all verified working 2026-08-22):

| Recipe | Purpose | Models |
|---|---|---|
| `t2i` | text→image | darkBeast_v1.1_int8_convrot + qwen3-vl-4b-heretic + qwen_image_vae + Krea-2-unlocked LoRA 0.8 |
| `t2i-nvfp4` | text→image, less VRAM | darkBeastKREA2nvfp4 |
| `style-ref` | image-grounded gen w/ style ref | + krea2_style_reference LoRA (runs as high-denoise img2img grounding) |
| `upscale` | refine existing image | img2img @ denoise 0.35, grounded encode |

Key flags: `--prompt`, `--image` (style-ref/upscale), `--aspect`
(1:1,4:3,3:2,16:9,9:16,3:4,2:3,4:5 — all ≤~1MP), `--steps` (default 8,
turbo-range), `--seed`, `--lora name:strength` (repeatable), `--denoise`,
`--prefix`, `--dry-run`.

## Behaviour

- Prints `{prompt_id, seed}` then waits; prints `{done, files}` at completion.
- Files land in `~/ComfyUI/output/<prefix>_*.png`; archive them into the
  project (`design_studio.py`) yourself after review.
- ALWAYS sequential: one ComfyUI job at a time, ever.

## Not yet available locally

Identity/multi-image edit (the `krea2_identity_edit.json` workflow) needs
models not yet downloaded: `krea2_turbo_fp8_scaled.safetensors`,
`qwen3vl_4b_fp8_scaled.safetensors`, LoRA `Krea2/krea2_identity_edit_v1_2.safetensors`.
Do NOT promise this capability until they exist under ~/ComfyUI/models.

## Prompting tips

Krea 2 responds well to: subject → action/pose → composition/camera → lighting
→ style anchor. Keep style anchors identical across a character-sheet set.
