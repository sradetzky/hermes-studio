#!/usr/bin/env python3
"""Submit an exact H3 graph through pinned comfyui-mcp tooling."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from studio_core.comfyui_mcp import (
    COMFYUI_MCP,
    MCPORTER,
    mcp_environment,
    submit_exact_h3_graph,
    unwrap_content,
)



def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload H3 references and submit an exact graph through comfyui-mcp")
    parser.add_argument("--graph-json", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--settings-file", type=Path, required=True)
    parser.add_argument("--prompt-sha256", required=True)
    parser.add_argument("--settings-updated-at", required=True)
    parser.add_argument("--image", action="append", type=Path, default=[])
    parser.add_argument("--comfyui-url", default="http://127.0.0.1:8188")
    parser.add_argument("--comfyui-path", type=Path, required=True)
    parser.add_argument("--comfyui-python", type=Path, required=True)
    parser.add_argument("--result-json", type=Path, required=True)
    return parser.parse_args(argv)


def _call_mcp(
        tool: str, arguments: dict, environment: dict[str, str],
        timeout: int = 180):
    command = [
        "npx", "-y", MCPORTER,
        "call", "--stdio", f"npx -y {COMFYUI_MCP}", tool,
        "--args", json.dumps(arguments, separators=(",", ":"), ensure_ascii=False),
        "--output", "json",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"comfyui-mcp {tool} failed ({completed.returncode}): {detail}")
    try:
        return unwrap_content(json.loads(completed.stdout))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"comfyui-mcp {tool} returned invalid JSON") from exc


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.result_json.exists() or args.result_json.is_symlink():
        raise ValueError(f"result file already exists; refusing duplicate submission: {args.result_json}")
    args.result_json.parent.mkdir(parents=True, exist_ok=True)
    with args.result_json.open("x", encoding="utf-8") as handle:
        handle.write('{"status":"submitting"}\n')
        handle.flush()
        os.fsync(handle.fileno())

    try:
        environment = mcp_environment(
            args.comfyui_url, args.comfyui_path, args.comfyui_python)
        result = submit_exact_h3_graph(
            args.graph_json,
            args.prompt_file,
            args.settings_file,
            args.prompt_sha256,
            args.settings_updated_at,
            args.image,
            environment,
            mcp_call=_call_mcp,
        )
        _write_result(args.result_json, result)
        print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))
        return 0
    except Exception as exc:
        _write_result(args.result_json, {"status": "failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    sys.exit(main())
