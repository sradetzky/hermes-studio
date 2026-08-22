#!/usr/bin/env python3
"""design_studio.py — core library + CLI for the Hermes Studio.

Manages the on-disk project structure (source of truth) and wraps H3
generation via the proven minimax-h3-run runner.

Root resolution order:
  1. --root flag / DESIGN_STUDIO_ROOT env var
  2. ~/repos/hermes-studio/studio-root

Usage:
  python3 scripts/design_studio.py create-project my-idea "A brief..."
  python3 scripts/design_studio.py list-projects
  python3 scripts/design_studio.py write-prompt my-idea - | prompt text...
  python3 scripts/design_studio.py append-chat my-idea user "hello"
  python3 scripts/design_studio.py generate my-idea --handoff h3_handoff_x.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

RUN_H3 = Path.home() / ".hermes/skills/minimax-h3-run/scripts/run_h3.py"
KREA2 = Path(__file__).resolve().parent / "krea2_image.py"
COMFY_ROOT = Path.home() / "ComfyUI"
COMFY_OUTPUT = COMFY_ROOT / "output"
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "studio-root"


def free_comfy_vram() -> dict:
    """Cleanup for legacy direct execution; production uses MCP clear_vram."""
    request = urllib.request.Request(
        "http://127.0.0.1:8188/free",
        data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"ok": response.status == 200, "status": response.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def interrupt_comfy() -> dict:
    request = urllib.request.Request(
        "http://127.0.0.1:8188/interrupt", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"ok": response.status == 200, "status": response.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def studio_root(override: str | None = None) -> Path:
    root = override or os.environ.get("DESIGN_STUDIO_ROOT") or str(DEFAULT_ROOT)
    p = Path(root).expanduser().resolve()
    for sub in ("projects", "shared", "tmp"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-_]+", "-", name.strip().lower()).strip("-")
    if not s:
        raise ValueError(f"cannot slugify project name: {name!r}")
    return s


def project_path(root: Path, name: str, must_exist: bool = True) -> Path:
    """Resolve a project by exact folder name. No fuzzy matching."""
    projects = (root / "projects").resolve()
    if not must_exist:
        today = _dt.date.today().isoformat()
        return (projects / f"{today}_{slugify(name)}").resolve()
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"invalid project id: {name!r}")
    p = (projects / name).resolve()
    if p.parent != projects:
        raise ValueError(f"project escapes projects directory: {name!r}")
    if p.is_dir():
        return p
    raise FileNotFoundError(
        f"project {name!r} not found; use an exact folder name (see list-projects)")


# ---------------------------------------------------------------- project mgmt

def create_project(root: Path, name: str, brief: str = "") -> Path:
    pp = project_path(root, name, must_exist=False)
    if pp.exists():
        raise FileExistsError(f"project already exists: {pp}")
    for sub in ("references", "generations", "final"):
        (pp / sub).mkdir(parents=True)
    (pp / "brief.md").write_text(
        f"# {name}\n\n{brief}\n\nCreated {_dt.datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8")
    (pp / "chat.jsonl").touch()
    (pp / "current_prompt.txt").touch()
    return pp


def list_projects(root: Path) -> list[str]:
    projects = root / "projects"
    if not projects.is_dir():
        return []
    return sorted(d.name for d in projects.iterdir()
                  if d.is_dir() and not d.is_symlink())


# ------------------------------------------------------------- prompt & chat

def write_prompt(root: Path, project: str, prompt: str) -> Path:
    pp = project_path(root, project)
    out = pp / "current_prompt.txt"
    out.write_text(prompt.rstrip() + "\n", encoding="utf-8")
    return out


def append_chat(root: Path, project: str, role: str, content: str) -> None:
    pp = project_path(root, project)
    entry = {"role": role,
             "content": content,
             "ts": _dt.datetime.now().isoformat(timespec="seconds")}
    # O_APPEND + single write: atomic enough for line-sized records even with
    # concurrent writers (threads / CLI + webapp).
    data = json.dumps(entry, ensure_ascii=False).encode("utf-8") + b"\n"
    fd = os.open(pp / "chat.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- generation

def next_generation_dir(pp: Path) -> Path:
    gens = pp / "generations"
    nums = [int(d.name) for d in gens.iterdir() if d.is_dir() and d.name.isdigit()]
    return gens / f"{max(nums, default=0) + 1:03d}"


def archive_outputs(root: Path, project: str, outputs: list[str],
                    metadata: dict | None = None) -> Path:
    """Archive completed MCP outputs from ComfyUI/output into one generation."""
    pp = project_path(root, project)
    output_root = COMFY_OUTPUT.resolve()
    sources = []
    for output in outputs:
        source = Path(output).expanduser()
        if not source.is_absolute():
            source = output_root / source
        source = source.resolve()
        if not source.is_relative_to(output_root):
            raise ValueError(f"output escapes ComfyUI output directory: {output!r}")
        if not source.is_file():
            raise FileNotFoundError(f"ComfyUI output not found: {source}")
        sources.append(source)
    if not sources:
        raise ValueError("at least one output file is required")

    gen_dir = next_generation_dir(pp)
    gen_dir.mkdir()
    copied = []
    try:
        for source in sources:
            target = gen_dir / source.name
            if target.exists():
                raise FileExistsError(f"duplicate output filename: {source.name}")
            shutil.copy2(source, target)
            copied.append(target.name)
        prompt_file = pp / "current_prompt.txt"
        if prompt_file.exists():
            shutil.copy2(prompt_file, gen_dir / "prompt.txt")
        meta = {
            **(metadata or {}),
            "generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "transport": "comfyui-mcp",
            "files": copied,
            "sources": [str(source) for source in sources],
        }
        (gen_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(gen_dir, ignore_errors=True)
        raise
    return gen_dir


def run_generation(root: Path, project: str, handoff: str | None = None,
                   extra_args: list[str] | None = None,
                   timeout: int = 7200, dry_run: bool = False) -> dict:
    """Submit an H3 generation via the proven run_h3.py runner, then archive."""
    pp = project_path(root, project)
    cmd = [sys.executable, str(RUN_H3), "--comfy-root", str(COMFY_ROOT),
           "-o", "/dev/stdout"]
    if handoff:
        h = Path(handoff).expanduser()
        if not h.exists():  # same archive fallback run_h3.py uses
            h = Path.home() / "Documents/MinimaxH3" / h.name
        if not h.exists():
            raise FileNotFoundError(f"handoff not found: {handoff}")
        cmd += ["--handoff", str(h)]
    if extra_args:
        cmd += extra_args

    if dry_run:
        result = subprocess.run(cmd + ["--dry-run"], capture_output=True, text=True)
        return {"dry_run": True, "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]}

    print(f"[design-studio] submitting: {' '.join(cmd)}", file=sys.stderr)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        interrupt_comfy()
        free_comfy_vram()
        raise
    cleanup = free_comfy_vram()

    summary = {}
    try:  # runner writes its summary JSON via -o /dev/stdout
        summary = json.loads(result.stdout[result.stdout.index("{"):])
    except Exception:
        pass
    if result.returncode != 0:
        return {"ok": False, "returncode": result.returncode,
                "stderr": result.stderr[-3000:], "summary": summary,
                "vram_cleanup": cleanup}

    gen_dir = next_generation_dir(pp)
    gen_dir.mkdir()
    meta = {"generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "handoff": str(handoff or ""), "runner": str(RUN_H3),
            "vram_cleanup": cleanup, **summary}

    video = None
    for key in ("video", "output", "path"):  # locate produced file in summary
        v = summary.get(key)
        if v and Path(v).exists():
            video = Path(v)
            break
    if video:
        shutil.copy2(video, gen_dir / "video.mp4")
        meta["source"] = str(video)
    (gen_dir / "prompt.txt").write_text(
        (pp / "current_prompt.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (gen_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    # preview.jpg is created by the reviewer/user; do not extract frames automatically.
    return {"ok": cleanup.get("ok", False), "generation": str(gen_dir),
            "meta": meta}


def run_image_generation(root: Path, project: str, recipe: str, prompt: str = "",
                         image: str | None = None, extra_args: list[str] | None = None,
                         timeout: int = 900) -> dict:
    """Krea 2 still image via scripts/krea2_image.py, archived like generations."""
    pp = project_path(root, project)
    prefix = f"studio_{slugify(project)}"
    cmd = [sys.executable, str(KREA2), "--recipe", recipe, "--prefix", prefix]
    if prompt:
        cmd += ["--prompt", prompt]
    if image:
        cmd += ["--image", str(Path(image).expanduser())]
    if extra_args:
        cmd += extra_args

    print(f"[design-studio] submitting: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    seed, prompt_id, files = None, None, []
    # runner prints a one-line summary JSON then an indented result JSON
    dec, pos = json.JSONDecoder(), 0
    while True:
        start = result.stdout.find("{", pos)
        if start < 0:
            break
        try:
            obj, end = dec.raw_decode(result.stdout[start:])
        except ValueError:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            seed = obj.get("seed", seed)
            prompt_id = obj.get("prompt_id", prompt_id)
            f = obj.get("files")
            if isinstance(f, list) and f:
                files = [x for x in f if isinstance(x, str)]
        pos = start + end
    if result.returncode != 0:
        return {"ok": False, "returncode": result.returncode,
                "stderr": result.stderr[-2000:] or result.stdout[-2000:]}

    gen_dir = next_generation_dir(pp)
    gen_dir.mkdir()
    copied = []
    for fname in files:
        src = COMFY_OUTPUT / fname
        if src.exists():
            dst = gen_dir / fname
            shutil.copy2(src, dst)
            copied.append(dst.name)
    meta = {"generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "kind": "image", "recipe": recipe, "prompt": prompt,
            "input_image": str(image or ""), "seed": seed,
            "prompt_id": prompt_id, "files": copied}
    if prompt:
        (gen_dir / "prompt.txt").write_text(prompt.rstrip() + "\n", encoding="utf-8")
    (gen_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "generation": str(gen_dir), "meta": meta}


# ---------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="studio root (default: $DESIGN_STUDIO_ROOT or repo studio-root/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("create-project")
    sp.add_argument("name"); sp.add_argument("brief", nargs="*", default=[])

    sub.add_parser("list-projects")

    sp = sub.add_parser("write-prompt")
    sp.add_argument("project"); sp.add_argument("prompt", nargs="+")

    sp = sub.add_parser("append-chat")
    sp.add_argument("project"); sp.add_argument("role"); sp.add_argument("content")

    sp = sub.add_parser("generate")
    sp.add_argument("project")
    sp.add_argument("--handoff")
    sp.add_argument("--arg", action="append", default=[],
                    help="extra run_h3.py arg, repeatable. '--arg --turbo' style: use e.g. --arg=--mp --arg=0.9")
    sp.add_argument("--timeout", type=int, default=7200)
    sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("generate-image")
    sp.add_argument("project")
    sp.add_argument("--recipe", required=True,
                    choices=["t2i", "t2i-nvfp4", "style-ref", "upscale", "edit"])
    sp.add_argument("--prompt", default="")
    sp.add_argument("--image", help="input image for style-ref / upscale / edit")
    sp.add_argument("--ref-boost", type=float, default=None, help="edit recipe")
    sp.add_argument("--arg", action="append", default=[],
                    help="extra krea2_image.py arg, e.g. --arg=--aspect --arg=16:9")
    sp.add_argument("--timeout", type=int, default=900)

    sp = sub.add_parser("archive-output")
    sp.add_argument("project")
    sp.add_argument("outputs", nargs="+")
    sp.add_argument("--prompt-id", default="")
    sp.add_argument("--kind", default="")
    sp.add_argument("--recipe", default="")
    sp.add_argument("--meta-json", default="{}")

    args = ap.parse_args(argv)
    root = studio_root(args.root)

    if args.cmd == "create-project":
        print(create_project(root, args.name, " ".join(args.brief)))
    elif args.cmd == "list-projects":
        print("\n".join(list_projects(root)) or "(no projects)")
    elif args.cmd == "write-prompt":
        print(write_prompt(root, args.project, " ".join(args.prompt)))
    elif args.cmd == "append-chat":
        append_chat(root, args.project, args.role, args.content)
        print("appended")
    elif args.cmd == "generate":
        extra = []
        for a in args.arg:
            extra.extend(shlex.split(a))
        out = run_generation(root, args.project, args.handoff, extra,
                             timeout=args.timeout, dry_run=args.dry_run)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") or out.get("dry_run") else 1
    elif args.cmd == "generate-image":
        extra = []
        for a in args.arg:
            extra.extend(shlex.split(a))
        if args.ref_boost is not None:
            extra += ["--ref-boost", str(args.ref_boost)]
        out = run_image_generation(root, args.project, args.recipe, args.prompt,
                                   image=args.image, extra_args=extra,
                                   timeout=args.timeout)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1
    elif args.cmd == "archive-output":
        try:
            meta = json.loads(args.meta_json)
        except json.JSONDecodeError as exc:
            ap.error(f"invalid --meta-json: {exc}")
        if not isinstance(meta, dict):
            ap.error("--meta-json must be a JSON object")
        if args.prompt_id:
            meta["prompt_id"] = args.prompt_id
        if args.kind:
            meta["kind"] = args.kind
        if args.recipe:
            meta["recipe"] = args.recipe
        print(archive_outputs(root, args.project, args.outputs, meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
