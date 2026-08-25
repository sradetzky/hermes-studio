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
  python3 scripts/design_studio.py list-clips 2026-08-23_my-idea
  python3 scripts/design_studio.py write-prompt 2026-08-23_my-idea clip-001 "..."
  python3 scripts/design_studio.py append-chat my-idea user "hello"
  python3 scripts/design_studio.py generate my-idea clip-001 --handoff h3_handoff_x.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if __package__ in {None, ""} and str(REPO_ROOT) not in sys.path:
    # Direct `python scripts/design_studio.py` execution needs the repo package
    # root for shared runtime modules. Installed/package imports need no shim.
    sys.path.insert(0, str(REPO_ROOT))

from studio_core import generation_archive as generation_archive_core
from studio_core.dispatch import (
    LOCAL_SPECIALIST_PROFILES,
    _dispatch_clip_scope,
    dispatch_grok,
    dispatch_profile,
)
from studio_core.job_store import JobStore
from studio_core.paths import StudioPaths
from studio_core.projects import (
    CLIP_STORE,
    ClipStore,
    clip_path,
    create_project,
    list_projects,
    next_generation_dir,
    project_path,
    read_project_text,
    slugify,
    studio_root,
    write_prompt,
)
from studio_core.safe_files import (
    atomic_publish_directory,
    copy_opened_file,
)

__all__ = [
    "ClipStore",
    "archive_outputs",
    "clip_path",
    "create_project",
    "dispatch_grok",
    "dispatch_profile",
    "list_projects",
    "next_generation_dir",
    "project_path",
    "read_project_text",
    "studio_root",
    "write_prompt",
]


STUDIO_PATHS = StudioPaths.from_environment()
REAL_HOME = STUDIO_PATHS.real_home
HERMES_HOME = STUDIO_PATHS.active_profile_home
HERMES_ROOT = STUDIO_PATHS.hermes_root
RUN_H3 = HERMES_HOME / "skills/minimax-h3-run/scripts/run_h3.py"
KREA2 = Path(__file__).resolve().parent / "krea2_image.py"
COMFY_ROOT = STUDIO_PATHS.comfy_root
COMFY_OUTPUT = COMFY_ROOT / "output"
GROK_IMAGE_OUTPUT = (
    HERMES_ROOT / "profiles" / "studio-grok" / "cache" / "images"
)
DEFAULT_ROOT = REPO_ROOT / "studio-root"
DEFAULT_RUNTIME = DEFAULT_ROOT.parent / ".runtime"
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


# ------------------------------------------------------------- prompt & chat


def append_chat(root: Path, project: str, role: str, content: str, *,
                clip_id: str | None = None) -> None:
    pp = project_path(root, project)
    scoped_clip_id, scoped_clip = _dispatch_clip_scope(root, pp, clip_id)
    chat_path = (scoped_clip / "chat.jsonl") if scoped_clip else (pp / "chat.jsonl")
    configured_root = Path(
        os.environ.get("DESIGN_STUDIO_ROOT", DEFAULT_ROOT)
    ).expanduser().resolve()
    if root.resolve() == configured_root:
        runtime = Path(os.environ.get(
            "HERMES_STUDIO_RUNTIME_ROOT", DEFAULT_RUNTIME
        )).expanduser().resolve()
        store = JobStore(runtime / "studio.db")
        store.initialize()
        store.import_chat_if_empty(
            pp.name, chat_path, clip_id=scoped_clip_id)
        store.append_external_event(
            pp.name, role, content, clip_id=scoped_clip_id)
        store.export_chat(
            pp.name, chat_path, clip_id=scoped_clip_id)
        return
    entry = {"role": role,
             "content": content,
             "ts": _dt.datetime.now().isoformat(timespec="seconds")}
    # O_APPEND + single write: atomic enough for line-sized records even with
    # concurrent writers (threads / CLI + webapp).
    data = json.dumps(entry, ensure_ascii=False).encode("utf-8") + b"\n"
    lock_path = chat_path.parent / ".chat.lock"
    if lock_path.is_symlink() or chat_path.is_symlink():
        raise ValueError("project chat files may not be symlinks")
    lock_fd = os.open(
        lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            fd = os.open(
                chat_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- generation
_h3_history_metadata = generation_archive_core._h3_history_metadata


def archive_outputs(root: Path, project: str, clip_id: str,
                    outputs: list[str], metadata: dict | None = None,
                    source_root: Path | None = None,
                    transport: str = "comfyui-mcp",
                    prompt_text: str | None = None) -> Path:
    """CLI compatibility adapter for the canonical archive implementation."""
    return generation_archive_core.archive_outputs(
        root,
        project,
        clip_id,
        outputs,
        metadata,
        source_root=source_root or COMFY_OUTPUT,
        transport=transport,
        prompt_text=prompt_text,
        copier=copy_opened_file,
        publisher=atomic_publish_directory,
    )




def run_generation(root: Path, project: str, clip_id: str,
                   handoff: str | None = None,
                   extra_args: list[str] | None = None,
                   timeout: int = 7200, dry_run: bool = False) -> dict:
    """Submit an H3 generation via the proven run_h3.py runner, then archive."""
    clip_path(root, project, clip_id)
    cmd = [sys.executable, str(RUN_H3), "--comfy-root", str(COMFY_ROOT),
           "-o", "/dev/stdout"]
    if handoff:
        h = Path(handoff).expanduser()
        if not h.exists():  # same archive fallback run_h3.py uses
            h = REAL_HOME / "Documents/MinimaxH3" / h.name
        if not h.exists():
            raise FileNotFoundError(f"handoff not found: {handoff}")
        cmd += ["--handoff", str(h)]
    if extra_args:
        cmd += extra_args

    if dry_run:
        result = subprocess.run(cmd + ["--dry-run"], capture_output=True, text=True)
        return {"dry_run": True, "ok": result.returncode == 0,
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]}

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

    meta = {"generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "handoff": str(handoff or ""), "runner": str(RUN_H3),
            "vram_cleanup": cleanup, **summary}

    video = None
    for key in ("video", "output", "path"):  # locate produced file in summary
        v = summary.get(key)
        if v and Path(v).exists():
            video = Path(v)
            break
    if not video:
        return {"ok": False, "error": "runner completed without a video output",
                "summary": summary, "vram_cleanup": cleanup}
    meta["source"] = str(video)
    gen_dir = archive_outputs(
        root, project, clip_id, [str(video)], meta, source_root=COMFY_OUTPUT,
        transport="legacy-direct")
    # preview.jpg is created by the reviewer/user; do not extract frames automatically.
    return {"ok": cleanup.get("ok", False), "generation": str(gen_dir),
            "meta": meta}


def run_image_generation(root: Path, project: str, clip_id: str,
                         recipe: str, prompt: str = "",
                         image: str | None = None, extra_args: list[str] | None = None,
                         timeout: int = 900) -> dict:
    """Krea 2 still image via scripts/krea2_image.py, archived like generations."""
    clip_path(root, project, clip_id)
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

    if not files:
        return {"ok": False, "error": "runner completed without image outputs",
                "prompt_id": prompt_id}
    meta = {"generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "kind": "image", "recipe": recipe, "prompt": prompt,
            "input_image": str(image or ""), "seed": seed,
            "prompt_id": prompt_id}
    gen_dir = archive_outputs(
        root, project, clip_id, files, meta, source_root=COMFY_OUTPUT,
        transport="legacy-direct", prompt_text=prompt)
    meta = json.loads((gen_dir / "meta.json").read_text(encoding="utf-8"))
    return {"ok": True, "generation": str(gen_dir), "meta": meta}


# ---------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="studio root (default: $DESIGN_STUDIO_ROOT or repo studio-root/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("create-project")
    sp.add_argument("name"); sp.add_argument("brief", nargs="*", default=[])

    sub.add_parser("list-projects")

    sp = sub.add_parser("migrate-clips")
    mode = sp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    sp.add_argument("project", nargs="?")

    sp = sub.add_parser("list-clips")
    sp.add_argument("project")

    sp = sub.add_parser("create-clip")
    sp.add_argument("project"); sp.add_argument("title")

    sp = sub.add_parser("update-clip")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("--title")
    enabled = sp.add_mutually_exclusive_group()
    enabled.add_argument("--enable", dest="enabled", action="store_true")
    enabled.add_argument("--disable", dest="enabled", action="store_false")
    sp.set_defaults(enabled=None)

    sp = sub.add_parser("reorder-clips")
    sp.add_argument("project"); sp.add_argument("clips", nargs="+")

    sp = sub.add_parser("select-take")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("generation", nargs="?"); sp.add_argument("filename", nargs="?")
    sp.add_argument("--clear", action="store_true")

    sp = sub.add_parser("write-prompt")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("prompt", nargs="+")

    sp = sub.add_parser("append-chat")
    sp.add_argument("project"); sp.add_argument("role"); sp.add_argument("content")
    sp.add_argument("--clip")

    sp = sub.add_parser("generate")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("--handoff")
    sp.add_argument("--arg", action="append", default=[],
                    help="extra run_h3.py arg, repeatable. '--arg --turbo' style: use e.g. --arg=--mp --arg=0.9")
    sp.add_argument("--timeout", type=int, default=7200)
    sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("generate-image")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("--recipe", required=True,
                    choices=["t2i", "t2i-nvfp4", "style-ref", "upscale", "edit"])
    sp.add_argument("--prompt", default="")
    sp.add_argument("--image", help="input image for style-ref / upscale / edit")
    sp.add_argument("--ref-boost", type=float, default=None, help="edit recipe")
    sp.add_argument("--arg", action="append", default=[],
                    help="extra krea2_image.py arg, e.g. --arg=--aspect --arg=16:9")
    sp.add_argument("--timeout", type=int, default=900)

    sp = sub.add_parser("archive-output")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("outputs", nargs="+")
    sp.add_argument("--prompt-id", default="")
    sp.add_argument("--kind", default="")
    sp.add_argument("--recipe", default="")
    sp.add_argument("--meta-json", default="{}")

    sp = sub.add_parser("archive-grok")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("outputs", nargs="+")
    sp.add_argument("--prompt-id", default="")
    sp.add_argument("--meta-json", default="{}")

    sp = sub.add_parser("dispatch-grok")
    sp.add_argument("project")
    sp.add_argument("task")
    sp.add_argument("--timeout", type=int, default=600)
    sp.add_argument("--clip")

    sp = sub.add_parser("dispatch-profile")
    sp.add_argument("project")
    sp.add_argument("profile", choices=sorted(LOCAL_SPECIALIST_PROFILES))
    sp.add_argument("task")
    sp.add_argument("--timeout", type=int, default=1800)
    sp.add_argument("--clip")

    args = ap.parse_args(argv)
    root = studio_root(
        args.root,
        create=not (args.cmd == "migrate-clips" and args.dry_run),
    )

    if args.cmd == "create-project":
        print(create_project(root, args.name, " ".join(args.brief)))
    elif args.cmd == "list-projects":
        print("\n".join(list_projects(root)) or "(no projects)")
    elif args.cmd == "migrate-clips":
        from studio_core.migration import migrate_clips

        print(json.dumps(
            migrate_clips(
                root, args.project, apply=args.apply, clip_store=CLIP_STORE),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ))
    elif args.cmd == "list-clips":
        pp = project_path(root, args.project)
        print(json.dumps(CLIP_STORE.describe(pp), indent=2, ensure_ascii=False))
    elif args.cmd == "create-clip":
        pp = project_path(root, args.project)
        print(json.dumps(
            CLIP_STORE.create_clip(pp, args.title), indent=2, ensure_ascii=False))
    elif args.cmd == "update-clip":
        pp = project_path(root, args.project)
        print(json.dumps(CLIP_STORE.update_clip(
            pp, args.clip, title=args.title, enabled=args.enabled),
            indent=2, ensure_ascii=False))
    elif args.cmd == "reorder-clips":
        pp = project_path(root, args.project)
        print(json.dumps(
            CLIP_STORE.reorder(pp, args.clips), indent=2, ensure_ascii=False))
    elif args.cmd == "select-take":
        if args.clear and (args.generation is not None or args.filename is not None):
            ap.error("--clear cannot be combined with a generation or filename")
        if not args.clear and (args.generation is None or args.filename is None):
            ap.error("selection requires both generation and filename, or --clear")
        pp = project_path(root, args.project)
        print(json.dumps(CLIP_STORE.select_take(
            pp, args.clip, None if args.clear else args.generation,
            None if args.clear else args.filename), indent=2, ensure_ascii=False))
    elif args.cmd == "write-prompt":
        print(write_prompt(
            root, args.project, args.clip, " ".join(args.prompt)))
    elif args.cmd == "append-chat":
        append_chat(
            root, args.project, args.role, args.content, clip_id=args.clip)
        print("appended")
    elif args.cmd == "generate":
        extra = []
        for a in args.arg:
            extra.extend(shlex.split(a))
        out = run_generation(root, args.project, args.clip, args.handoff, extra,
                             timeout=args.timeout, dry_run=args.dry_run)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") or out.get("dry_run") else 1
    elif args.cmd == "generate-image":
        extra = []
        for a in args.arg:
            extra.extend(shlex.split(a))
        if args.ref_boost is not None:
            extra += ["--ref-boost", str(args.ref_boost)]
        out = run_image_generation(
            root, args.project, args.clip, args.recipe, args.prompt,
            image=args.image, extra_args=extra, timeout=args.timeout)
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
        print(archive_outputs(
            root, args.project, args.clip, args.outputs, meta))
    elif args.cmd == "archive-grok":
        try:
            meta = json.loads(args.meta_json)
        except json.JSONDecodeError as exc:
            ap.error(f"invalid --meta-json: {exc}")
        if not isinstance(meta, dict):
            ap.error("--meta-json must be a JSON object")
        if args.prompt_id:
            meta["prompt_id"] = args.prompt_id
        meta.setdefault("kind", "image")
        meta.setdefault("recipe", "grok-imagine-image-quality")
        print(archive_outputs(
            root, args.project, args.clip, args.outputs, meta,
            source_root=GROK_IMAGE_OUTPUT, transport="xai-imagine"))
    elif args.cmd == "dispatch-grok":
        print(dispatch_grok(
            root, args.project, args.task, args.timeout, clip_id=args.clip))
    elif args.cmd == "dispatch-profile":
        print(dispatch_profile(
            root, args.project, args.profile, args.task, args.timeout,
            clip_id=args.clip))
    return 0


if __name__ == "__main__":
    sys.exit(main())
