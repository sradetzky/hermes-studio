from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


class ComfyQueueClient:
    """Read-only, sanitized view of the local ComfyUI queue."""

    def __init__(self, base_url: str, timeout_seconds: float = 1.5):
        self.queue_url = _queue_url(base_url)
        self.timeout_seconds = timeout_seconds

    def snapshot(self) -> dict:
        try:
            request = Request(self.queue_url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read())
            running = _queue_items(payload, "queue_running", "Running", 0)
            pending = _queue_items(payload, "queue_pending", "Pending", 1)
        except (OSError, TimeoutError, ValueError, TypeError):
            return {
                "available": False,
                "running": [],
                "pending": [],
                "error": "ComfyUI queue unavailable",
            }
        return {"available": True, "running": running, "pending": pending}


def _queue_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("COMFYUI_URL must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("COMFYUI_URL must not contain credentials")
    path = f"{parsed.path.rstrip('/')}/queue"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _queue_items(payload: object, primary: str, legacy: str,
                 position_start: int) -> list[dict]:
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
        items.append({"prompt_id": prompt_id, "position": index})
    return items
