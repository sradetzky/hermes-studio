from __future__ import annotations

import json
import time
from threading import Lock
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


_SUMMARY_FIELDS = (
    "recipe", "mode", "kind", "width", "height", "frames",
    "media_seconds", "steps", "accel", "seed",
)


class ComfyQueueClient:
    """Read-only, sanitized view of the local ComfyUI queue."""

    def __init__(self, base_url: str, timeout_seconds: float = 1.5):
        self.base_url = _base_url(base_url)
        self.queue_url = _api_url(self.base_url, "queue")
        self.jobs_url = _api_url(self.base_url, "api/jobs", [
            ("status", "completed"),
            ("sort_by", "created_at"),
            ("sort_order", "desc"),
            ("limit", "1"),
        ])
        self.timeout_seconds = timeout_seconds
        self._running_since: dict[str, float] = {}
        self._summaries: dict[str, dict] = {}
        self._recent_completed: dict | None = None
        self._state_lock = Lock()

    def snapshot(self, *, include_recent: bool = False) -> dict:
        try:
            payload = self._read_json(self.queue_url)
            now_ms = int(time.time() * 1000)
            running = _queue_items(
                payload, "queue_running", "Running", 0, now_ms=now_ms)
            pending = _queue_items(
                payload, "queue_pending", "Pending", 1, now_ms=now_ms,
                include_wait=True)
        except (OSError, TimeoutError, ValueError, TypeError):
            return {
                "available": False,
                "running": [],
                "pending": [],
                "recent_completed": None,
                "error": "ComfyUI queue unavailable",
            }

        with self._state_lock:
            now = time.monotonic()
            active_since = {}
            for item in running:
                prompt_id = item["prompt_id"]
                started = self._running_since.get(prompt_id, now)
                active_since[prompt_id] = started
                item["elapsed_seconds"] = max(0, int(now - started))
            self._running_since = active_since

            for item in running + pending:
                self._remember_summary(item)
            recent_completed = self._latest_completed() if include_recent else None

        return {
            "available": True,
            "running": running,
            "pending": pending,
            "recent_completed": recent_completed,
        }

    def _read_json(self, url: str) -> object:
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read())

    def _remember_summary(self, item: dict) -> None:
        summary = {key: item[key] for key in _SUMMARY_FIELDS if key in item}
        self._summaries[item["prompt_id"]] = summary
        while len(self._summaries) > 32:
            del self._summaries[next(iter(self._summaries))]

    def _latest_completed(self) -> dict | None:
        try:
            payload = self._read_json(self.jobs_url)
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise ValueError("invalid ComfyUI jobs response")
            if not payload["jobs"]:
                self._recent_completed = None
                return None
            job = payload["jobs"][0]
            if not isinstance(job, dict) or job.get("status") != "completed":
                raise ValueError("invalid completed ComfyUI job")
            prompt_id = job.get("id")
            if not isinstance(prompt_id, str) or not prompt_id:
                raise ValueError("invalid completed ComfyUI prompt id")
            if (self._recent_completed is not None
                    and self._recent_completed.get("prompt_id") == prompt_id):
                return self._recent_completed

            summary = self._summaries.get(prompt_id)
            if summary is None:
                detail_url = _api_url(
                    self.base_url, f"api/jobs/{quote(prompt_id, safe='')}")
                detail = self._read_json(detail_url)
                graph = _job_graph(detail)
                summary = _workflow_summary(graph)

            completed: dict[str, object] = {
                "prompt_id": prompt_id,
                "status": "completed",
            }
            started_at = _timestamp_ms(job.get("execution_start_time"))
            completed_at = _timestamp_ms(job.get("execution_end_time"))
            if started_at is not None and completed_at is not None \
                    and completed_at >= started_at:
                completed["execution_seconds"] = round(
                    (completed_at - started_at) / 1000, 3)
            if completed_at is not None:
                completed["completed_at"] = completed_at
            completed.update(summary)
            self._recent_completed = completed
            return completed
        except (OSError, TimeoutError, ValueError, TypeError):
            return self._recent_completed


def _base_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("COMFYUI_URL must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("COMFYUI_URL must not contain credentials")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _api_url(base_url: str, endpoint: str,
             query: list[tuple[str, str]] | None = None) -> str:
    parsed = urlsplit(base_url)
    path = f"{parsed.path.rstrip('/')}/{endpoint.lstrip('/')}"
    return urlunsplit((
        parsed.scheme, parsed.netloc, path, urlencode(query or []), "",
    ))


def _queue_items(payload: object, primary: str, legacy: str,
                 position_start: int, *, now_ms: int,
                 include_wait: bool = False) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("invalid ComfyUI queue response")
    raw_items = payload.get(primary, payload.get(legacy))
    if not isinstance(raw_items, list):
        raise ValueError("invalid ComfyUI queue response")
    items = []
    for index, raw in enumerate(raw_items, start=position_start):
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            raise ValueError("invalid ComfyUI queue item")
        prompt_id = raw[1]
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ValueError("invalid ComfyUI prompt id")
        item = {"prompt_id": prompt_id, "position": index}
        if len(raw) >= 3:
            item.update(_workflow_summary(raw[2]))
        if include_wait and len(raw) >= 4 and isinstance(raw[3], dict):
            created_at = _timestamp_ms(raw[3].get("create_time"))
            if created_at is not None:
                item["queued_seconds"] = max(0, (now_ms - created_at) // 1000)
        items.append(item)
    return items


def _job_graph(payload: object) -> object:
    if not isinstance(payload, dict):
        raise ValueError("invalid ComfyUI job detail")
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError("invalid ComfyUI job workflow")
    return workflow.get("prompt")


def _workflow_summary(graph: object) -> dict:
    if not isinstance(graph, dict):
        return {}
    nodes = []
    for raw in graph.values():
        if not isinstance(raw, dict):
            continue
        class_type = raw.get("class_type")
        inputs = raw.get("inputs")
        if isinstance(class_type, str) and isinstance(inputs, dict):
            nodes.append((class_type, inputs))

    summary = {}
    h3_cond = _first_node(
        nodes, {"MiniMaxH3ReferenceToVideo", "MiniMaxH3ImageToVideo"})
    if h3_cond is not None:
        class_type, inputs = h3_cond
        summary.update({"recipe": "H3", "kind": "video"})
        if class_type == "MiniMaxH3ReferenceToVideo":
            summary["mode"] = "R2V"
        elif "last_frame" in inputs:
            summary["mode"] = "FL2VA"
        elif "first_frame" in inputs:
            summary["mode"] = "I2VA"
        else:
            summary["mode"] = "T2VA"
        _add_dimensions(summary, inputs)
        frames = _bounded_int(inputs.get("length"), 1, 1_000_000)
        if frames is not None:
            summary["frames"] = frames
        summary["accel"] = all(
            any(class_type == expected and inputs.get("enabled") is True
                for class_type, inputs in nodes)
            for expected in (
                "MiniMaxH3FusedModulation", "MiniMaxH3ChunkFeedForward"))
    elif any(
            class_type == "CLIPLoader" and inputs.get("type") == "krea2"
            or class_type.startswith("NO8DKrea2")
            for class_type, inputs in nodes):
        summary.update({"recipe": "Krea 2", "kind": "image"})
        latent = _first_node(nodes, {"EmptyLatentImage"})
        if latent is not None:
            _add_dimensions(summary, latent[1])

    scheduler = _first_node(nodes, {"BasicScheduler"})
    sampler = _first_node(nodes, {"KSampler"})
    step_inputs = scheduler[1] if scheduler is not None else (
        sampler[1] if sampler is not None else {})
    steps = _bounded_int(step_inputs.get("steps"), 1, 10_000)
    if steps is not None:
        summary["steps"] = steps

    noise = _first_node(nodes, {"RandomNoise"})
    seed_value = noise[1].get("noise_seed") if noise is not None else (
        sampler[1].get("seed") if sampler is not None else None)
    seed = _bounded_int(seed_value, 0, 2 ** 64 - 1)
    if seed is not None:
        summary["seed"] = str(seed)

    if "frames" in summary:
        video = _first_node(nodes, {"CreateVideo"})
        fps = _bounded_int(video[1].get("fps"), 1, 1_000) \
            if video is not None else None
        if fps is not None:
            summary["media_seconds"] = max(1, round(summary["frames"] / fps))
    return summary


def _first_node(nodes: list[tuple[str, dict]],
                class_types: set[str]) -> tuple[str, dict] | None:
    return next((node for node in nodes if node[0] in class_types), None)


def _add_dimensions(summary: dict, inputs: dict) -> None:
    width = _bounded_int(inputs.get("width"), 1, 16_384)
    height = _bounded_int(inputs.get("height"), 1, 16_384)
    if width is not None and height is not None:
        summary.update({"width": width, "height": height})


def _bounded_int(value: object, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    integer = int(value)
    if integer != value or not minimum <= integer <= maximum:
        return None
    return integer


def _timestamp_ms(value: object) -> int | None:
    return _bounded_int(value, 0, 10 ** 16)
