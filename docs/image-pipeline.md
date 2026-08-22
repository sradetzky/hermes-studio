# Image Pipeline — Krea 2 (local)

All local via ComfyUI; the native Krea2ImageNode is a paid cloud API node and
is NOT used.

## Runner

`scripts/krea2_image.py --recipe <r> [opts]`

| Recipe | Purpose | Graph |
|---|---|---|
| `t2i` | text→image | darkBeast_v1.1_int8_convrot, euler/simple 8 steps cfg 1 |
| `t2i-nvfp4` | text→image, less VRAM | darkBeastKREA2nvfp4 |
| `style-ref` | image-grounded gen w/ style LoRA | + krea2_style_reference, high-denoise img2img grounding |
| `upscale` | refine existing image | grounded encode img2img @ denoise ~0.35 |

Common flags: `--prompt`, `--image`, `--aspect` (1:1 4:3 3:2 16:9 9:16 3:4 2:3
4:5 — all ≤~1MP), `--steps`, `--seed`, `--lora name:strength`, `--denoise`,
`--prefix`, `--dry-run`.

## Model files (all under ~/ComfyUI/models/)

- diffusion_models: `darkBeast_v1.1_int8_convrot.safetensors`,
  `darkBeastKREA2nvfp4.safetensors`, `krea2_turbo_fp8_scaled.safetensors`
- text_encoders: `qwen3-vl-4b-heretic_nvfp4.safetensors`,
  `qwen3vl_4b_fp8_scaled.safetensors`
- vae: `qwen_image_vae.safetensors`
- loras: `Krea-2-unlocked.safetensors` (0.8), `krea2_style_reference.safetensors`,
  `Krea2/krea2_identity_edit_v1_2.safetensors`

Sources: Comfy-Org/Krea-2 (HF), conradlocke/krea2-identity-edit (LoRA).

## Identity edit

The `krea2_identity_edit.json` UI workflow (in
~/ComfyUI/user/default/workflows/krea2/) does instruction-based image editing
with identity preservation. Not yet wired as a recipe in krea2_image.py — its
graph uses Krea2EditGroundedEncode/Krea2EditModelPatch (frontend nodes) plus
the turbo fp8 UNET + qwen3vl_4b CLIP + identity-edit LoRA. Port it to API
format when needed.

## Project integration

`design_studio.py generate-image <project> --recipe <r> ...` archives into
`generations/NNN/` with meta.json (seed/prompt_id) + prompt.txt. Files land in
ComfyUI/output first and are copied over.
