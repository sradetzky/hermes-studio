import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from studio_core.generation_contracts import parse_generation_contract
from studio_core.projects import ClipStore
from webapp.generation_input_store import (
    GenerationInputError,
    GenerationInputStore,
)


class GenerationInputStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        for name in ("references", "final", "research"):
            (self.project / name).mkdir()
        self.clips = ClipStore()
        self.clips.initialize(self.project, "Project")
        self.clips.create_clip(self.project, "Second")
        self.source = self._selected_video("clip-001", "001")
        (self.project / "references" / "identity.png").write_bytes(b"identity")
        self.store = GenerationInputStore()

    def tearDown(self):
        self.temp.cleanup()

    def _selected_video(self, clip_id: str, generation_id: str) -> Path:
        generation = (
            self.project / "clips" / clip_id / "generations" / generation_id)
        generation.mkdir()
        video = generation / "take.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-nostdin",
            "-f", "lavfi", "-i", "color=c=red:s=64x64:r=4:d=0.5",
            "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=4:d=0.5",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ], check=True, capture_output=True)
        self.clips.select_take(
            self.project, clip_id, generation_id, video.name)
        return video

    def _materialized(self) -> dict:
        return self.store.materialize_previous_selected_take(
            self.project, "clip-002", project_reference_count=1)

    def test_describes_and_materializes_exact_previous_selected_take(self):
        eligibility = self.store.describe_previous_selected_take(
            self.project, "clip-002", project_reference_count=1)
        self.assertEqual(eligibility, {
            "eligible": True,
            "source_clip_id": "clip-001",
            "source_generation_id": "001",
            "source_filename": "take.mp4",
            "picture_number": 2,
        })

        materialized = self._materialized()
        self.assertEqual(materialized["type"], "previous_selected_take_last_frame")
        self.assertEqual(materialized["slot"], 2)
        self.assertEqual(materialized["source_clip_id"], "clip-001")
        self.assertEqual(materialized["source_generation_id"], "001")
        self.assertEqual(materialized["source_filename"], "take.mp4")
        self.assertEqual(materialized["extraction_offset_seconds"], 0.25)
        self.assertEqual(
            materialized["source_video_sha256"],
            hashlib.sha256(self.source.read_bytes()).hexdigest())
        derived = (
            self.project / "clips" / "clip-002" / "generation-inputs" /
            materialized["derived_filename"])
        self.assertTrue(derived.is_file())
        self.assertGreater(derived.stat().st_size, 0)
        self.assertEqual(
            materialized["derived_frame_sha256"],
            hashlib.sha256(derived.read_bytes()).hexdigest())
        pixel = subprocess.run([
            "ffmpeg", "-v", "error", "-nostdin", "-i", str(derived),
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ], check=True, capture_output=True).stdout[:3]
        self.assertGreater(pixel[2], pixel[0])

    def test_describes_previous_take_before_prompt_references_exist(self):
        described = self.store.describe_previous_selected_take(
            self.project, "clip-002", project_reference_count=0)

        self.assertEqual(described, {
            "eligible": True,
            "source_clip_id": "clip-001",
            "source_generation_id": "001",
            "source_filename": "take.mp4",
            "picture_number": 1,
        })

    def _inputs(self) -> tuple[list[dict], Path]:
        previous = self._materialized()
        project_input = self.store.snapshot_project_reference(
            self.project, "identity.png", slot=1)
        paths = self.store.validate(
            self.project, "clip-002", [project_input, previous])
        self.assertEqual(paths[0], self.project / "references" / "identity.png")
        self.assertEqual(paths[1].name, previous["derived_filename"])
        return [project_input, previous], paths[1]

    def test_validates_exact_ordered_input_identities(self):
        self._inputs()

    def test_rejects_mutated_source_and_derived_bytes(self):
        inputs, _derived = self._inputs()
        self.source.write_bytes(b"changed")
        with self.assertRaisesRegex(GenerationInputError, "source video changed"):
            self.store.validate(self.project, "clip-002", inputs)

        self.source = self._selected_video("clip-001", "002")
        inputs, derived = self._inputs()
        derived.write_bytes(b"changed")
        with self.assertRaisesRegex(GenerationInputError, "derived frame changed"):
            self.store.validate(self.project, "clip-002", inputs)

    def _assert_previous_selection_changed(self, inputs: list[dict]) -> None:
        with self.assertRaisesRegex(
                GenerationInputError,
                "previous selected take changed after enqueue"):
            self.store.validate(self.project, "clip-002", inputs)

    def test_rejects_reordered_clips(self):
        inputs, _derived = self._inputs()
        self.clips.reorder(self.project, ["clip-002", "clip-001"])
        self._assert_previous_selection_changed(inputs)

    def test_rejects_disabled_previous_clip(self):
        inputs, _derived = self._inputs()
        self.clips.update_clip(self.project, "clip-001", enabled=False)
        self._assert_previous_selection_changed(inputs)

    def test_rejects_changed_selection(self):
        inputs, _derived = self._inputs()
        self._selected_video("clip-001", "002")
        self._assert_previous_selection_changed(inputs)

    def test_rejects_deleted_take(self):
        inputs, _derived = self._inputs()
        self.clips.delete_take(self.project, "clip-001", "001")
        self._assert_previous_selection_changed(inputs)

    def test_contract_v2_preserves_typed_input_provenance(self):
        prompt = "A 5-second prompt"
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        project_input = self.store.snapshot_project_reference(
            self.project, "identity.png", slot=1)
        previous = self._materialized()
        payload = {
            "schema_version": 2,
            "action": "generate-current-prompt",
            "prompt": prompt,
            "prompt_sha256": prompt_hash,
            "settings_updated_at": "2026-08-25T00:00:00+00:00",
            "settings_manifest": {
                "schema_version": 2,
                "prompt_sha256": prompt_hash,
                "updated_at": "2026-08-25T00:00:00+00:00",
                "mode": "r2v", "aspect": "16:9", "mp": 0.5,
                "width": 832, "height": 480, "seed": 42,
                "steps": 8, "accel": True,
            },
            "execution": {
                "resolution": {
                    "mode": "explicit", "width": 832, "height": 480,
                    "megapixels": 0.399,
                },
                "timing": {
                    "requested_seconds": 5.0, "fps": 24, "frames": 124,
                    "actual_seconds": 5.167,
                },
                "inputs": [project_input, previous],
            },
            "expected_generation_id": "001",
        }
        contract = parse_generation_contract(payload)
        self.assertEqual(contract.to_dict(), payload)
        self.assertEqual(contract.execution.references, (
            "identity.png", previous["derived_filename"]))
        self.assertEqual(
            json.loads(json.dumps(contract.to_dict()))["execution"]["inputs"][1]
            ["source_video_sha256"],
            previous["source_video_sha256"])


if __name__ == "__main__":
    unittest.main()
