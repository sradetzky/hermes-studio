from __future__ import annotations

import json
from pathlib import Path

from studio_core.generation_contracts import (
    CONTRACT_SCHEMA_VERSION,
    LEGACY_CONTRACT_SCHEMA_VERSION,
    GenerationContract,
    GenerationExecutionContract,
    GenerationInputContract,
    GenerationResolutionContract,
    GenerationSettingsContract,
    GenerationTimingContract,
    ProjectReferenceInputContract,
)
from studio_core.job_contracts import GenerationJobPayload
from studio_core.models import Job
from studio_core.projects import (
    ClipStore,
    next_generation_dir,
    project_path,
    read_project_text,
)
from webapp.config import Settings
from webapp.generation_input_store import GenerationInputStore
from webapp.generation_settings_store import GenerationSettingsStore
from webapp.media_review_store import MediaReviewStore


class GenerationJobService:
    """Build, revalidate, and verify immutable web generation jobs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.clips = ClipStore()
        self.generation_settings = GenerationSettingsStore(settings)
        self.inputs = GenerationInputStore()
        self.media = MediaReviewStore()

    @staticmethod
    def _settings_contract(current: dict) -> GenerationSettingsContract:
        manifest = current["manifest"]
        settings = current["settings"]
        return GenerationSettingsContract(
            schema_version=manifest["schema_version"],
            prompt_sha256=manifest["prompt_sha256"],
            updated_at=manifest["updated_at"],
            mode=settings["mode"],
            aspect=settings["aspect"],
            mp=settings["mp"],
            width=settings["width"],
            height=settings["height"],
            seed=(int(settings["seed"]) if settings["seed"] is not None else None),
            steps=settings["steps"],
            accel=settings["accel"],
        )

    @staticmethod
    def _resolution_contract(current: dict) -> GenerationResolutionContract:
        resolution = current["readiness"]["resolution"]
        return GenerationResolutionContract(
            mode=resolution["mode"],
            width=resolution["width"],
            height=resolution["height"],
            megapixels=resolution["megapixels"],
        )

    @staticmethod
    def _timing_contract(current: dict) -> GenerationTimingContract:
        timing = current["readiness"]["timing"]
        return GenerationTimingContract(
            requested_seconds=timing["requested_seconds"],
            frames=timing["frames"],
            actual_seconds=timing["actual_seconds"],
            fps=timing["fps"],
        )

    def _current_contract_state(
            self, project: Path, clip_id: str,
            prompt_sha256: str, settings_updated_at: str) -> tuple[Path, dict]:
        manifest = self.clips.describe(project)
        entry = next(
            (item for item in manifest["clips"] if item["id"] == clip_id),
            None,
        )
        if entry is None:
            raise ValueError(f"clip not found: {clip_id}")
        if not entry["enabled"]:
            raise ValueError("clip is disabled")
        clip = self.clips.resolve_clip(project, clip_id)
        current = self.generation_settings.validate_generation_request(
            project, clip, prompt_sha256, settings_updated_at)
        return clip, current

    def build_request(
            self, project: Path, project_id: str, clip_id: str,
            prompt_sha256: str, settings_updated_at: str,
            use_previous_take_last_frame: bool) -> str:
        clip, current = self._current_contract_state(
            project, clip_id, prompt_sha256, settings_updated_at)
        settings = self._settings_contract(current)
        project_references = current["readiness"]["references"]
        inputs: list[GenerationInputContract] = [
            self.inputs.snapshot_project_reference(
                project, filename, slot=index)
            for index, filename in enumerate(project_references, 1)
        ]
        if use_previous_take_last_frame:
            if settings.mode != "r2v":
                raise ValueError(
                    "previous selected take continuity is available only for R2V")
            eligibility = self.inputs.describe_previous_selected_take(
                project,
                clip_id,
                project_reference_count=len(project_references),
            )
            if not eligibility["eligible"]:
                raise ValueError(
                    "previous selected take is not available for this generation")
            inputs.append(self.inputs.materialize_previous_selected_take(
                project,
                clip_id,
                project_reference_count=len(project_references),
                mode=settings.mode,
            ))
        prompt = read_project_text(clip, "current_prompt.txt", required=True)
        contract = GenerationContract(
            schema_version=CONTRACT_SCHEMA_VERSION,
            action="generate-current-prompt",
            prompt=prompt,
            prompt_sha256=prompt_sha256,
            settings_updated_at=settings_updated_at,
            settings_manifest=settings,
            execution=GenerationExecutionContract(
                resolution=self._resolution_contract(current),
                timing=self._timing_contract(current),
                inputs=tuple(inputs),
            ),
            expected_generation_id=next_generation_dir(
                self.settings.studio_root, project_id, clip_id).name,
        )
        return json.dumps(
            contract.to_dict(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False)

    def validate(self, job: Job) -> GenerationContract:
        if not isinstance(job.payload, GenerationJobPayload):
            raise ValueError("generation runner received the wrong payload type")
        contract = job.payload.contract
        project = project_path(self.settings.studio_root, job.project)
        clip, current = self._current_contract_state(
            project,
            job.clip_id,
            contract.prompt_sha256,
            contract.settings_updated_at,
        )
        project_inputs = tuple(
            item for item in contract.execution.inputs
            if isinstance(item, ProjectReferenceInputContract)
        )
        if tuple(item.filename for item in project_inputs) != tuple(
                current["readiness"]["references"]):
            raise ValueError("generation inputs changed after enqueue")
        if contract.schema_version == LEGACY_CONTRACT_SCHEMA_VERSION:
            if any(item.sha256 is not None for item in project_inputs):
                raise ValueError("legacy generation inputs are invalid")
        else:
            self.inputs.validate(project, job.clip_id, contract.execution.inputs)
        if (
            read_project_text(clip, "current_prompt.txt", required=True)
            != contract.prompt
            or self._settings_contract(current) != contract.settings_manifest
            or self._resolution_contract(current) != contract.execution.resolution
            or self._timing_contract(current) != contract.execution.timing
        ):
            raise ValueError("generation contract changed after enqueue")
        expected_generation_id = next_generation_dir(
            self.settings.studio_root, job.project, job.clip_id).name
        if expected_generation_id != contract.expected_generation_id:
            raise ValueError("generation archive sequence changed after enqueue")
        return contract

    def verify_completion(self, job: Job) -> None:
        if not isinstance(job.payload, GenerationJobPayload):
            raise ValueError("generation runner received the wrong payload type")
        contract = job.payload.contract
        project = project_path(self.settings.studio_root, job.project)
        clip = self.clips.resolve_clip(project, job.clip_id)
        generation_id = contract.expected_generation_id
        try:
            details = self.media.describe_generation(
                project, clip, generation_id, include_prompt=True)
        except Exception as exc:
            raise ValueError(
                "generation archive postcondition was not satisfied") from exc
        meta = details["meta"]
        expected_inputs = (
            contract.to_dict()["execution"].get("inputs", []))
        if (
            meta.get("studio_job_id") != job.id
            or meta.get("generation_contract_version") != contract.schema_version
            or meta.get("prompt_sha256") != contract.prompt_sha256
            or meta.get("settings_updated_at") != contract.settings_updated_at
            or not isinstance(meta.get("prompt_id"), str)
            or not meta["prompt_id"]
            or not details["files"]
            or sorted(meta.get("files", [])) != sorted(details["files"])
            or meta.get("generation_inputs", []) != expected_inputs
            or details.get("prompt") != contract.prompt
        ):
            raise ValueError(
                "generation archive does not match its immutable job contract")
        try:
            archived_settings = json.loads(read_project_text(
                clip / "generations" / generation_id,
                "settings.json",
                required=True,
            ))
        except json.JSONDecodeError as exc:
            raise ValueError("generation archive settings are invalid") from exc
        if archived_settings != contract.settings_manifest.to_dict():
            raise ValueError(
                "generation archive settings do not match its immutable job contract")
