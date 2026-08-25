from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import re
import os
import shutil
import urllib.parse
import urllib.request
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from studio_core.generation_contracts import (
    GenerationContract,
    executed_generation_prompt_sha256,
)
from studio_core.paths import StudioPaths
from studio_core.projects import clip_path, next_generation_dir, read_project_text
from studio_core.safe_files import (
    SafeFilesystemError,
    atomic_publish_directory,
    copy_opened_file,
    open_regular_beneath,
    open_regular_file,
)


@dataclass(frozen=True)
class GenerationArchiveContext:
    job_id: str
    project: str
    clip_id: str
    contract: GenerationContract
    prompt_id: str
    comfy_url: str

COMFY_OUTPUT = StudioPaths.from_environment().comfy_root / "output"


def _history_integer(value: object, field: str, minimum: int,
                     maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"ComfyUI history {field} is invalid")
    integer = int(value)
    if integer != value or not minimum <= integer <= maximum:
        raise ValueError(f"ComfyUI history {field} is invalid")
    return integer

def _history_branch_node(graph: dict, branch: set[str],
                         class_type: str) -> tuple[str, dict] | None:
    matches = []
    for node_id in branch:
        node = graph.get(node_id)
        if (isinstance(node, dict) and node.get("class_type") == class_type
                and isinstance(node.get("inputs"), dict)):
            matches.append((node_id, node["inputs"]))
    if len(matches) > 1:
        raise ValueError(
            f"ComfyUI history output branch has ambiguous {class_type} nodes")
    return matches[0] if matches else None

def _history_upstream_branch(graph: dict, output_node_id: str) -> set[str]:
    if output_node_id not in graph:
        raise ValueError("ComfyUI history output node is missing from the graph")
    branch = set()
    pending = [output_node_id]
    while pending:
        node_id = pending.pop()
        if node_id in branch:
            continue
        node = graph.get(node_id)
        if (not isinstance(node, dict)
                or not isinstance(node.get("class_type"), str)
                or not isinstance(node.get("inputs"), dict)):
            raise ValueError("ComfyUI history output branch node is invalid")
        branch.add(node_id)
        values = list(node["inputs"].values())
        while values:
            value = values.pop()
            if isinstance(value, dict):
                values.extend(value.values())
            elif isinstance(value, list):
                if (len(value) == 2 and isinstance(value[0], str)
                        and value[0] in graph and isinstance(value[1], int)
                        and not isinstance(value[1], bool)):
                    pending.append(value[0])
                else:
                    values.extend(value)
    return branch

def _history_reference(graph: dict, branch: set[str], link: object) -> str:
    if (not isinstance(link, list) or len(link) != 2
            or not isinstance(link[0], str) or link[0] not in branch):
        raise ValueError("ComfyUI history reference link is invalid")
    node = graph.get(link[0])
    if (not isinstance(node, dict) or node.get("class_type") != "LoadImage"
            or not isinstance(node.get("inputs"), dict)):
        raise ValueError("ComfyUI history reference node is invalid")
    image = node["inputs"].get("image")
    if not isinstance(image, str) or not image or Path(image).name != image:
        raise ValueError("ComfyUI history reference filename is invalid")
    return image

def _history_output_producers(entry: dict) -> dict[str, set[str]]:
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("ComfyUI history outputs are invalid")
    producers: dict[str, set[str]] = {}
    for node_id, node_output in outputs.items():
        if not isinstance(node_id, str):
            raise ValueError("ComfyUI history output node id is invalid")
        if not isinstance(node_output, dict):
            continue
        for items in node_output.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "output":
                    continue
                filename = item.get("filename")
                subfolder = item.get("subfolder", "")
                if (not isinstance(filename, str) or not filename
                        or "/" in filename or "\\" in filename
                        or not isinstance(subfolder, str)):
                    raise ValueError("ComfyUI history output filename is invalid")
                if (subfolder and ("\\" in subfolder
                        or subfolder.startswith("/")
                        or any(part in {"", ".", ".."}
                               for part in subfolder.split("/")))):
                    raise ValueError("ComfyUI history output subfolder is invalid")
                name = f"{subfolder}/{filename}" if subfolder else filename
                producers.setdefault(name, set()).add(node_id)
    if not producers:
        raise ValueError("ComfyUI history contains no output files")
    return producers

def _h3_history_metadata(
        comfy_url: str, prompt_id: str, outputs: list[str]) -> dict:
    base_url = comfy_url.rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("COMFYUI_URL must be an http(s) URL without credentials")
    encoded_id = urllib.parse.quote(prompt_id, safe="")
    request = urllib.request.Request(
        f"{base_url}/history/{encoded_id}",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("could not read authoritative ComfyUI history") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get(prompt_id), dict):
        raise ValueError("ComfyUI history does not contain the archived prompt")
    entry = payload[prompt_id]
    status = entry.get("status")
    if (not isinstance(status, dict) or status.get("completed") is not True
            or status.get("status_str") != "success"):
        raise ValueError("ComfyUI history prompt is not successfully completed")
    prompt_record = entry.get("prompt")
    if (not isinstance(prompt_record, list) or len(prompt_record) < 5
            or prompt_record[1] != prompt_id or not isinstance(prompt_record[2], dict)):
        raise ValueError("ComfyUI history prompt graph is invalid")
    graph = prompt_record[2]
    output_nodes = prompt_record[4]
    if (not isinstance(output_nodes, list)
            or not all(isinstance(node_id, str) for node_id in output_nodes)):
        raise ValueError("ComfyUI history output-node list is invalid")
    archived_outputs = set()
    for output in outputs:
        if (not isinstance(output, str) or not output or "\\" in output
                or output.startswith("/")
                or any(part in {"", ".", ".."} for part in output.split("/"))):
            raise ValueError("web generation output path is invalid")
        archived_outputs.add(output)
    producers = _history_output_producers(entry)
    if not archived_outputs <= set(producers):
        raise ValueError("archived output does not belong to the ComfyUI prompt")
    producer_ids = {
        node_id for output in archived_outputs for node_id in producers[output]
    }
    if len(producer_ids) != 1:
        raise ValueError("archived outputs do not have one exact producer node")
    output_node_id = next(iter(producer_ids))
    if output_node_id not in output_nodes:
        raise ValueError("archived output producer was not executed")
    branch = _history_upstream_branch(graph, output_node_id)
    output_node = graph[output_node_id]
    if output_node.get("class_type") != "SaveVideo":
        raise ValueError("archived output was not produced by SaveVideo")

    reference_condition = _history_branch_node(
        graph, branch, "MiniMaxH3ReferenceToVideo")
    image_condition = _history_branch_node(
        graph, branch, "MiniMaxH3ImageToVideo")
    if reference_condition is not None and image_condition is not None:
        raise ValueError("ComfyUI history output branch has ambiguous H3 conditioning")
    condition = reference_condition or image_condition
    if condition is None:
        raise ValueError("ComfyUI history does not contain an H3 graph")
    if reference_condition is not None:
        recipe, mode = "h3-ref2va", "r2v"
    else:
        inputs = condition[1]
        if "last_frame" in inputs:
            recipe, mode = "h3-fl2va", "fl2va"
        elif "first_frame" in inputs:
            recipe, mode = "h3-i2va", "i2va"
        else:
            recipe, mode = "h3-t2va", "t2va"
    inputs = condition[1]
    prompt = inputs.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("ComfyUI history H3 prompt is invalid")
    width = _history_integer(inputs.get("width"), "width", 1, 16_384)
    height = _history_integer(inputs.get("height"), "height", 1, 16_384)
    length = _history_integer(inputs.get("length"), "length", 1, 1_000_000)

    noise = _history_branch_node(graph, branch, "RandomNoise")
    scheduler = _history_branch_node(graph, branch, "BasicScheduler")
    video = _history_branch_node(graph, branch, "CreateVideo")
    if noise is None or scheduler is None or video is None:
        raise ValueError("ComfyUI history H3 execution nodes are incomplete")
    seed = _history_integer(
        noise[1].get("noise_seed"), "seed", 0, 2 ** 64 - 1)
    steps = _history_integer(scheduler[1].get("steps"), "steps", 1, 10_000)
    fps = _history_integer(video[1].get("fps"), "fps", 1, 1_000)

    accel_nodes = [
        class_type for class_type in (
            "MiniMaxH3FusedModulation", "MiniMaxH3ChunkFeedForward")
        if ((node := _history_branch_node(graph, branch, class_type)) is not None
            and node[1].get("enabled") is True)
    ]
    references = []
    if mode == "r2v":
        prefix = "ref_images.ref_image_"
        indexed = []
        for key, link in inputs.items():
            if key.startswith(prefix) and key[len(prefix):].isdigit():
                indexed.append((int(key[len(prefix):]), link))
        references = [
            _history_reference(graph, branch, link)
            for _index, link in sorted(indexed)
        ]
    else:
        references = [
            _history_reference(graph, branch, inputs[key])
            for key in ("first_frame", "last_frame") if key in inputs
        ]

    return {
        "prompt_id": prompt_id,
        "output_node_id": output_node_id,
        "kind": "video",
        "recipe": recipe,
        "mode": mode,
        "width": width,
        "height": height,
        "mp": round(width * height / 1_000_000, 3),
        "length": length,
        "duration_sec": round(length / fps, 3),
        "fps": fps,
        "seed": seed,
        "steps": steps,
        "accel": len(accel_nodes) == 2,
        "accel_nodes": accel_nodes,
        "references": references,
        "upscale": False,
        "executed_prompt_sha256": hashlib.sha256(
            prompt.encode("utf-8")).hexdigest(),
    }

def _validate_authoritative_generation_contract(
        authoritative: dict, contract: GenerationContract) -> None:
    settings = contract.settings_manifest
    execution = contract.execution
    expected = {
        "executed_prompt_sha256": executed_generation_prompt_sha256(
            contract.prompt),
        "mode": settings.mode,
        "width": execution.resolution.width,
        "height": execution.resolution.height,
        "length": execution.timing.frames,
        "fps": execution.timing.fps,
        "steps": settings.steps,
        "accel": settings.accel,
        "references": list(execution.references),
    }
    for field, value in expected.items():
        if authoritative.get(field) != value:
            raise ValueError(
                f"ComfyUI history {field} does not match the generation contract")
    if settings.seed is not None:
        if authoritative.get("seed") != settings.seed:
            raise ValueError(
                "ComfyUI history seed does not match the generation contract")


def _generation_metadata(
        project: str, clip_id: str, outputs: list[str], metadata: dict,
        context: GenerationArchiveContext | None,
) -> tuple[dict, GenerationContract | None]:
    if context is None:
        return metadata, None
    if context.project != project or context.clip_id != clip_id:
        raise ValueError("web generation archive target does not match its job")
    if not context.job_id:
        raise ValueError("web generation archive requires a Studio job id")
    if not context.prompt_id:
        raise ValueError("web generation archive requires a prompt_id")
    if (metadata.get("prompt_id", context.prompt_id) != context.prompt_id):
        raise ValueError("web generation archive prompt does not match its context")
    contract = context.contract
    authoritative = _h3_history_metadata(
        context.comfy_url, context.prompt_id, outputs)
    _validate_authoritative_generation_contract(authoritative, contract)
    return {
        **metadata,
        **authoritative,
        "prompt_sha256": contract.prompt_sha256,
        "studio_job_id": context.job_id,
        "generation_contract_version": contract.schema_version,
        "settings_updated_at": contract.settings_updated_at,
        "generation_inputs": contract.execution.to_dict(
            contract.schema_version).get("inputs", []),
    }, contract

def archive_outputs(root: Path, project: str, clip_id: str,
                    outputs: list[str], metadata: dict | None = None,
                    source_root: Path | None = None,
                    transport: str = "comfyui-mcp",
                    prompt_text: str | None = None, *,
                    generation_context: GenerationArchiveContext | None = None,
                    copier=copy_opened_file,
                    publisher=atomic_publish_directory) -> Path:
    """Archive outputs beneath one exact clip from one trusted source root."""
    clip = clip_path(root, project, clip_id)
    archive_metadata, generation_contract = _generation_metadata(
        project, clip_id, outputs, dict(metadata or {}), generation_context)
    output_root = Path(os.path.abspath(
        os.path.expanduser(os.fspath(source_root or COMFY_OUTPUT))))
    with ExitStack() as source_descriptors:
        sources = []
        for output in outputs:
            try:
                source = source_descriptors.enter_context(
                    open_regular_beneath(output_root, output))
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    f"ComfyUI output not found: {output}") from exc
            except SafeFilesystemError as exc:
                raise ValueError(
                    "output may not be a symlink, special file, or escape "
                    f"the ComfyUI output directory: {output!r}"
                ) from exc
            if source.name in {"prompt.txt", "settings.json", "meta.json"}:
                raise ValueError(f"output filename is reserved: {source.name}")
            sources.append(source)
        if not sources:
            raise ValueError("at least one output file is required")

        generations = clip / "generations"
        if (generations.is_symlink() or not generations.is_dir()
                or generations.resolve().parent != clip):
            raise ValueError("generations directory is not a regular clip directory")
        lock_path = clip / ".generation-archive.lock"
        if lock_path.is_symlink():
            raise ValueError("generation archive lock may not be a symlink")
        lock_fd = os.open(
            lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600)
        with os.fdopen(lock_fd, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            gen_dir = next_generation_dir(root, project, clip_id)
            if (generation_contract is not None
                    and gen_dir.name
                    != generation_contract.expected_generation_id):
                raise ValueError(
                    "generation archive sequence does not match its job contract")
            staging = generations / f".publishing-{uuid.uuid4().hex}"
            staging.mkdir()
            copied = []
            try:
                for source in sources:
                    target = staging / source.name
                    if target.exists():
                        raise FileExistsError(
                            f"duplicate output filename: {source.name}")
                    copier(source, target)
                    copied.append(target.name)
                supplied_prompt = (
                    None if prompt_text is None else prompt_text.rstrip() + "\n")
                if (generation_contract is not None and supplied_prompt is not None
                        and supplied_prompt != generation_contract.prompt):
                    raise ValueError(
                        "provided prompt does not match the generation contract")
                archived_prompt = (
                    generation_contract.prompt
                    if generation_contract is not None else
                    read_project_text(clip, "current_prompt.txt", required=True)
                    if supplied_prompt is None else supplied_prompt
                )
                expected_prompt_hash = archive_metadata.get("prompt_sha256")
                if (expected_prompt_hash is not None
                        and hashlib.sha256(archived_prompt.encode("utf-8")).hexdigest()
                        != expected_prompt_hash):
                    raise ValueError(
                        "archived prompt does not match the ComfyUI history")
                (staging / "prompt.txt").write_text(
                    archived_prompt, encoding="utf-8")
                if generation_contract is not None:
                    (staging / "settings.json").write_text(
                        json.dumps(
                            generation_contract.settings_manifest.to_dict(),
                            indent=2,
                            ensure_ascii=False,
                        ) + "\n",
                        encoding="utf-8",
                    )
                else:
                    settings_path = clip / "current_generation.json"
                    try:
                        with open_regular_file(settings_path) as settings:
                            copy_opened_file(settings, staging / "settings.json")
                    except FileNotFoundError:
                        # JSON null is the stable snapshot for an unsaved state.
                        (staging / "settings.json").write_text(
                            "null\n", encoding="utf-8")
                    except SafeFilesystemError as exc:
                        raise ValueError(
                            "current generation settings are not a regular clip file"
                        ) from exc
                meta = {
                    **archive_metadata,
                    "generated": _dt.datetime.now().isoformat(timespec="seconds"),
                    "transport": transport,
                    "files": copied,
                    "sources": [str(source.path) for source in sources],
                }
                (staging / "meta.json").write_text(
                    json.dumps(meta, indent=2) + "\n", encoding="utf-8")
                publisher(staging, gen_dir)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return gen_dir
