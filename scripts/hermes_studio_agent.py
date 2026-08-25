#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from studio_core.interaction_store import InteractionStore
from studio_core.interactions import gateway_answer


class GatewayError(RuntimeError):
    pass


class GatewayClient:
    def __init__(
        self,
        python: Path,
        cwd: Path,
        environment: dict[str, str],
    ) -> None:
        self._stderr: list[str] = []
        self._responses: dict[str, dict[str, Any]] = {}
        self._frames: queue.Queue[dict[str, Any] | Exception] = queue.Queue()
        self.process = subprocess.Popen(
            [str(python), "-m", "tui_gateway.entry"],
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        ready = self._read()
        assert ready is not None
        if (
            ready.get("method") != "event"
            or (ready.get("params") or {}).get("type") != "gateway.ready"
        ):
            raise GatewayError("Hermes gateway did not announce readiness")

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            if sum(len(item) for item in self._stderr) < 16_000:
                self._stderr.append(line)

    def _drain_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    self._frames.put(GatewayError(
                        "Hermes gateway emitted invalid JSON-RPC"))
                    return
                if not isinstance(value, dict):
                    self._frames.put(GatewayError(
                        "Hermes gateway emitted a non-object JSON-RPC frame"))
                    return
                self._frames.put(value)
        finally:
            self._frames.put(GatewayError(
                "Hermes gateway exited before completing the request"))

    def _read(self, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            value = self._frames.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(value, Exception):
            detail = "".join(self._stderr).strip()
            raise GatewayError(
                f"{value}{f': {detail}' if detail else ''}") from value
        return value

    def _send(self, value: dict[str, Any]) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise GatewayError("Hermes gateway is not running")
        self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(
        self,
        method: str,
        params: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })
        while True:
            response = self._responses.pop(request_id, None)
            if response is not None:
                break
            frame = self._read()
            assert frame is not None
            if frame.get("method") == "event":
                on_event(frame.get("params") or {})
                continue
            frame_id = frame.get("id")
            if isinstance(frame_id, str):
                self._responses[frame_id] = frame
        if "error" in response:
            error = response.get("error") or {}
            raise GatewayError(str(error.get("message") or "Hermes gateway request failed"))
        result = response.get("result")
        if not isinstance(result, dict):
            raise GatewayError(f"Hermes gateway returned an invalid {method} result")
        return result

    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        while True:
            frame = self._read(timeout)
            if frame is None:
                return None
            if frame.get("method") == "event":
                params = frame.get("params")
                if isinstance(params, dict):
                    return params
                raise GatewayError("Hermes gateway emitted an invalid event")
            frame_id = frame.get("id")
            if isinstance(frame_id, str):
                self._responses[frame_id] = frame

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--gateway-python", type=Path, required=True)
    value.add_argument("--database", type=Path, required=True)
    value.add_argument("--job-id", required=True)
    value.add_argument("--profile", required=True)
    value.add_argument("--profile-home", type=Path, required=True)
    value.add_argument("--project", required=True)
    value.add_argument("--clip-id", default="")
    value.add_argument("--chat-scope", choices=("project", "clip"), required=True)
    value.add_argument("--session-id")
    value.add_argument("--source", required=True)
    value.add_argument("--cwd", type=Path, required=True)
    value.add_argument("--toolsets", required=True)
    value.add_argument("--prompt", required=True)
    return value


def require_unlimited_clarify(profile_home: Path) -> None:
    config_path = profile_home / "config.yaml"
    if not config_path.is_file():
        raise GatewayError("Hermes profile config is unavailable")
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GatewayError("Hermes profile config is unreadable") from exc
    agent = config.get("agent") if isinstance(config, dict) else None
    timeout = agent.get("clarify_timeout") if isinstance(agent, dict) else None
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or timeout > 0
    ):
        raise GatewayError(
            "Hermes profile agent.clarify_timeout must be unlimited (0) "
            "for durable Studio interactions; run scripts/sync-profiles.sh")


def run(args: argparse.Namespace) -> tuple[str, str]:
    if not args.gateway_python.is_file():
        raise GatewayError("Hermes gateway Python is unavailable")
    if not args.cwd.is_dir():
        raise GatewayError("Hermes gateway workspace is unavailable")
    require_unlimited_clarify(args.profile_home)
    environment = os.environ.copy()
    environment["HERMES_TUI_TOOLSETS"] = args.toolsets
    environment.pop("HERMES_TUI_SIDECAR_URL", None)
    interactions = InteractionStore(args.database)
    gateway = GatewayClient(args.gateway_python, args.cwd, environment)
    live_session_id = ""
    stored_session_id = ""
    final_text = ""
    failure = ""
    active_interaction = None

    def handle_event(event: dict[str, Any]) -> None:
        nonlocal active_interaction, final_text, failure
        event_type = event.get("type")
        if event_type == "clarify.expire":
            payload = event.get("payload") or {}
            request_id = payload.get("request_id") if isinstance(payload, dict) else None
            if (
                active_interaction is None
                or request_id != active_interaction.hermes_request_id
            ):
                raise GatewayError("Hermes expired an unknown clarification")
            interactions.expire(
                active_interaction.id,
                args.job_id,
                active_interaction.hermes_request_id,
            )
            active_interaction = None
            raise GatewayError(
                "Hermes clarification expired before Studio delivered an answer")
        if event_type == "message.complete":
            if active_interaction is not None:
                raise GatewayError(
                    "Hermes completed while a clarification was still pending")
            payload = event.get("payload") or {}
            if isinstance(payload, dict):
                final_text = str(payload.get("text") or "").strip()
                failure = str(payload.get("failure_reason") or "").strip()
            return
        if event_type == "error":
            payload = event.get("payload") or {}
            if isinstance(payload, dict):
                failure = str(payload.get("message") or "Hermes agent failed").strip()
            return
        if event_type != "clarify.request":
            return
        if event.get("session_id") not in {None, "", live_session_id}:
            raise GatewayError("clarify request belongs to another Hermes session")
        if active_interaction is not None:
            raise GatewayError("Hermes opened more than one clarification")
        payload = event.get("payload")
        active_interaction = interactions.create(
            args.job_id, stored_session_id, payload)

    try:
        if args.session_id:
            session = gateway.request(
                "session.resume",
                {
                    "session_id": args.session_id,
                    "profile": args.profile,
                    "omit_messages": True,
                    "cols": 100,
                },
                handle_event,
            )
        else:
            session = gateway.request(
                "session.create",
                {
                    "profile": args.profile,
                    "cwd": str(args.cwd),
                    "source": args.source,
                    "close_on_disconnect": True,
                    "cols": 100,
                },
                handle_event,
            )
        live_session_id = str(session.get("session_id") or "")
        stored_session_id = str(
            session.get("stored_session_id")
            or session.get("session_key")
            or session.get("resumed")
            or ""
        )
        if not live_session_id or not stored_session_id:
            raise GatewayError("Hermes gateway did not return exact session identity")
        gateway.request(
            "prompt.submit",
            {"session_id": live_session_id, "text": args.prompt},
            handle_event,
        )
        while not final_text and not failure:
            event = gateway.next_event(timeout=0.2)
            if event is not None:
                handle_event(event)
            if active_interaction is None:
                continue
            answered = interactions.answered(active_interaction.id, args.job_id)
            if answered is None:
                continue
            assert answered.answers is not None
            for question in answered.payload.questions:
                params: dict[str, Any] = {
                    "request_id": answered.hermes_request_id,
                    "answer": gateway_answer(answered.answers[question.id]),
                }
                if answered.payload.batch:
                    params["question_id"] = question.id
                response = gateway.request("clarify.respond", params, handle_event)
                if response.get("status") != "ok":
                    interactions.expire(
                        answered.id,
                        args.job_id,
                        answered.hermes_request_id,
                    )
                    active_interaction = None
                    raise GatewayError(
                        "Hermes was no longer waiting for this clarification"
                    )
            interactions.resolve(answered.id, args.job_id)
            active_interaction = None
        if failure and not final_text:
            raise GatewayError(failure)
        if not final_text:
            raise GatewayError("Hermes agent returned an empty reply")
        gateway.request(
            "session.close", {"session_id": live_session_id}, lambda _event: None
        )
        return final_text, stored_session_id
    finally:
        gateway.close()


def main() -> int:
    args = parser().parse_args()
    try:
        reply, session_id = run(args)
    except Exception as exc:
        print(f"Hermes Studio gateway worker failed: {exc}", file=sys.stderr)
        return 1
    print(reply)
    print(f"session_id: {session_id}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
