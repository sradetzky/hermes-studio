import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from fastapi.testclient import TestClient

from studio_core import safe_files
from tests.webapp_test_support import (
    generation_settings_payload as _generation_settings_payload,
)
from tests.webapp_route_support import RouteWebAppTestCase as WebAppTestCase


class GenerationMediaRouteTests(WebAppTestCase):
    def test_previous_take_continuity_rejects_non_r2v_before_materializing(self):
        with TestClient(self.app(), raise_server_exceptions=False) as client:
            project = self.create_project(client, "previous-take-mode")
            root = self.settings.studio_root / "projects" / project
            created = client.post(
                f"/api/project/{project}/clips",
                json={"title": "Continuation"},
            )
            self.assertEqual(created.status_code, 201)

            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            source = generation / "take.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi",
                "-i", "color=c=blue:s=64x64:r=4:d=0.5", "-an",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ], check=True, capture_output=True)
            selected = client.put(
                f"/api/project/{project}/clips/clip-001/selected-take",
                json={"generation": "001", "filename": "take.mp4"},
            )
            self.assertEqual(selected.status_code, 200)

            (root / "references" / "identity.png").write_bytes(b"identity")
            clip = root / "clips" / "clip-002"
            (clip / "current_prompt.txt").write_text(
                "A 5-second continuation with <Picture 1> (identity.png)\n")
            contract = client.put(
                f"/api/project/{project}/clips/clip-002/generation-settings",
                json=_generation_settings_payload(mode="i2va"),
            ).json()
            self.assertTrue(contract["readiness"]["ready"])
            self.assertTrue(
                contract["previous_selected_take_input"]["eligible"])

            response = client.post(
                f"/api/project/{project}/clips/clip-002/generate",
                json={
                    "prompt_sha256": contract["manifest"]["prompt_sha256"],
                    "settings_updated_at": contract["manifest"]["updated_at"],
                    "use_previous_take_last_frame": True,
                },
            )

            self.assertEqual(response.status_code, 409)
            self.assertIn("R2V", response.json()["detail"])
            self.assertFalse((clip / "generation-inputs").exists())

    def test_generation_settings_reject_unsafe_values(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "generation-validation")
            root = self.settings.studio_root / "projects" / project
            clip = root / "clips" / "clip-001"
            (clip / "current_prompt.txt").write_text("prompt\n")
            cases = [
                _generation_settings_payload(mp=1.2),
                _generation_settings_payload(width=1344, height=None),
                _generation_settings_payload(width=1536, height=768),
                _generation_settings_payload(steps=0),
                _generation_settings_payload(seed="9007199254740992"),
            ]
            for payload in cases:
                with self.subTest(payload=payload):
                    response = client.put(
                        f"/api/project/{project}/clips/clip-001/generation-settings",
                        json=payload,
                    )
                    self.assertEqual(response.status_code, 400)
            nan_payload = _generation_settings_payload(mp=float("nan"))
            response = client.put(
                f"/api/project/{project}/clips/clip-001/generation-settings",
                content=json.dumps(nan_payload),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertFalse((clip / "current_generation.json").exists())

    def test_clip_api_isolates_prompt_settings_takes_and_selection(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "clip-api")
            root = self.settings.studio_root / "projects" / project
            created = client.post(
                f"/api/project/{project}/clips", json={"title": "Closing"})
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["clip"]["id"], "clip-002")

            first = root / "clips" / "clip-001"
            second = root / "clips" / "clip-002"
            (first / "current_prompt.txt").write_text("First 5-second prompt\n")
            (second / "current_prompt.txt").write_text("Second 6-second prompt\n")
            for clip_id, seed in (("clip-001", "11"), ("clip-002", "22")):
                response = client.put(
                    f"/api/project/{project}/clips/{clip_id}/generation-settings",
                    json=_generation_settings_payload(seed=seed),
                )
                self.assertEqual(response.status_code, 200)

            for clip, content in ((first, b"first"), (second, b"second")):
                generation = clip / "generations" / "001"
                generation.mkdir()
                (generation / "take.mp4").write_bytes(content)

            first_detail = client.get(
                f"/api/project/{project}/clips/clip-001").json()
            second_detail = client.get(
                f"/api/project/{project}/clips/clip-002").json()
            self.assertEqual(first_detail["current_prompt"], "First 5-second prompt\n")
            self.assertEqual(second_detail["current_prompt"], "Second 6-second prompt\n")
            self.assertEqual(first_detail["generation_settings"]["settings"]["seed"],
                             "11")
            self.assertEqual(second_detail["generation_settings"]["settings"]["seed"],
                             "22")

            first_take = client.get(
                f"/api/project/{project}/clips/clip-001/generations"
            ).json()["generations"][0]
            second_take = client.get(
                f"/api/project/{project}/clips/clip-002/generations"
            ).json()["generations"][0]
            self.assertIn("/clips/clip-001/", first_take["media"][0]["url"])
            self.assertIn("/clips/clip-002/", second_take["media"][0]["url"])
            self.assertEqual(client.get(first_take["media"][0]["url"]).content, b"first")
            self.assertEqual(client.get(second_take["media"][0]["url"]).content, b"second")

            selected = client.put(
                f"/api/project/{project}/clips/clip-002/selected-take",
                json={"generation": "001", "filename": "take.mp4"},
            )
            self.assertEqual(selected.status_code, 200)
            self.assertEqual(selected.json()["clip"]["selected_take"], {
                "generation": "001", "filename": "take.mp4"})

            updated = client.patch(
                f"/api/project/{project}/clips/clip-002",
                json={"title": "Finale", "enabled": False},
            )
            self.assertEqual(updated.status_code, 200)
            blocked_selection = client.put(
                f"/api/project/{project}/clips/clip-002/selected-take",
                json={"generation": "001", "filename": "take.mp4"},
            )
            self.assertEqual(blocked_selection.status_code, 400)
            reordered = client.put(
                f"/api/project/{project}/clips/order",
                json={"clip_ids": ["clip-002", "clip-001"]},
            )
            self.assertEqual(
                [entry["id"] for entry in reordered.json()["clips"]],
                ["clip-002", "clip-001"],
            )
            project_state = client.get(f"/api/project/{project}").json()
            self.assertEqual(project_state["clips"][0]["title"], "Finale")
            self.assertFalse(project_state["clips"][0]["enabled"])
            self.assertIsNone(project_state["clips"][1]["selected_take"])
            self.assertEqual(
                client.get(f"/api/project/{project}/generation-settings").status_code,
                404,
            )
            self.assertEqual(
                client.get(f"/api/project/{project}/generations").status_code, 404)

    def test_project_metadata_symlinks_are_not_read(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "metadata-symlink")
            root = self.settings.studio_root / "projects" / project
            outside = Path(self.temp.name) / "outside.txt"
            outside.write_text("outside secret")
            (root / "brief.md").unlink()
            (root / "brief.md").symlink_to(outside)
            listing = client.get("/api/projects").json()["projects"]
            self.assertEqual(listing[0]["brief"], "")
            self.assertEqual(
                client.get(f"/api/project/{project}").json()["brief"], "")

    def test_project_metadata_parent_swap_is_not_read(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "metadata-parent-swap")
            root = self.settings.studio_root / "projects" / project
            displaced = Path(self.temp.name) / "displaced-project"
            outside = Path(self.temp.name) / "outside-project"
            outside.mkdir()
            (outside / "brief.md").write_text("outside secret")
            real_open = os.open
            swapped = False

            def swap_parent_at_final_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if not swapped and Path(path).name == "brief.md":
                    swapped = True
                    root.rename(displaced)
                    root.symlink_to(outside, target_is_directory=True)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch("studio_core.safe_files.os.open",
                       side_effect=swap_parent_at_final_open):
                listing = client.get("/api/projects").json()["projects"]

            self.assertTrue(swapped)
            self.assertEqual(listing[0]["brief"], "")
            self.assertEqual((outside / "brief.md").read_text(), "outside secret")

    def test_media_route_exposes_only_media_areas(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client)
            root = self.settings.studio_root / "projects" / project
            (root / "references" / "reference.png").write_bytes(b"image")
            self.assertEqual(client.get(
                f"/media/projects/{project}/references/reference.png"
            ).status_code, 200)
            self.assertEqual(client.get(
                f"/media/projects/{project}/brief.md/x"
            ).status_code, 404)
            self.assertEqual(client.get(
                f"/media/projects/{project}/research/note.md"
            ).status_code, 404)

    def test_media_response_remains_bound_to_validated_file_identity(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "media-identity")
            root = self.settings.studio_root / "projects" / project
            source = root / "references" / "reference.png"
            displaced = root / "references" / "validated.png"
            outside = Path(self.temp.name) / "outside-secret.png"
            source.write_bytes(b"validated")
            outside.write_bytes(b"secret")
            real_pread = os.pread
            swapped = False

            def swap_before_read(descriptor, amount, offset):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    source.rename(displaced)
                    source.symlink_to(outside)
                return real_pread(descriptor, amount, offset)

            with patch("webapp.safe_response.os.pread",
                       side_effect=swap_before_read):
                response = client.get(
                    f"/media/projects/{project}/references/reference.png")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"validated")
            self.assertTrue(swapped)
            self.assertEqual(source.read_bytes(), b"secret")

    def test_media_response_supports_single_byte_ranges(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "media-range")
            root = self.settings.studio_root / "projects" / project
            (root / "references" / "reference.png").write_bytes(b"012345")
            url = f"/media/projects/{project}/references/reference.png"

            partial = client.get(url, headers={"Range": "bytes=1-3"})
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.content, b"123")
            self.assertEqual(partial.headers["content-range"], "bytes 1-3/6")
            self.assertEqual(partial.headers["accept-ranges"], "bytes")

            unsatisfied = client.get(url, headers={"Range": "bytes=9-10"})
            self.assertEqual(unsatisfied.status_code, 416)
            self.assertEqual(unsatisfied.headers["content-range"], "bytes */6")

            (root / "references" / "empty.png").write_bytes(b"")
            empty = client.get(
                f"/media/projects/{project}/references/empty.png")
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.content, b"")
            self.assertEqual(empty.headers["content-length"], "0")

    def test_generation_detail_and_review_actions(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "review")
            root = self.settings.studio_root / "projects" / project
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"video")
            (generation / "still.png").write_bytes(b"image")
            (generation / "prompt.txt").write_text("structured prompt\n")
            (generation / "meta.json").write_text(
                json.dumps({"seed": 42, "recipe": "r2v"}))

            listing = client.get(
                f"/api/project/{project}/clips/clip-001/generations"
            ).json()["generations"]
            self.assertEqual(listing[0]["files"], ["still.png", "video.mp4"])
            self.assertEqual(
                {item["kind"] for item in listing[0]["media"]},
                {"image", "video"},
            )
            detail = client.get(
                f"/api/project/{project}/clips/clip-001/generations/001"
            ).json()
            self.assertEqual(detail["prompt"], "structured prompt\n")
            self.assertEqual(detail["meta"]["seed"], 42)

            promoted = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "video.mp4"},
            )
            self.assertEqual(promoted.status_code, 200)
            promoted_name = promoted.json()["result"]["target"]
            self.assertEqual(
                (root / "final" / promoted_name).read_bytes(), b"video")
            self.assertEqual((generation / "video.mp4").read_bytes(), b"video")

            repeated = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "video.mp4"},
            )
            self.assertEqual(
                repeated.json()["result"]["target"], promoted_name)

            referenced = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/use-as-reference",
                json={"filename": "still.png"},
            )
            self.assertEqual(referenced.status_code, 200)
            reference_name = referenced.json()["result"]["target"]
            self.assertEqual(
                (root / "references" / reference_name).read_bytes(), b"image")
            refreshed = client.get(
                f"/api/project/{project}/clips/clip-001/generations/001"
            ).json()
            media = {item["name"]: item for item in refreshed["media"]}
            self.assertTrue(media["video.mp4"]["promoted"])
            self.assertTrue(media["still.png"]["reference"])

    def test_promote_retry_republishes_when_content_identity_changes(self):
        for changed_side in ("source", "target"):
            with self.subTest(changed_side=changed_side), TestClient(
                    self.app()) as client:
                project = self.create_project(
                    client, f"review-content-{changed_side}")
                root = self.settings.studio_root / "projects" / project
                generation = (
                    root / "clips" / "clip-001" / "generations" / "001")
                generation.mkdir()
                source = generation / "video.mp4"
                source.write_bytes(b"original")
                endpoint = (
                    f"/api/project/{project}/clips/clip-001/"
                    "generations/001/promote")

                first = client.post(
                    endpoint, json={"filename": "video.mp4"}).json()["result"]
                first_target = root / "final" / first["target"]
                if changed_side == "source":
                    source.write_bytes(b"replacement source")
                else:
                    first_target.write_bytes(b"replacement target")

                repeated = client.post(
                    endpoint, json={"filename": "video.mp4"})

                self.assertEqual(repeated.status_code, 200, repeated.text)
                second = repeated.json()["result"]
                self.assertNotEqual(second["target"], first["target"])
                self.assertEqual(
                    (root / "final" / second["target"]).read_bytes(),
                    source.read_bytes(),
                )
                review = json.loads(
                    (generation / ".review.json").read_text())
                self.assertEqual(len(review["actions"]), 2)
                for action in review["actions"]:
                    self.assertRegex(action["source_sha256"], r"^[0-9a-f]{64}$")
                    self.assertEqual(
                        action["source_sha256"], action["target_sha256"])

    def test_delete_take_clears_selection_and_preserves_published_copy(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "delete-take")
            root = self.settings.studio_root / "projects" / project
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"video")
            (generation / "prompt.txt").write_text("prompt")

            selected = client.put(
                f"/api/project/{project}/clips/clip-001/selected-take",
                json={"generation": "001", "filename": "video.mp4"},
            )
            self.assertEqual(selected.status_code, 200)
            promoted = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "video.mp4"},
            )
            promoted_name = promoted.json()["result"]["target"]

            deleted = client.delete(
                f"/api/project/{project}/clips/clip-001/generations/001")

            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(deleted.json()["deleted"], "001")
            self.assertIsNone(deleted.json()["clip"]["selected_take"])
            self.assertFalse(generation.exists())
            self.assertEqual(
                (root / "final" / promoted_name).read_bytes(), b"video")
            self.assertEqual(client.get(
                f"/api/project/{project}/clips/clip-001/generations"
            ).json()["generations"], [])
            self.assertEqual(client.delete(
                f"/api/project/{project}/clips/clip-001/generations/001"
            ).status_code, 404)

    def test_delete_take_rejects_unsafe_generation_and_active_project_job(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "delete-take-safety")
            root = self.settings.studio_root / "projects" / project
            generations = root / "clips" / "clip-001" / "generations"
            outside = Path(self.temp.name) / "outside-take"
            outside.mkdir()
            (outside / "secret.mp4").write_bytes(b"secret")
            (generations / "unsafe").symlink_to(outside, target_is_directory=True)

            unsafe = client.delete(
                f"/api/project/{project}/clips/clip-001/generations/unsafe")
            self.assertEqual(unsafe.status_code, 400)
            self.assertEqual((outside / "secret.mp4").read_bytes(), b"secret")

            generation = generations / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"video")
            queued = client.post(
                f"/api/project/{project}/clips/clip-001/chat",
                json={"message": "working"},
            )
            self.assertEqual(queued.status_code, 202)
            active = client.delete(
                f"/api/project/{project}/clips/clip-001/generations/001")
            self.assertEqual(active.status_code, 409)
            self.assertTrue(generation.is_dir())

    def test_failed_delete_restores_selected_take(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "delete-take-rollback")
            root = self.settings.studio_root / "projects" / project
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"video")
            client.put(
                f"/api/project/{project}/clips/clip-001/selected-take",
                json={"generation": "001", "filename": "video.mp4"},
            )

            with patch(
                    "studio_core.projects.remove_published_directory_if_same",
                    return_value=False):
                response = client.delete(
                    f"/api/project/{project}/clips/clip-001/generations/001")

            self.assertEqual(response.status_code, 400)
            self.assertTrue(generation.is_dir())
            clip = client.get(f"/api/project/{project}").json()["clips"][0]
            self.assertEqual(clip["selected_take"], {
                "generation": "001", "filename": "video.mp4"})

    def test_generation_review_actions_never_overwrite_and_reject_bad_sources(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "review-safety")
            root = self.settings.studio_root / "projects" / project
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"new")
            (generation / "prompt.txt").write_text("not media")
            (root / "final" / "clip-001_001_video.mp4").write_bytes(b"existing")

            promoted = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "video.mp4"},
            )
            self.assertEqual(promoted.status_code, 200)
            self.assertEqual(
                promoted.json()["result"]["target"],
                "clip-001_001_video_2.mp4")
            self.assertEqual(
                (root / "final" / "clip-001_001_video.mp4").read_bytes(),
                b"existing")
            self.assertEqual(
                (root / "final" / "clip-001_001_video_2.mp4").read_bytes(),
                b"new")

            traversal = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "../video.mp4"},
            )
            unsupported = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "prompt.txt"},
            )
            missing = client.get(
                f"/api/project/{project}/clips/clip-001/generations/999")
            self.assertEqual(traversal.status_code, 400)
            self.assertEqual(unsupported.status_code, 415)
            self.assertEqual(missing.status_code, 404)

    def test_review_rollback_preserves_replacement_at_published_name(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "review-rollback-identity")
            root = self.settings.studio_root / "projects" / project
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"video")
            target = root / "final" / "clip-001_001_video.mp4"
            displaced = root / "final" / ".published-original"

            def replace_target_then_fail(*_args, **_kwargs):
                target.rename(displaced)
                target.write_bytes(b"replacement")
                raise safe_files.SafeFilesystemError("injected review failure")

            with patch(
                    "webapp.media_review_store.atomic_write_bytes_at",
                    side_effect=replace_target_then_fail):
                response = client.post(
                    f"/api/project/{project}/clips/clip-001/"
                    "generations/001/promote",
                    json={"filename": "video.mp4"},
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(target.read_bytes(), b"replacement")
            self.assertEqual(displaced.read_bytes(), b"video")

    def test_concurrent_promote_is_idempotent(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "review-concurrent")
            root = self.settings.studio_root / "projects" / project
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"video")

            def promote():
                return client.post(
                    f"/api/project/{project}/clips/clip-001/generations/001/promote",
                    json={"filename": "video.mp4"},
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: promote(), range(2)))
            self.assertTrue(all(response.status_code == 200
                                for response in responses))
            self.assertEqual(
                {response.json()["result"]["target"] for response in responses},
                {"clip-001_001_video.mp4"},
            )
            self.assertEqual([
                item.name for item in (root / "final").iterdir()
                if not item.name.startswith(".")
            ], ["clip-001_001_video.mp4"])

    def test_media_and_upload_reject_symlinked_reference_directory(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "symlink")
            root = self.settings.studio_root / "projects" / project
            outside = Path(self.temp.name) / "outside"
            outside.mkdir()
            (root / "references").rmdir()
            (root / "references").symlink_to(outside, target_is_directory=True)
            (outside / "secret.png").write_bytes(b"secret")
            self.assertEqual(client.get(
                f"/media/projects/{project}/references/secret.png"
            ).status_code, 404)
            response = client.post(
                f"/api/project/{project}/references",
                files={"files": ("image.png", b"image", "image/png")},
            )
            self.assertEqual(response.status_code, 400)
            self.assertFalse((outside / "image.png").exists())
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "still.png").write_bytes(b"image")
            reviewed = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/use-as-reference",
                json={"filename": "still.png"},
            )
            self.assertEqual(reviewed.status_code, 400)
            self.assertFalse(
                (outside / "clip-001_001_still.png").exists())

    def test_concurrent_same_name_uploads_never_overwrite(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client)
            barrier = Barrier(2)

            def upload(content):
                barrier.wait()
                return client.post(
                    f"/api/project/{project}/references",
                    files={"files": ("same.png", content, "image/png")},
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(upload, (b"first", b"second")))
            self.assertTrue(all(response.status_code == 201
                                for response in responses))
            names = sorted(
                response.json()["references"][0]["name"]
                for response in responses
            )
            self.assertEqual(names, ["same.png", "same_2.png"])
            directory = (
                self.settings.studio_root / "projects" / project / "references")
            contents = {
                path.read_bytes() for path in directory.iterdir()
                if not path.name.startswith(".")
            }
            self.assertEqual(contents, {b"first", b"second"})

    def test_failed_upload_batch_rolls_back(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client)
            response = client.post(
                f"/api/project/{project}/references",
                files=[
                    ("files", ("small.png", b"ok", "image/png")),
                    ("files", ("large.png", b"x" * 2048, "image/png")),
                ],
            )
            self.assertEqual(response.status_code, 413)
            directory = (
                self.settings.studio_root / "projects" / project / "references")
            self.assertEqual([
                path for path in directory.iterdir()
                if not path.name.startswith(".")
            ], [])

    def test_movie_readiness_reports_every_enabled_clip_in_manifest_order(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "movie-readiness")
            root = self.settings.studio_root / "projects" / project
            created = client.post(
                f"/api/project/{project}/clips", json={"title": "Ending"})
            self.assertEqual(created.status_code, 201)

            generation = (
                root / "clips" / "clip-001" / "generations" / "001")
            generation.mkdir()
            (generation / "opening.mp4").write_bytes(b"selected video")
            selected = client.put(
                f"/api/project/{project}/clips/clip-001/selected-take",
                json={"generation": "001", "filename": "opening.mp4"},
            )
            self.assertEqual(selected.status_code, 200)

            response = client.get(f"/api/project/{project}/movie")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {
                "readiness": {
                    "ready": False,
                    "enabled_clip_count": 2,
                    "clips": [
                        {
                            "id": "clip-001",
                            "title": "Main clip",
                            "ready": True,
                            "reason": "",
                            "selected_take": {
                                "generation": "001",
                                "filename": "opening.mp4",
                            },
                        },
                        {
                            "id": "clip-002",
                            "title": "Ending",
                            "ready": False,
                            "reason": "Select a video take",
                            "selected_take": None,
                        },
                    ],
                    "blocking": [
                        {"id": "clip-002", "title": "Ending",
                         "reason": "Select a video take"},
                    ],
                },
                "movies": [],
            })

            (generation / "opening.mp4").unlink()
            stale = client.get(f"/api/project/{project}/movie")
            self.assertEqual(stale.status_code, 200)
            self.assertEqual(
                stale.json()["readiness"]["clips"][0]["reason"],
                "Selected video is missing or unsafe",
            )

    def test_movie_export_is_one_visible_project_job_and_freezes_selection(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "movie-job")
            root = self.settings.studio_root / "projects" / project
            generation = (
                root / "clips" / "clip-001" / "generations" / "001")
            generation.mkdir()
            (generation / "take.mp4").write_bytes(b"selected video")
            selected = client.put(
                f"/api/project/{project}/clips/clip-001/selected-take",
                json={"generation": "001", "filename": "take.mp4"},
            )
            self.assertEqual(selected.status_code, 200)

            submitted = client.post(f"/api/project/{project}/movie")
            self.assertEqual(submitted.status_code, 202)
            job = submitted.json()
            self.assertEqual(job["kind"], "export_movie")
            self.assertEqual(job["chat_scope"], "project")
            self.assertEqual(job["clip_id"], "")
            self.assertEqual(job["status"], "queued")

            jobs = client.get(f"/api/project/{project}/jobs").json()["jobs"]
            self.assertEqual([item["id"] for item in jobs], [job["id"]])
            blocked = client.put(
                f"/api/project/{project}/clips/clip-001/selected-take",
                json={"generation": None, "filename": None},
            )
            self.assertEqual(blocked.status_code, 409)
