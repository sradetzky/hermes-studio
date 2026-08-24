from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections import defaultdict, deque
from contextlib import closing
from typing import Any

from webapp.config import Settings
from webapp.job_store import JobStore
from webapp.models import Job


log = logging.getLogger(__name__)
_SAFE_ARGUMENT_KEYS = {
    "action", "file_path", "goal", "image_url", "name", "path", "pattern",
    "profile", "project", "query", "selector", "target", "url",
}


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_arguments(value: Any) -> dict:
    arguments = _json_object(value)
    safe = {}
    for key in _SAFE_ARGUMENT_KEYS:
        item = arguments.get(key)
        if isinstance(item, (str, int, float, bool)):
            safe[key] = str(item)[:240]
    return safe


def _tool_summary(tool_name: str, arguments: dict, completed: bool) -> str:
    verb = "Used" if completed else "Using"
    path = arguments.get("path") or arguments.get("file_path")
    name = arguments.get("name")
    action = arguments.get("action")
    summaries = {
        "read_file": ("Read", "Reading"),
        "write_file": ("Wrote", "Writing"),
        "patch": ("Updated", "Updating"),
        "search_files": ("Searched files", "Searching files"),
        "skill_view": ("Loaded skill", "Loading skill"),
        "vision_analyze": ("Inspected media", "Inspecting media"),
        "terminal": ("Ran a terminal command", "Running a terminal command"),
        "delegate_task": ("Completed a profile handoff", "Handing work to a profile"),
        "mcp__comfyui__queue": ("Checked the ComfyUI queue", "Checking the ComfyUI queue"),
        "mcp__comfyui__enqueue_workflow": ("Queued a ComfyUI workflow", "Queueing a ComfyUI workflow"),
        "mcp__comfyui__upload_image": ("Uploaded a ComfyUI reference", "Uploading a ComfyUI reference"),
        "mcp__comfyui__clear_vram": ("Released ComfyUI VRAM", "Releasing ComfyUI VRAM"),
        "mcp__comfyui__create_workflow": ("Prepared a ComfyUI workflow", "Preparing a ComfyUI workflow"),
        "mcp__comfyui__get_history": ("Checked ComfyUI history", "Checking ComfyUI history"),
        "mcp__comfyui__get_system_stats": ("Checked ComfyUI status", "Checking ComfyUI status"),
    }
    pair = summaries.get(tool_name)
    if pair:
        summary = pair[0] if completed else pair[1]
        suffix = path or name
        if suffix and tool_name in {"read_file", "write_file", "skill_view"}:
            summary += f" {suffix}"
        return summary
    suffix = f" ({action})" if action else ""
    return f"{verb} {tool_name}{suffix}"


def _tool_failed(content: str) -> bool:
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        lowered = (content or "").lower()
        return "error" in lowered and "error\": null" not in lowered
    if not isinstance(parsed, dict):
        return False
    return parsed.get("success") is False or bool(parsed.get("error"))


class HermesSessionEventBridge:
    """Project structured Hermes session records into Studio job events."""

    def __init__(self, store: JobStore, settings: Settings, job: Job,
                 source: str, started_at: float,
                 session_id: str | None = None):
        self.store = store
        self.settings = settings
        self.job = job
        self.source = source
        self.started_at = started_at
        self.session_id = session_id
        self.database_path = settings.profile_state_path(job.profile)
        self.cursor = 0
        self._baseline_ready = session_id is None
        self._open_tools: dict[
            str, deque[tuple[str | None, float, dict]]
        ] = defaultdict(deque)
        self._disabled = False
        self._connected = False

    def prepare(self) -> bool:
        if not self.session_id:
            self._baseline_ready = True
            return True
        if not self.database_path.is_file():
            self._baseline_ready = False
            return False
        try:
            with closing(self._connection()) as connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(id), 0) AS cursor FROM messages "
                    "WHERE session_id = ?", (self.session_id,),
                ).fetchone()
                self.cursor = row["cursor"]
            self._baseline_ready = True
            return True
        except sqlite3.Error:
            log.debug("Could not prepare Hermes event bridge", exc_info=True)
            self._baseline_ready = False
            return False

    def poll(self) -> None:
        if self._disabled or not self.database_path.is_file():
            return
        try:
            if self.session_id and not self._baseline_ready:
                if not self.prepare():
                    return
            with closing(self._connection()) as connection:
                if not self.session_id:
                    self.session_id = self._discover_session(connection)
                    if not self.session_id:
                        return
                if not self._connected:
                    self.store.append_job_event(
                        self.job.id,
                        self.job.profile,
                        "profile.connected",
                        f"Connected to {self.job.profile} session",
                        status="running",
                        detail={"session_id": self.session_id},
                    )
                    self._connected = True
                rows = connection.execute(
                    "SELECT id, role, content, tool_call_id, tool_name, tool_calls, "
                    "reasoning, reasoning_content, timestamp "
                    "FROM messages WHERE session_id = ? AND id > ? "
                    "ORDER BY id",
                    (self.session_id, self.cursor),
                ).fetchall()
            for row in rows:
                self._project_row(row)
                self.cursor = row["id"]
        except sqlite3.Error:
            log.debug("Could not poll Hermes session events", exc_info=True)
        except Exception:
            log.exception("Hermes event bridge failed for job %s", self.job.id)
            self._disabled = True

    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=0.25)
        connection.row_factory = sqlite3.Row
        return connection

    def _discover_session(self, connection: sqlite3.Connection) -> str | None:
        row = connection.execute(
            "SELECT id FROM sessions WHERE source = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (self.source,),
        ).fetchone()
        return row["id"] if row else None

    def _project_row(self, row: sqlite3.Row) -> None:
        role = row["role"]
        timestamp = float(row["timestamp"] or time.time())
        if role == "assistant":
            reasoning = (row["reasoning_content"] or row["reasoning"] or "").strip()
            if reasoning:
                visible = reasoning[:4000]
                self.store.append_job_event(
                    self.job.id,
                    self.job.profile,
                    "reasoning",
                    visible[:500],
                    status="running",
                    detail={"text": visible},
                )
            content = (row["content"] or "").strip()
            calls = self._tool_calls(row["tool_calls"])
            if content and calls:
                self.store.append_job_event(
                    self.job.id,
                    self.job.profile,
                    "commentary",
                    content[:500],
                    status="running",
                    detail={"text": content[:4000]},
                )
            for call_id, tool_name, arguments in calls:
                safe = _safe_arguments(arguments)
                self._open_tools[tool_name].append((call_id, timestamp, safe))
                self.store.append_job_event(
                    self.job.id,
                    self.job.profile,
                    "tool.started",
                    _tool_summary(tool_name, safe, completed=False),
                    status="running",
                    detail={"tool": tool_name, "arguments": safe},
                )
        elif role == "tool":
            tool_name = row["tool_name"] or "tool"
            content = row["content"] or ""
            call_id = row["tool_call_id"]
            opened = self._open_tools[tool_name]
            match = next((index for index, item in enumerate(opened)
                          if call_id and item[0] == call_id), None)
            if match is not None:
                _, started, safe = opened[match]
                del opened[match]
            elif opened:
                _, started, safe = opened.popleft()
            else:
                started, safe = timestamp, {}
            failed = _tool_failed(content)
            self.store.append_job_event(
                self.job.id,
                self.job.profile,
                "tool.completed",
                _tool_summary(tool_name, safe, completed=True),
                status="failed" if failed else "completed",
                detail={
                    "tool": tool_name,
                    "arguments": safe,
                    "duration": round(max(0.0, timestamp - started), 2),
                },
            )

    @staticmethod
    def _tool_calls(value: Any) -> list[tuple[str | None, str, Any]]:
        if not value:
            return []
        try:
            calls = json.loads(value) if isinstance(value, str) else value
        except json.JSONDecodeError:
            return []
        result = []
        for call in calls if isinstance(calls, list) else []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            tool_name = function.get("name") or call.get("name")
            if tool_name:
                result.append((
                    str(call["id"]) if call.get("id") else None,
                    str(tool_name),
                    function.get("arguments") or call.get("arguments"),
                ))
        return result
