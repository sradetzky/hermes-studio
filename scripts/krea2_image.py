#!/usr/bin/env python3
"""krea2_image.py — Krea 2 image generation/editing via local ComfyUI.

Builds API-format graphs from proven local recipes and queues them on
http://127.0.0.1:8188. All models verified present in ~/ComfyUI/models.

Recipes:
  t2i        text→image   darkBeast_v1.1_int8_convrot + qwen3-vl-4b-heretic
                        + Krea-2-unlocked LoRA 0.8, euler/simple 8 steps cfg1
  t2i-nvfp4  same on darkBeastKREA2nvfp4 (smaller VRAM footprint)
  style-ref  character sheet w/ krea2_style_reference LoRA + reference image
  upscale    NO8D high-quality portrait "upscale" (img2img refine @ denoise)
  edit       Krea2 identity edit: instruction-based edit of an image with
             identity preservation (turbo fp8 UNET + identity_edit LoRA,
             NO8D server-side reference patch + grounded encode)

  Usage:
  python3 scripts/krea2_image.py --recipe t2i --prompt "..." [--aspect 16:9]
      [--steps 8] [--seed N] [--lora NAME:STRENGTH ...] [--output-prefix pfx]
  python3 scripts/krea2_image.py --recipe style-ref --prompt "..." \
      --image ref.png
  python3 scripts/krea2_image.py --recipe upscale --image in.png \
      [--denoise 0.35] [--strength 1.0]
  python3 scripts/krea2_image.py --dry-run ...   # print graph only

Outputs land in <project>/generations/NNN/ when called through
design_studio.py generate-image, or ComfyUI/output otherwise.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.request
from pathlib import Path

HOST = "http://127.0.0.1:8188"

# Local model files (verified 2026-08-22)
UNET_INT8 = "darkBeast_v1.1_int8_convrot.safetensors"
UNET_NVFP4 = "darkBeastKREA2nvfp4.safetensors"
UNET_TURBO_FP8 = "krea2_turbo_fp8_scaled.safetensors"
CLIP = "qwen3-vl-4b-heretic_nvfp4.safetensors"
CLIP_EDIT = "qwen3vl_4b_fp8_scaled.safetensors"
VAE = "qwen_image_vae.safetensors"
LORA_UNLOCKED = "Krea-2-unlocked.safetensors"
LORA_STYLE_REF = "krea2_style_reference.safetensors"
LORA_IDENTITY_EDIT = "Krea2/krea2_identity_edit_v1_2.safetensors"

ASPECTS = {  # ~1MP canvas discipline, same ceiling as H3
    "1:1": (1024, 1024), "4:3": (1152, 864), "3:2": (1248, 832),
    "16:9": (1344, 768), "9:16": (768, 1344), "3:4": (864, 1152),
    "2:3": (832, 1248), "4:5": (928, 1152),
}


def api_graph(unet: str, loras: list[tuple[str, float]], prompt: str,
              width: int, height: int, steps: int, seed: int,
              sampler: str = "euler", scheduler: str = "simple",
              prefix: str = "studio_krea2") -> dict:
    g = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "krea2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
        "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
        "latent": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "sample": {"class_type": "KSampler", "inputs": {
            "model": ["model_out", 0], "seed": seed, "steps": steps, "cfg": 1.0,
            "sampler_name": sampler, "scheduler": scheduler,
            "positive": ["pos", 0], "negative": ["neg", 0],
            "latent_image": ["latent", 0], "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": prefix}},
    }
    # chain loras onto unet; last one feeds the sampler
    prev, prev_out = "unet", 0
    for i, (name, strength) in enumerate(loras):
        key = f"lora_{i}"
        g[key] = {"class_type": "LoraLoaderModelOnly",
                  "inputs": {"model": [prev, prev_out], "lora_name": name,
                             "strength_model": strength}}
        prev, prev_out = key, 0
    g["sample"]["inputs"]["model"] = [prev, prev_out]
    return g


def identity_edit_graph(image_file: str, instruction: str, width: int, height: int,
                        steps: int, seed: int, prefix: str,
                        ref_boost: float = 1.0) -> dict:
    """Krea2 identity edit via NO8D server-side nodes.

    Mirrors the no8d_krea2_high_quality_portrait_upscale wiring:
    source image -> VAEEncode (latent) + NO8DReferenceModel patch;
    instruction + source image -> grounded conditioning; empty target latent.
    """
    g = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET_TURBO_FP8, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_EDIT, "type": "krea2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "lora": {"class_type": "LoraLoaderModelOnly",
                 "inputs": {"model": ["unet", 0], "lora_name": LORA_IDENTITY_EDIT,
                            "strength_model": 1.0}},
        "load": {"class_type": "LoadImage", "inputs": {"image": image_file}},
        "encode_src": {"class_type": "VAEEncode", "inputs": {"pixels": ["load", 0], "vae": ["vae", 0]}},
        "ref_patch": {"class_type": "NO8DKrea2ReferenceModel",
                      "inputs": {"model": ["lora", 0], "source_latent": ["encode_src", 0],
                                 "ref_boost": ref_boost, "vae": ["vae", 0],
                                 "source_image": ["load", 0]}},
        "pos": {"class_type": "NO8DKrea2GroundedEncode",
                "inputs": {"clip": ["clip", 0], "text": instruction, "image": ["load", 0]}},
        "neg": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}},
        "latent": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "sample": {"class_type": "KSampler", "inputs": {
            "model": ["ref_patch", 0], "seed": seed, "steps": steps, "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple",
            "positive": ["pos", 0], "negative": ["neg", 0],
            "latent_image": ["latent", 0], "denoise": 1.0}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": prefix}},
    }
    return g


def upload_image(path: Path) -> str:
    """Upload an image into ComfyUI/input, return stored filename."""
    boundary = "----StudioBoundary"
    data = path.read_bytes()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{HOST}/upload/image?overwrite=true&type=input", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    return out["name"]


def img2img_graph(unet: str, loras, image_file: str, width: int, height: int,
                  steps: int, seed: int, denoise: float, prefix: str) -> dict:
    g = api_graph(unet, loras, "high quality portrait", width, height,
                  steps, seed, prefix=prefix)
    # swap empty latent for encoded source; grounded conditioning on source text
    g["load"] = {"class_type": "LoadImage", "inputs": {"image": image_file}}
    g["encode"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["load", 0], "vae": ["vae", 0]}}
    g["pos"] = {"class_type": "NO8DKrea2GroundedEncode",
                "inputs": {"clip": ["clip", 0], "text": "refine details, enhance quality",
                           "image": ["load", 0]}}
    g["neg"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["pos", 0]}}
    del g["latent"]
    g["sample"]["inputs"]["latent_image"] = ["encode", 0]
    g["sample"]["inputs"]["denoise"] = denoise
    return {k: v for k, v in g.items() if v is not None}


def queue(graph: dict) -> str:
    req = urllib.request.Request(f"{HOST}/prompt",
                                 data=json.dumps({"prompt": graph}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["prompt_id"]


def free_vram() -> dict:
    """Best-effort cleanup for the explicit legacy REST execution path."""
    request = urllib.request.Request(
        f"{HOST}/free",
        data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"ok": response.status == 200, "status": response.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def interrupt() -> dict:
    request = urllib.request.Request(f"{HOST}/interrupt", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"ok": response.status == 200, "status": response.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def wait(prompt_id: str, timeout: int = 600) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with urllib.request.urlopen(f"{HOST}/history/{prompt_id}", timeout=15) as r:
            h = json.load(r)
        if prompt_id in h:
            status = h[prompt_id].get("status", {})
            if status.get("completed"):
                outs = []
                for node_out in h[prompt_id]["outputs"].values():
                    for img in node_out.get("images", []):
                        outs.append(img["filename"])
                return {"done": True, "files": outs}
            if status.get("status_str") == "error":
                return {"done": False, "error": str(status)[:2000]}
        time.sleep(2)
    return {"done": False, "error": "timeout"}


def parse_loras(values: list[str]) -> list[tuple[str, float]]:
    loras = []
    for value in values:
        name, separator, raw_strength = value.rpartition(":")
        if not separator or not name:
            raise ValueError(f"invalid LoRA {value!r}; expected NAME:STRENGTH")
        try:
            strength = float(raw_strength)
        except ValueError as exc:
            raise ValueError(
                f"invalid LoRA strength in {value!r}; expected a number") from exc
        loras.append((name, strength))
    return loras


def input_image(path: str, dry_run: bool) -> str:
    image = Path(path).expanduser()
    if not image.is_file():
        raise FileNotFoundError(f"input image not found: {image}")
    return image.name if dry_run else upload_image(image)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recipe", required=True,
                    choices=["t2i", "t2i-nvfp4", "style-ref", "upscale", "edit"])
    ap.add_argument("--prompt", default="")
    ap.add_argument("--image", help="input image (style-ref / upscale / edit)")
    ap.add_argument("--aspect", default="1:1", choices=sorted(ASPECTS))
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--denoise", type=float, default=0.35, help="upscale recipe")
    ap.add_argument("--ref-boost", type=float, default=1.0, help="edit recipe")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--lora", action="append", default=[],
                    help="extra lora as name:strength (repeatable)")
    ap.add_argument("--prefix", default="studio_krea2")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    seed = args.seed if args.seed is not None else random.randrange(2**31)
    w, h = ASPECTS[args.aspect]
    graph = None
    try:
        extra_loras = parse_loras(args.lora)
    except ValueError as exc:
        ap.error(str(exc))

    if args.recipe == "t2i":
        loras = [(LORA_UNLOCKED, 0.8), *extra_loras]
        graph = api_graph(UNET_INT8, loras, args.prompt, w, h, steps=args.steps, seed=seed, prefix=args.prefix)
    elif args.recipe == "t2i-nvfp4":
        loras = [(LORA_UNLOCKED, 0.8), *extra_loras]
        graph = api_graph(UNET_NVFP4, loras, args.prompt, w, h, steps=args.steps, seed=seed, prefix=args.prefix)
    elif args.recipe == "style-ref":
        if not args.image:
            ap.error("--image required for style-ref")
        up = input_image(args.image, args.dry_run)
        loras = [(LORA_UNLOCKED, 0.8), (LORA_STYLE_REF, 1.0), *extra_loras]
        # style-reference LoRA expects the ref image fed as second encode input;
        # simplest reliable path locally: img2img-style grounding at low denoise
        graph = img2img_graph(UNET_NVFP4, loras, up, w, h, args.steps, seed,
                              denoise=0.75, prefix=args.prefix)
    elif args.recipe == "upscale":
        if not args.image:
            ap.error("--image required for upscale")
        up = input_image(args.image, args.dry_run)
        graph = img2img_graph(UNET_NVFP4,
                              [(LORA_UNLOCKED, 0.8), *extra_loras], up, w, h,
                              args.steps, seed, args.denoise, args.prefix)
    elif args.recipe == "edit":
        if not args.image:
            ap.error("--image required for edit")
        if not args.prompt:
            ap.error("--prompt (edit instruction) required for edit")
        if extra_loras:
            ap.error("--lora is not supported by the edit recipe")
        up = input_image(args.image, args.dry_run)
        graph = identity_edit_graph(up, args.prompt, w, h, args.steps, seed,
                                    args.prefix, ref_boost=args.ref_boost)

    if args.dry_run:
        assert graph is not None
        print(json.dumps({"graph": graph, "seed": seed}, indent=2)[:4000])
        return 0

    assert graph is not None
    pid = queue(graph)
    print(json.dumps({"prompt_id": pid, "seed": seed, "recipe": args.recipe}))
    try:
        result = wait(pid, timeout=args.timeout)
    except Exception:
        interrupt()
        free_vram()
        raise
    if result.get("error") == "timeout":
        result["interrupt"] = interrupt()
    cleanup = free_vram()
    result["vram_cleanup"] = cleanup
    print(json.dumps(result, indent=2))
    return 0 if result.get("done") and cleanup.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
