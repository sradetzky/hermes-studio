from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path


CONTRACT_SCHEMA_VERSION = 1
_PAYLOAD_KEYS = {
    "schema_version",
    "action",
    "prompt",
    "prompt_sha256",
    "settings_updated_at",
    "settings_manifest",
    "execution",
    "expected_generation_id",
}
_SETTINGS_KEYS = {
    "schema_version",
    "prompt_sha256",
    "updated_at",
    "mode",
    "aspect",
    "mp",
    "width",
    "height",
    "seed",
    "steps",
    "accel",
}


class GenerationContractError(ValueError):
    pass


def parse_generation_job_payload(value: str) -> dict:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GenerationContractError(
            "generation request payload is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_KEYS:
        raise GenerationContractError("generation request payload is invalid")
    prompt = payload.get("prompt")
    prompt_sha256 = payload.get("prompt_sha256")
    updated_at = payload.get("settings_updated_at")
    expected_generation_id = payload.get("expected_generation_id")
    if (payload.get("schema_version") != CONTRACT_SCHEMA_VERSION
            or payload.get("action") != "generate-current-prompt"
            or not isinstance(prompt, str) or not prompt
            or not isinstance(prompt_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", prompt_sha256) is None
            or hashlib.sha256(prompt.encode("utf-8")).hexdigest() != prompt_sha256
            or not isinstance(updated_at, str) or not updated_at
            or not isinstance(expected_generation_id, str)
            or re.fullmatch(r"[0-9]{3,}", expected_generation_id) is None):
        raise GenerationContractError("generation request payload is invalid")

    manifest = payload.get("settings_manifest")
    if (not isinstance(manifest, dict) or set(manifest) != _SETTINGS_KEYS
            or manifest.get("schema_version") != 2
            or manifest.get("prompt_sha256") != prompt_sha256
            or manifest.get("updated_at") != updated_at):
        raise GenerationContractError("generation settings snapshot is invalid")

    execution = payload.get("execution")
    if (not isinstance(execution, dict)
            or set(execution) != {"resolution", "timing", "references"}
            or not isinstance(execution.get("resolution"), dict)
            or not isinstance(execution.get("timing"), dict)
            or not isinstance(execution.get("references"), list)
            or not all(isinstance(item, str)
                       for item in execution["references"])):
        raise GenerationContractError("generation execution snapshot is invalid")
    return payload


def load_running_generation_contract(
        runtime_root: Path, job_id: str, project: str, clip_id: str) -> dict:
    database = runtime_root / "studio.db"
    try:
        with closing(sqlite3.connect(
                f"{database.resolve().as_uri()}?mode=ro", uri=True,
                timeout=0.25)) as connection:
            row = connection.execute(
                "SELECT project, clip_id, kind, status, message FROM jobs "
                "WHERE id = ?",
                (job_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise GenerationContractError(
            "could not read the immutable generation contract") from exc
    if (row is None or row[0] != project or row[1] != clip_id
            or row[2] != "generate" or row[3] != "running"):
        raise GenerationContractError(
            "web generation contract does not match its running job")
    return parse_generation_job_payload(row[4])
