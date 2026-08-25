import json
import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import design_studio as ds
from studio_core import safe_files
from studio_core.projects import ClipStore, ClipStoreError
from studio_core.job_store import JobStore


class GenerationArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = ds.studio_root(self.temp.name)
        with redirect_stdout(StringIO()):
            self.project = ds.create_project(self.root, "same-name", "test")

    def tearDown(self):
        self.temp.cleanup()

    def test_archives_mcp_outputs_with_protected_metadata(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        source = comfy_output / "result.png"
        source.write_bytes(b"image")
        settings = {"schema_version": 2, "seed": 42, "steps": 20}
        clip = self.project / "clips" / "clip-001"
        (clip / "current_generation.json").write_text(
            json.dumps(settings) + "\n", encoding="utf-8")
        ds.write_prompt(self.root, self.project.name, "clip-001", "the prompt")
        with patch.object(ds, "COMFY_OUTPUT", comfy_output):
            generation = ds.archive_outputs(
                self.root,
                self.project.name,
                "clip-001",
                ["result.png"],
                {"prompt_id": "mcp-123", "transport": "forged"},
            )
        meta = json.loads((generation / "meta.json").read_text())
        self.assertEqual(meta["prompt_id"], "mcp-123")
        self.assertEqual(meta["transport"], "comfyui-mcp")
        self.assertEqual(meta["files"], ["result.png"])
        self.assertEqual((generation / "result.png").read_bytes(), b"image")
        self.assertEqual((generation / "prompt.txt").read_text(), "the prompt\n")
        self.assertEqual(
            json.loads((generation / "settings.json").read_text()), settings)

    def test_web_generation_archive_uses_authoritative_history_metadata(self):
        comfy_output = Path(self.temp.name) / "web-comfy-output"
        (comfy_output / "video").mkdir(parents=True)
        output = comfy_output / "video" / "result.mp4"
        output.write_bytes(b"video")
        clip = self.project / "clips" / "clip-001"
        prompt = "A complete 10-second prompt"
        ds.write_prompt(self.root, self.project.name, "clip-001", prompt)
        archived_prompt = prompt + "\n"
        prompt_sha256 = hashlib.sha256(archived_prompt.encode()).hexdigest()
        settings_manifest = {
            "schema_version": 2,
            "prompt_sha256": prompt_sha256,
            "updated_at": "2026-08-24T12:00:00+00:00",
            "mode": "r2v",
            "aspect": "16:9",
            "mp": 0.9,
            "width": 1280,
            "height": 704,
            "seed": None,
            "steps": 8,
            "accel": True,
        }
        (clip / "current_generation.json").write_text(
            json.dumps(settings_manifest) + "\n", encoding="utf-8")
        runtime_root = Path(self.temp.name) / "runtime"
        store = JobStore(runtime_root / "studio.db")
        store.initialize()
        generation_inputs = [{
            "type": "project_reference",
            "slot": 1,
            "filename": "first.png",
            "sha256": "1" * 64,
        }, {
            "type": "previous_selected_take_last_frame",
            "slot": 2,
            "source_clip_id": "clip-000",
            "source_generation_id": "001",
            "source_filename": "take.mp4",
            "source_video_sha256": "2" * 64,
            "extraction_offset_seconds": 0.25,
            "derived_filename": "previous-selected-take-second.png",
            "derived_frame_sha256": "3" * 64,
        }]
        payload = {
            "schema_version": 2,
            "action": "generate-current-prompt",
            "prompt": archived_prompt,
            "prompt_sha256": prompt_sha256,
            "settings_updated_at": settings_manifest["updated_at"],
            "settings_manifest": settings_manifest,
            "execution": {
                "resolution": {
                    "mode": "explicit", "width": 1280, "height": 704,
                    "megapixels": 0.901,
                },
                "timing": {
                    "requested_seconds": 10.0, "frames": 243,
                    "actual_seconds": 10.125, "fps": 24,
                },
                "inputs": generation_inputs,
            },
            "expected_generation_id": "001",
        }
        job = store.create_generation_job(
            self.project.name,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "studio",
            clip_id="clip-001",
        )
        self.assertIsNotNone(store.claim_next("worker"))
        ds.write_prompt(
            self.root, self.project.name, "clip-001", "mutated 10-second prompt")
        (clip / "current_generation.json").write_text(
            '{"mutated":true}\n', encoding="utf-8")
        prompt_id = "prompt-123"
        graph = {
            "unet": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": "h3.safetensors"},
            },
            "ref-1": {
                "class_type": "LoadImage",
                "inputs": {"image": "first.png"},
            },
            "ref-2": {
                "class_type": "LoadImage",
                "inputs": {"image": "previous-selected-take-second.png"},
            },
            "cond": {
                "class_type": "MiniMaxH3ReferenceToVideo",
                "inputs": {
                    "prompt": prompt,
                    "width": 1280,
                    "height": 704,
                    "length": 243,
                    "ref_images.ref_image_0": ["ref-1", 0],
                    "ref_images.ref_image_1": ["ref-2", 0],
                },
            },
            "noise": {
                "class_type": "RandomNoise",
                "inputs": {"noise_seed": 1338023213352416},
            },
            "scheduler": {
                "class_type": "BasicScheduler",
                "inputs": {"model": ["chunk", 0], "steps": 8},
            },
            "fused": {
                "class_type": "MiniMaxH3FusedModulation",
                "inputs": {"model": ["unet", 0], "enabled": True},
            },
            "chunk": {
                "class_type": "MiniMaxH3ChunkFeedForward",
                "inputs": {"model": ["fused", 0], "enabled": True},
            },
            "guider": {
                "class_type": "BasicGuider",
                "inputs": {
                    "model": ["chunk", 0],
                    "conditioning": ["cond", 0],
                },
            },
            "sample": {
                "class_type": "SamplerCustomAdvanced",
                "inputs": {
                    "noise": ["noise", 0],
                    "guider": ["guider", 0],
                    "sigmas": ["scheduler", 0],
                    "latent_image": ["cond", 1],
                },
            },
            "video-vae": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "video.safetensors"},
            },
            "audio-vae": {
                "class_type": "VAELoader",
                "inputs": {"vae_name": "audio.safetensors"},
            },
            "vae-decode": {
                "class_type": "VAEDecode",
                "inputs": {
                    "samples": ["sample", 0],
                    "vae": ["video-vae", 0],
                },
            },
            "audio-decode": {
                "class_type": "VAEDecodeAudio",
                "inputs": {
                    "samples": ["sample", 0],
                    "vae": ["audio-vae", 0],
                },
            },
            "video": {
                "class_type": "CreateVideo",
                "inputs": {
                    "images": ["vae-decode", 0],
                    "audio": ["audio-decode", 0],
                    "fps": 24.0,
                },
            },
            "save": {
                "class_type": "SaveVideo",
                "inputs": {"video": ["video", 0]},
            },
        }
        history = {
            prompt_id: {
                "prompt": [1, prompt_id, graph, {}, ["save"]],
                "status": {"completed": True, "status_str": "success"},
                "outputs": {
                    "save": {"images": [{
                        "filename": output.name,
                        "subfolder": "video",
                        "type": "output",
                    }]},
                },
            },
        }
        environment = {
            "HERMES_STUDIO_JOB_KIND": "generate",
            "HERMES_STUDIO_JOB_ID": job.id,
            "HERMES_STUDIO_PROJECT": self.project.name,
            "HERMES_STUDIO_CLIP": "clip-001",
            "HERMES_STUDIO_RUNTIME_ROOT": str(runtime_root),
            "COMFYUI_URL": "http://127.0.0.1:8188",
        }
        with (
            patch.dict(os.environ, environment),
            patch(
                "scripts.design_studio.urllib.request.urlopen",
                return_value=closing(StringIO(json.dumps(history))),
            ) as fetch,
        ):
            generation = ds.archive_outputs(
                self.root,
                self.project.name,
                "clip-001",
                ["video/result.mp4"],
                {
                    "prompt_id": prompt_id,
                    "kind": "video",
                    "seed": 1,
                    "width": 32,
                },
                source_root=comfy_output,
            )

        meta = json.loads((generation / "meta.json").read_text())
        self.assertEqual(
            fetch.call_args.args[0].full_url,
            f"http://127.0.0.1:8188/history/{prompt_id}",
        )
        self.assertEqual(meta["studio_job_id"], job.id)
        self.assertEqual(meta["output_node_id"], "save")
        self.assertEqual(meta["generation_contract_version"], 2)
        self.assertEqual(meta["generation_inputs"], generation_inputs)
        self.assertEqual(meta["settings_updated_at"], settings_manifest["updated_at"])
        self.assertEqual(meta["recipe"], "h3-ref2va")
        self.assertEqual(meta["mode"], "r2v")
        self.assertEqual((meta["width"], meta["height"]), (1280, 704))
        self.assertEqual(meta["mp"], 0.901)
        self.assertEqual(meta["length"], 243)
        self.assertEqual(meta["duration_sec"], 10.125)
        self.assertEqual(meta["fps"], 24)
        self.assertEqual(meta["seed"], 1338023213352416)
        self.assertEqual(meta["steps"], 8)
        self.assertTrue(meta["accel"])
        self.assertEqual(meta["accel_nodes"], [
            "MiniMaxH3FusedModulation",
            "MiniMaxH3ChunkFeedForward",
        ])
        self.assertEqual(meta["references"], [
            "first.png", "previous-selected-take-second.png"])
        self.assertEqual(
            meta["prompt_sha256"],
            hashlib.sha256(archived_prompt.encode()).hexdigest(),
        )
        self.assertEqual(
            meta["executed_prompt_sha256"],
            hashlib.sha256(prompt.encode()).hexdigest(),
        )
        self.assertFalse(meta["upscale"])
        self.assertEqual((generation / "prompt.txt").read_text(), archived_prompt)
        self.assertEqual(
            json.loads((generation / "settings.json").read_text()),
            settings_manifest,
        )

    def test_h3_history_metadata_follows_the_archived_output_branch(self):
        prompt_id = "branch-prompt"
        graph = {
            "decoy-cond": {
                "class_type": "MiniMaxH3ReferenceToVideo",
                "inputs": {
                    "prompt": "decoy prompt",
                    "width": 1280,
                    "height": 704,
                    "length": 243,
                },
            },
            "decoy-noise": {
                "class_type": "RandomNoise",
                "inputs": {"noise_seed": 1},
            },
            "decoy-scheduler": {
                "class_type": "BasicScheduler",
                "inputs": {"steps": 8},
            },
            "decoy-video": {
                "class_type": "CreateVideo",
                "inputs": {"fps": 24},
            },
            "cond": {
                "class_type": "MiniMaxH3ImageToVideo",
                "inputs": {
                    "prompt": "actual branch prompt",
                    "width": 640,
                    "height": 384,
                    "length": 125,
                },
            },
            "model": {"class_type": "UNETLoader", "inputs": {}},
            "noise": {
                "class_type": "RandomNoise",
                "inputs": {"noise_seed": 22},
            },
            "scheduler": {
                "class_type": "BasicScheduler",
                "inputs": {"model": ["model", 0], "steps": 9},
            },
            "guider": {
                "class_type": "BasicGuider",
                "inputs": {
                    "model": ["model", 0],
                    "conditioning": ["cond", 0],
                },
            },
            "sample": {
                "class_type": "SamplerCustomAdvanced",
                "inputs": {
                    "noise": ["noise", 0],
                    "guider": ["guider", 0],
                    "sigmas": ["scheduler", 0],
                    "latent_image": ["cond", 1],
                },
            },
            "vae": {"class_type": "VAELoader", "inputs": {}},
            "vae-decode": {
                "class_type": "VAEDecode",
                "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]},
            },
            "audio-decode": {
                "class_type": "VAEDecodeAudio",
                "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]},
            },
            "video": {
                "class_type": "CreateVideo",
                "inputs": {
                    "images": ["vae-decode", 0],
                    "audio": ["audio-decode", 0],
                    "fps": 25,
                },
            },
            "save": {
                "class_type": "SaveVideo",
                "inputs": {"video": ["video", 0]},
            },
        }
        history = {
            prompt_id: {
                "prompt": [1, prompt_id, graph, {}, ["save"]],
                "status": {"completed": True, "status_str": "success"},
                "outputs": {"save": {"images": [{
                    "filename": "result.mp4",
                    "subfolder": "",
                    "type": "output",
                }]}},
            },
        }
        with patch(
                "scripts.design_studio.urllib.request.urlopen",
                return_value=closing(StringIO(json.dumps(history)))):
            metadata = ds._h3_history_metadata(
                prompt_id, ["result.mp4"])
        self.assertEqual(metadata["mode"], "t2va")
        self.assertEqual(metadata["output_node_id"], "save")
        self.assertEqual(metadata["executed_prompt_sha256"], hashlib.sha256(
            b"actual branch prompt").hexdigest())
        self.assertEqual((metadata["width"], metadata["height"]), (640, 384))
        self.assertEqual(metadata["length"], 125)
        self.assertEqual(metadata["seed"], 22)
        self.assertEqual(metadata["steps"], 9)
        self.assertEqual(metadata["fps"], 25)

        graph["other-save"] = {
            "class_type": "SaveVideo",
            "inputs": {"video": ["video", 0]},
        }
        history[prompt_id]["prompt"][4].append("other-save")
        history[prompt_id]["outputs"]["other-save"] = {
            "images": [{
                "filename": "result.mp4",
                "subfolder": "",
                "type": "output",
            }],
        }
        with (
            patch(
                "scripts.design_studio.urllib.request.urlopen",
                return_value=closing(StringIO(json.dumps(history))),
            ),
            self.assertRaisesRegex(ValueError, "one exact producer"),
        ):
            ds._h3_history_metadata(prompt_id, ["result.mp4"])

    def test_web_generation_archive_rejects_missing_history_identity(self):
        comfy_output = Path(self.temp.name) / "web-missing-history"
        comfy_output.mkdir()
        (comfy_output / "result.mp4").write_bytes(b"video")
        environment = {
            "HERMES_STUDIO_JOB_KIND": "generate",
            "HERMES_STUDIO_PROJECT": self.project.name,
            "HERMES_STUDIO_CLIP": "clip-001",
        }
        with (
            patch.dict(os.environ, environment),
            self.assertRaisesRegex(ValueError, "requires a prompt_id"),
        ):
            ds.archive_outputs(
                self.root,
                self.project.name,
                "clip-001",
                ["result.mp4"],
                {"kind": "video"},
                source_root=comfy_output,
            )
        generations = self.project / "clips" / "clip-001" / "generations"
        self.assertEqual(list(generations.iterdir()), [])

    def _assert_archive_swap_at_open_is_rejected(self, filename, source, outside):
        real_open = os.open
        swapped = False

        def swap_at_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == filename:
                swapped = True
                source.unlink()
                source.symlink_to(outside)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("studio_core.safe_files.os.open", side_effect=swap_at_open),
            self.assertRaises(ValueError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source.parent if filename == "result.png" else outside.parent,
            )
        self.assertTrue(swapped)
        self.assertEqual(outside.read_bytes(), b"SECRET")
        self.assertEqual(
            list((self.project / "clips" / "clip-001" / "generations").iterdir()),
            [],
        )

    def test_archive_media_swap_at_descriptor_open_is_rejected(self):
        source_root = Path(self.temp.name) / "media-swap"
        source_root.mkdir()
        source = source_root / "result.png"
        source.write_bytes(b"safe image")
        outside = Path(self.temp.name) / "outside-media.bin"
        outside.write_bytes(b"SECRET")
        self._assert_archive_swap_at_open_is_rejected(
            "result.png", source, outside)

    def test_archive_prompt_swap_at_descriptor_open_is_rejected(self):
        source_root = Path(self.temp.name) / "prompt-swap"
        source_root.mkdir()
        media = source_root / "result.png"
        media.write_bytes(b"safe image")
        clip = self.project / "clips" / "clip-001"
        prompt = clip / "current_prompt.txt"
        prompt.write_text("safe prompt\n", encoding="utf-8")
        outside = Path(self.temp.name) / "outside-prompt.txt"
        outside.write_bytes(b"SECRET")
        real_open = os.open
        swapped = False

        def swap_at_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == "current_prompt.txt":
                swapped = True
                prompt.unlink()
                prompt.symlink_to(outside)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("studio_core.safe_files.os.open", side_effect=swap_at_open),
            self.assertRaises(ValueError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)
        self.assertTrue(swapped)
        self.assertEqual(outside.read_bytes(), b"SECRET")

    def test_archive_settings_swap_at_descriptor_open_is_rejected(self):
        source_root = Path(self.temp.name) / "settings-swap"
        source_root.mkdir()
        media = source_root / "result.png"
        media.write_bytes(b"safe image")
        clip = self.project / "clips" / "clip-001"
        settings = clip / "current_generation.json"
        settings.write_text('{"seed": 1}\n', encoding="utf-8")
        outside = Path(self.temp.name) / "outside-settings.json"
        outside.write_bytes(b"SECRET")
        real_open = os.open
        swapped = False

        def swap_at_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == "current_generation.json":
                swapped = True
                settings.unlink()
                settings.symlink_to(outside)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("studio_core.safe_files.os.open", side_effect=swap_at_open),
            self.assertRaises(ValueError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)
        self.assertTrue(swapped)
        self.assertEqual(outside.read_bytes(), b"SECRET")

    def test_archive_settings_parent_swap_does_not_copy_outside(self):
        source_root = Path(self.temp.name) / "settings-parent-swap"
        source_root.mkdir()
        (source_root / "result.png").write_bytes(b"safe image")
        clip = self.project / "clips" / "clip-001"
        (clip / "current_generation.json").write_text(
            '{"seed": 1}\n', encoding="utf-8")
        displaced = Path(self.temp.name) / "displaced-settings-clip"
        outside = Path(self.temp.name) / "outside-settings-clip"
        outside.mkdir()
        (outside / "current_generation.json").write_text(
            '{"outside": "secret"}\n', encoding="utf-8")
        real_open = os.open
        swapped = False
        copied_settings = []
        real_copy = ds.copy_opened_file

        def swap_parent_at_final_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == "current_generation.json":
                swapped = True
                clip.rename(displaced)
                clip.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def inspect_copy(source, target):
            if source.name == "current_generation.json":
                copied_settings.append(os.pread(source.descriptor, 4096, 0))
            return real_copy(source, target)

        with (
            patch("studio_core.safe_files.os.open",
                  side_effect=swap_parent_at_final_open),
            patch.object(ds, "copy_opened_file", side_effect=inspect_copy),
            self.assertRaises(ValueError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)

        self.assertTrue(swapped)
        self.assertNotIn(b'{"outside": "secret"}\n', copied_settings)
        self.assertEqual(
            (outside / "current_generation.json").read_text(),
            '{"outside": "secret"}\n',
        )

    def test_archive_publication_rejects_swapped_staging_identity(self):
        source_root = Path(self.temp.name) / "archive-publication-swap"
        source_root.mkdir()
        source = source_root / "result.png"
        source.write_bytes(b"safe image")
        generations = self.project / "clips" / "clip-001" / "generations"
        target = generations / "001"
        displaced = generations / ".displaced-archive-source"
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        swapped = False

        def swap_source(parent_fd, source_name, destination_fd,
                        destination_name, flags):
            nonlocal swapped
            name = os.fsdecode(source_name)
            if not swapped and name.startswith(".publishing-"):
                swapped = True
                os.rename(name, displaced.name,
                          src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.mkdir(name, dir_fd=parent_fd)
                replacement_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
                try:
                    marker_fd = os.open(
                        "replacement.txt",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=replacement_fd,
                    )
                    os.close(marker_fd)
                finally:
                    os.close(replacement_fd)
            return real_renameat2(
                parent_fd, source_name, destination_fd, destination_name, flags)

        with (
            patch.object(safe_files, "_renameat2", side_effect=swap_source),
            self.assertRaisesRegex(safe_files.SafeFilesystemError,
                                   "publication identity"),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)

        self.assertTrue(swapped)
        self.assertEqual(source.read_bytes(), b"safe image")
        self.assertTrue((target / "replacement.txt").is_file())
        self.assertEqual((displaced / "result.png").read_bytes(), b"safe image")

    def test_archive_without_current_settings_records_exact_null_snapshot(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        (comfy_output / "result.png").write_bytes(b"image")
        generation = ds.archive_outputs(
            self.root, self.project.name, "clip-001", ["result.png"],
            source_root=comfy_output)
        self.assertEqual((generation / "prompt.txt").read_text(), "")
        self.assertEqual((generation / "settings.json").read_text(), "null\n")

    def test_archive_rejects_output_outside_comfy_directory(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(b"image")
        with patch.object(ds, "COMFY_OUTPUT", comfy_output):
            with self.assertRaises(ValueError):
                ds.archive_outputs(
                    self.root, self.project.name, "clip-001", [str(outside)])

    def test_archive_rejects_symlinked_output(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(b"image")
        (comfy_output / "linked.png").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["linked.png"],
                source_root=comfy_output)

    def test_archive_rejects_symlinked_output_directory_component(self):
        comfy_output = Path(self.temp.name) / "nested-comfy-output"
        comfy_output.mkdir()
        outside = Path(self.temp.name) / "outside-output-directory"
        outside.mkdir()
        (outside / "secret.png").write_bytes(b"SECRET")
        (comfy_output / "linked").symlink_to(
            outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symlink"):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001",
                ["linked/secret.png"], source_root=comfy_output)
        self.assertEqual((outside / "secret.png").read_bytes(), b"SECRET")

    def test_archive_rejects_swapped_media_root_parent(self):
        media_parent = Path(self.temp.name) / "media-parent"
        source_root = media_parent / "source"
        source_root.mkdir(parents=True)
        (source_root / "result.png").write_bytes(b"safe image")
        displaced = Path(self.temp.name) / "displaced-media-parent"
        outside_parent = Path(self.temp.name) / "outside-media-parent"
        outside_root = outside_parent / "source"
        outside_root.mkdir(parents=True)
        (outside_root / "result.png").write_bytes(b"OUTSIDE")
        real_open = os.open
        swapped = False

        def swap_parent_before_root_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            component_walk = dir_fd is not None and os.fsdecode(path) == media_parent.name
            if not swapped and (Path(path) == source_root or component_walk):
                swapped = True
                media_parent.rename(displaced)
                media_parent.symlink_to(outside_parent, target_is_directory=True)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("studio_core.safe_files.os.open",
                  side_effect=swap_parent_before_root_open),
            self.assertRaises(ValueError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)

        self.assertTrue(swapped)
        self.assertEqual((outside_root / "result.png").read_bytes(), b"OUTSIDE")
        self.assertEqual(
            list((self.project / "clips" / "clip-001" / "generations").iterdir()),
            [],
        )

    def test_archive_rejects_symlinked_generation_directory(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        (comfy_output / "result.png").write_bytes(b"image")
        outside = Path(self.temp.name) / "outside-generations"
        outside.mkdir()
        generations = self.project / "clips" / "clip-001" / "generations"
        generations.rmdir()
        generations.symlink_to(
            outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "generations directory"):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=comfy_output)
        self.assertEqual(list(outside.iterdir()), [])

    def test_archive_is_hidden_until_atomic_publication(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        source = comfy_output / "result.png"
        source.write_bytes(b"image")
        visible_during_copy = []
        real_copy = ds.copy_opened_file

        def inspect_copy(source_path, target_path):
            visible_during_copy.append([
                item.name for item in (
                    self.project / "clips" / "clip-001" / "generations").iterdir()
                if item.name.isdigit()
            ])
            return real_copy(source_path, target_path)

        with patch.object(ds, "copy_opened_file", side_effect=inspect_copy):
            generation = ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=comfy_output)
        self.assertEqual(visible_during_copy, [[]])
        self.assertEqual(generation.name, "001")
        self.assertTrue((generation / "meta.json").is_file())

    def test_archive_collision_at_publication_preserves_destination_and_source(self):
        source_root = Path(self.temp.name) / "publication-collision"
        source_root.mkdir()
        source = source_root / "result.png"
        source.write_bytes(b"safe image")
        generations = self.project / "clips" / "clip-001" / "generations"
        target = generations / "001"
        real_publish = ds.atomic_publish_directory

        def collide_at_publication(staging, destination):
            destination.mkdir()
            return real_publish(staging, destination)

        with (
            patch.object(
                ds, "atomic_publish_directory",
                side_effect=collide_at_publication,
            ),
            self.assertRaises(FileExistsError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)

        self.assertEqual(source.read_bytes(), b"safe image")
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(
            [path for path in generations.iterdir() if path.name.startswith(".publishing-")],
            [],
        )

    def test_archives_grok_cache_with_xai_transport(self):
        grok_cache = Path(self.temp.name) / "grok-cache"
        grok_cache.mkdir()
        image = grok_cache / "imagine.png"
        image.write_bytes(b"image")
        generation = ds.archive_outputs(
            self.root, self.project.name, "clip-001", [str(image)],
            {"prompt": "test"}, source_root=grok_cache,
            transport="xai-imagine")
        meta = json.loads((generation / "meta.json").read_text())
        self.assertEqual(meta["transport"], "xai-imagine")
        self.assertTrue((generation / "imagine.png").is_file())

    def test_concurrent_take_allocation_is_unique_and_complete(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        (comfy_output / "result.png").write_bytes(b"image")
        with ThreadPoolExecutor(max_workers=8) as pool:
            generations = list(pool.map(
                lambda _: ds.archive_outputs(
                    self.root, self.project.name, "clip-001", ["result.png"],
                    source_root=comfy_output),
                range(8),
            ))
        self.assertEqual(
            {generation.name for generation in generations},
            {f"{index:03d}" for index in range(1, 9)},
        )
        for generation in generations:
            self.assertEqual(
                {path.name for path in generation.iterdir()},
                {"result.png", "prompt.txt", "settings.json", "meta.json"},
            )

    def test_generation_runners_archive_under_the_requested_clip(self):
        ClipStore().create_clip(self.project, "Second")
        comfy_output = Path(self.temp.name) / "runner-output"
        comfy_output.mkdir()
        video = comfy_output / "video.mp4"
        image = comfy_output / "image.png"
        video.write_bytes(b"video")
        image.write_bytes(b"image")

        video_result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"video": str(video)}),
            stderr="",
        )
        image_result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "seed": 7, "prompt_id": "image-1", "files": ["image.png"]}),
            stderr="",
        )
        with (
            patch.object(ds, "COMFY_OUTPUT", comfy_output),
            patch.object(ds, "free_comfy_vram", return_value={"ok": True}),
            patch.object(ds.subprocess, "run", return_value=video_result),
        ):
            generated_video = ds.run_generation(
                self.root, self.project.name, "clip-002")
        with (
            patch.object(ds, "COMFY_OUTPUT", comfy_output),
            patch.object(ds.subprocess, "run", return_value=image_result),
        ):
            generated_image = ds.run_image_generation(
                self.root, self.project.name, "clip-001", "t2i", "still prompt")

        self.assertEqual(
            Path(generated_video["generation"]).parent,
            self.project / "clips" / "clip-002" / "generations",
        )
        self.assertEqual(
            Path(generated_image["generation"]).parent,
            self.project / "clips" / "clip-001" / "generations",
        )
        self.assertEqual(
            (Path(generated_image["generation"]) / "prompt.txt").read_text(),
            "still prompt\n",
        )

    def test_generation_runners_reject_unknown_clip_before_execution(self):
        with patch.object(ds.subprocess, "run") as run:
            with self.assertRaisesRegex(ClipStoreError, "clip not found"):
                ds.run_generation(
                    self.root, self.project.name, "clip-999", dry_run=True)
            with self.assertRaisesRegex(ClipStoreError, "clip not found"):
                ds.run_image_generation(
                    self.root, self.project.name, "clip-999", "t2i", "prompt")
        run.assert_not_called()

    def test_archive_rejects_reserved_media_name_and_unsafe_settings(self):
        comfy_output = Path(self.temp.name) / "unsafe-output"
        comfy_output.mkdir()
        (comfy_output / "meta.json").write_text("media")
        with self.assertRaisesRegex(ValueError, "reserved"):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["meta.json"],
                source_root=comfy_output)

        (comfy_output / "result.png").write_bytes(b"image")
        outside = Path(self.temp.name) / "settings.json"
        outside.write_text("{}")
        settings = (
            self.project / "clips" / "clip-001" / "current_generation.json")
        settings.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "regular clip file"):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=comfy_output)
        self.assertEqual(
            list((self.project / "clips" / "clip-001" / "generations").iterdir()),
            [],
        )
