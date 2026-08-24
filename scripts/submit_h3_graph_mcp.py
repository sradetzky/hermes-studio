#!/usr/bin/env python3
"""Submit an exact H3 graph through pinned comfyui-mcp tooling."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

MCPORTER = "mcporter@0.13.7"
COMFYUI_MCP = "comfyui-mcp@0.52.61"
_FILENAME_RE = re.compile(r"Filename:\s*([^\s]+)")


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


def _read_regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected a regular file: {path}")
    return path.read_bytes()


def _call_mcp(
        tool: str, arguments: dict[str, Any], environment: dict[str, str]) -> Any:
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
        timeout=180,
        env=environment,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"comfyui-mcp {tool} failed ({completed.returncode}): {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"comfyui-mcp {tool} returned invalid JSON") from exc


def _upload_filename(result: Any) -> str:
    value = _unwrap_content(result)
    if isinstance(value, dict):
        value = value.get("filename") or value.get("result")
    if isinstance(value, str):
        match = _FILENAME_RE.search(value)
        filename = match.group(1) if match else value.strip()
        if filename and "\n" not in filename:
            return filename
    raise RuntimeError("comfyui-mcp upload_image did not return a filename")


def _unwrap_content(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        texts: list[str] = []
        for item in value["content"]:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return texts[0]
    return value


def _batch_result(result: Any) -> tuple[str, str]:
    value = _unwrap_content(result)
    if isinstance(value, dict) and isinstance(value.get("result"), str):
        try:
            value = json.loads(value["result"])
        except json.JSONDecodeError:
            pass
    if not isinstance(value, dict):
        raise RuntimeError("comfyui-mcp batch returned an invalid result")
    batch_id = value.get("batch_id")
    prompt_ids = value.get("prompt_ids")
    if not isinstance(batch_id, str) or not batch_id:
        raise RuntimeError("comfyui-mcp batch result is missing batch_id")
    if not isinstance(prompt_ids, list) or len(prompt_ids) != 1:
        raise RuntimeError("comfyui-mcp batch result must contain one prompt_id")
    prompt_id = prompt_ids[0]
    if not isinstance(prompt_id, str) or not prompt_id:
        raise RuntimeError("comfyui-mcp batch result contains an invalid prompt_id")
    return batch_id, prompt_id


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
        mcp_environment = os.environ.copy()
        mcp_environment.update({
            "COMFYUI_URL": args.comfyui_url,
            "COMFYUI_PATH": str(args.comfyui_path),
            "COMFYUI_PYTHON": str(args.comfyui_python),
            "COMFYUI_JOB_TIMEOUT_S": "10800",
            "COMFYUI_JOB_POLL_INTERVAL_S": "2",
        })
        graph_document = json.loads(_read_regular(args.graph_json).decode("utf-8"))
        graph = graph_document.get("prompt_stage1_h3")
        if not isinstance(graph, dict) or not graph:
            raise ValueError("graph JSON is missing prompt_stage1_h3")

        prompt_bytes = _read_regular(args.prompt_file)
        actual_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
        if actual_sha256 != args.prompt_sha256:
            raise ValueError("current prompt SHA-256 no longer matches the generation token")
        prompt = prompt_bytes.decode("utf-8").strip()
        graph_prompts = [
            node.get("inputs", {}).get("prompt")
            for node in graph.values()
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict)
        ]
        if prompt not in graph_prompts:
            raise ValueError("graph prompt does not exactly match current_prompt.txt")

        image_map: dict[str, str] = {}
        for image in args.image:
            _read_regular(image)
            uploaded = _call_mcp(
                "upload_image",
                {"action": "image", "source_path": str(image)},
                mcp_environment,
            )
            image_map[image.name] = _upload_filename(uploaded)
        for node in graph.values():
            if not isinstance(node, dict) or node.get("class_type") != "LoadImage":
                continue
            inputs = node.get("inputs")
            if isinstance(inputs, dict) and inputs.get("image") in image_map:
                inputs["image"] = image_map[inputs["image"]]

        settings = json.loads(_read_regular(args.settings_file).decode("utf-8"))
        if settings.get("prompt_sha256") != args.prompt_sha256:
            raise ValueError("current settings prompt SHA-256 no longer matches the token")
        if settings.get("updated_at") != args.settings_updated_at:
            raise ValueError("current settings timestamp no longer matches the token")
        if hashlib.sha256(_read_regular(args.prompt_file)).hexdigest() != args.prompt_sha256:
            raise ValueError("current prompt changed immediately before submission")

        batch_id, prompt_id = _batch_result(_call_mcp(
            "batch",
            {"action": "submit", "workflows": [graph], "disable_random_seed": True},
            mcp_environment,
        ))
        result = {
            "status": "submitted",
            "batch_id": batch_id,
            "prompt_id": prompt_id,
            "images": image_map,
        }
        _write_result(args.result_json, result)
        print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))
        return 0
    except Exception as exc:
        _write_result(args.result_json, {"status": "failed", "error": str(exc)})
        raise


if __name__ == "__main__":
    sys.exit(main())
