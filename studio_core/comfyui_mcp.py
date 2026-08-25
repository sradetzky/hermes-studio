from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from studio_core.generation_contracts import executed_generation_prompt
from studio_core.safe_files import (
    SafeFilesystemError,
    open_regular_file,
    read_opened_bytes,
)

MCPORTER = "mcporter@0.13.7"
COMFYUI_MCP = "comfyui-mcp@0.52.61"
_FILENAME_RE = re.compile(r"Filename:\s*([^\s]+)")
McpCall = Callable[[str, dict[str, Any], dict[str, str], int], Any]


def unwrap_content(value: Any) -> Any:
    if isinstance(value, dict) and value.get("isError") is True:
        detail = unwrap_content({key: item for key, item in value.items() if key != "isError"})
        raise RuntimeError(f"comfyui-mcp tool failed: {detail}")
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        texts = [
            item["text"] for item in value["content"]
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return texts[0]
    if isinstance(value, dict) and isinstance(value.get("result"), str):
        try:
            return json.loads(value["result"])
        except json.JSONDecodeError:
            pass
    return value


def call_mcp(
        tool: str, arguments: dict[str, Any], environment: dict[str, str],
        timeout: int = 180) -> Any:
    command = [
        "npx", "-y", MCPORTER,
        "call", "--stdio", f"npx -y {COMFYUI_MCP}", tool,
        "--args", json.dumps(arguments, separators=(",", ":"), ensure_ascii=False),
        "--timeout", str(timeout * 1000),
        "--output", "json",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout + 15,
        env=environment,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"comfyui-mcp {tool} failed ({completed.returncode}): {detail}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"comfyui-mcp {tool} returned invalid JSON") from exc
    return unwrap_content(result)


def mcp_environment(
        comfyui_url: str, comfyui_path: Path, comfyui_python: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "COMFYUI_URL": comfyui_url,
        "COMFYUI_PATH": str(comfyui_path),
        "COMFYUI_PYTHON": str(comfyui_python),
        "COMFYUI_JOB_TIMEOUT_S": "10800",
        "COMFYUI_JOB_POLL_INTERVAL_S": "2",
    })
    return environment


def _read_regular(path: Path) -> bytes:
    try:
        with open_regular_file(path) as opened:
            return read_opened_bytes(opened)
    except (FileNotFoundError, OSError, SafeFilesystemError) as exc:
        raise ValueError(f"expected a regular file: {path}") from exc


def _validate_settings_contract(
        path: Path, prompt_sha256: str, updated_at: str) -> None:
    settings = json.loads(_read_regular(path).decode("utf-8"))
    if settings.get("prompt_sha256") != prompt_sha256:
        raise ValueError("current settings prompt SHA-256 no longer matches the token")
    if settings.get("updated_at") != updated_at:
        raise ValueError("current settings timestamp no longer matches the token")


def _upload_filename(result: Any) -> str:
    value = unwrap_content(result)
    if isinstance(value, dict):
        value = value.get("filename") or value.get("result")
    if isinstance(value, str):
        match = _FILENAME_RE.search(value)
        filename = match.group(1) if match else value.strip()
        if filename and "\n" not in filename:
            return filename
    raise RuntimeError("comfyui-mcp upload_image did not return a filename")


def _batch_result(result: Any) -> tuple[str, str]:
    value = unwrap_content(result)
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


def submit_exact_h3_graph(
        graph_path: Path, prompt_file: Path, settings_file: Path,
        prompt_sha256: str, settings_updated_at: str, images: list[Path],
        environment: dict[str, str], *, mcp_call: McpCall = call_mcp) -> dict[str, Any]:
    graph_document = json.loads(_read_regular(graph_path).decode("utf-8"))
    graph = graph_document.get("prompt_stage1_h3")
    if not isinstance(graph, dict) or not graph:
        raise ValueError("graph JSON is missing prompt_stage1_h3")

    prompt_bytes = _read_regular(prompt_file)
    if hashlib.sha256(prompt_bytes).hexdigest() != prompt_sha256:
        raise ValueError("current prompt SHA-256 no longer matches the generation token")
    prompt = executed_generation_prompt(prompt_bytes.decode("utf-8"))
    graph_prompts = [
        node.get("inputs", {}).get("prompt")
        for node in graph.values()
        if isinstance(node, dict) and isinstance(node.get("inputs"), dict)
    ]
    if prompt not in graph_prompts:
        raise ValueError("graph prompt does not exactly match current_prompt.txt")
    _validate_settings_contract(settings_file, prompt_sha256, settings_updated_at)

    image_map: dict[str, str] = {}
    for image in images:
        _read_regular(image)
        uploaded = mcp_call(
            "upload_image",
            {"action": "image", "source_path": str(image)},
            environment,
            180,
        )
        image_map[image.name] = _upload_filename(uploaded)
    for node in graph.values():
        if not isinstance(node, dict) or node.get("class_type") != "LoadImage":
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict) and inputs.get("image") in image_map:
            inputs["image"] = image_map[inputs["image"]]

    _validate_settings_contract(settings_file, prompt_sha256, settings_updated_at)
    if hashlib.sha256(_read_regular(prompt_file)).hexdigest() != prompt_sha256:
        raise ValueError("current prompt changed immediately before submission")
    batch_id, prompt_id = _batch_result(mcp_call(
        "batch",
        {"action": "submit", "workflows": [graph], "disable_random_seed": True},
        environment,
        180,
    ))
    return {
        "status": "submitted",
        "batch_id": batch_id,
        "prompt_id": prompt_id,
        "images": image_map,
    }


def cleanup_comfyui(
        environment: dict[str, str], *, prompt_id: str | None = None,
        cancel: bool = False, mcp_call: McpCall = call_mcp) -> None:
    cleanup_error: Exception | None = None
    if cancel:
        arguments: dict[str, Any] = {"action": "cancel", "clear_pending": True}
        if prompt_id:
            arguments["prompt_id"] = prompt_id
        try:
            mcp_call("queue", arguments, environment, 180)
        except Exception as exc:
            cleanup_error = exc
        try:
            queue = mcp_call("queue", {"action": "list"}, environment, 180)
            if (not isinstance(queue, dict) or queue.get("running") != 0
                    or queue.get("pending") != 0):
                raise RuntimeError("ComfyUI queue did not verify empty after cancellation")
        except Exception as exc:
            if cleanup_error is not None:
                exc.add_note(f"Queue cancellation also failed: {cleanup_error}")
            cleanup_error = exc
        else:
            cleanup_error = None
    try:
        result = mcp_call(
            "clear_vram",
            {"unload_models": True, "free_memory": True},
            environment,
            180,
        )
        if isinstance(result, str) and result.startswith("Failed to free VRAM"):
            raise RuntimeError(result)
    except Exception as exc:
        if cleanup_error is not None:
            exc.add_note(f"Queue cleanup also failed: {cleanup_error}")
        raise
    if cleanup_error is not None:
        raise cleanup_error
