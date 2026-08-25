import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from scripts import design_studio as ds
from studio_core.projects import ClipStore
from webapp.config import Settings
from studio_core.job_store import JobStore
from studio_core.models import JobStatus
from webapp.movie_store import MovieStore, MovieStoreError
from webapp.studio_manager import StudioJobManager


class MovieExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            repo=Path(__file__).resolve().parent.parent,
            studio_root=root / "studio",
            comfy_output=root / "comfy-output",
            runtime_root=root / "runtime",
            job_timeout_seconds=30,
        )
        self.settings.comfy_output.mkdir()
        self.project = ds.create_project(self.settings.studio_root, "movie-export")
        self.clips = ClipStore()
        self.clips.create_clip(self.project, "Ending")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _make_video(path: Path, *, width: int, height: int,
                    fps: int, color: str, audio: bool) -> None:
        command = [
            "ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi",
            "-i", f"color=c={color}:s={width}x{height}:r={fps}:d=0.4",
        ]
        if audio:
            command += [
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:d=0.4",
                "-shortest",
            ]
        command += [
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            *(["-c:a", "aac"] if audio else ["-an"]),
            "-movflags", "+faststart", str(path),
        ]
        subprocess.run(command, check=True, capture_output=True)

    def _selected_video(self, clip_id: str, generation_id: str, **video) -> Path:
        generation = (
            self.project / "clips" / clip_id / "generations" / generation_id)
        generation.mkdir()
        media = generation / "take.mp4"
        self._make_video(media, **video)
        self.clips.select_take(
            self.project, clip_id, generation_id, media.name)
        return media

    @staticmethod
    def _probe(path: Path) -> dict:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", str(path),
        ], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def _wait_for_terminal(self, store: JobStore, job_id: str):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = store.get_job(job_id)
            if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
                return job
            time.sleep(0.05)
        self.fail("movie export job did not finish")

    def test_supervised_movie_job_stream_copies_compatible_sources(self):
        first = self._selected_video(
            "clip-001", "001", width=160, height=96,
            fps=10, color="red", audio=True)
        second = self._selected_video(
            "clip-002", "001", width=160, height=96,
            fps=10, color="blue", audio=True)
        original = {path: path.read_bytes() for path in (first, second)}
        store = JobStore(self.settings.database_path)
        cleanup_calls = []
        manager = StudioJobManager(
            self.settings, store,
            cleanup_callback=lambda: cleanup_calls.append(True))
        manager.start()
        try:
            job = manager.submit_movie_export(self.project.name)
            completed = self._wait_for_terminal(store, job.id)
        finally:
            manager.stop()

        self.assertEqual(completed.status, JobStatus.COMPLETED, completed.error)
        self.assertEqual(completed.kind, "export_movie")
        self.assertEqual(completed.chat_scope, "project")
        self.assertIn("movie-001/movie.mp4", completed.reply)
        movie = self.project / "final" / "movie-001" / "movie.mp4"
        provenance_path = movie.with_name("provenance.json")
        self.assertTrue(movie.is_file())
        provenance = json.loads(provenance_path.read_text())
        self.assertEqual(provenance["job_id"], job.id)
        self.assertEqual(provenance["assembly"]["mode"], "stream-copy")
        self.assertEqual(
            [source["clip_id"] for source in provenance["sources"]],
            ["clip-001", "clip-002"],
        )
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertEqual(MovieStore().verify_export(
            self.project, json.loads(job.message), job.id)["id"], "movie-001")
        self.assertEqual(cleanup_calls, [])

    def test_mismatched_sources_are_deterministically_normalized(self):
        self._selected_video(
            "clip-001", "001", width=160, height=96,
            fps=10, color="red", audio=True)
        self._selected_video(
            "clip-002", "001", width=128, height=72,
            fps=15, color="blue", audio=False)
        movies = MovieStore()
        contract = movies.build_contract(self.project)
        self.assertEqual(contract["assembly"]["mode"], "normalized")

        result = movies.export(self.project, contract, "job-normalized")
        movie = self.project / "final" / result["id"] / "movie.mp4"
        probe = self._probe(movie)
        video = next(stream for stream in probe["streams"]
                     if stream["codec_type"] == "video")
        audio = next(stream for stream in probe["streams"]
                     if stream["codec_type"] == "audio")
        self.assertEqual((video["width"], video["height"]), (160, 96))
        self.assertEqual(video["r_frame_rate"], "10/1")
        self.assertEqual(int(audio["sample_rate"]), 48000)
        self.assertEqual(audio["channels"], 2)
        self.assertGreater(float(probe["format"]["duration"]), 0.7)
        provenance = json.loads(movie.with_name("provenance.json").read_text())
        self.assertEqual(provenance["assembly"]["mode"], "normalized")

        next_contract = movies.build_contract(self.project)
        self.assertEqual(next_contract["output"]["id"], "movie-002")

    def test_export_rejects_source_content_changed_after_enqueue(self):
        source = self._selected_video(
            "clip-001", "001", width=160, height=96,
            fps=10, color="red", audio=True)
        self.clips.update_clip(self.project, "clip-002", enabled=False)
        movies = MovieStore()
        contract = movies.build_contract(self.project)
        source.write_bytes(b"changed after enqueue")

        with self.assertRaisesRegex(ValueError, "changed after enqueue"):
            movies.export(self.project, contract, "job-stale")
        self.assertFalse((self.project / "final" / "movie-001").exists())

    def test_tampered_filter_contract_is_rejected_before_ffmpeg(self):
        self._selected_video(
            "clip-001", "001", width=160, height=96,
            fps=10, color="red", audio=True)
        self._selected_video(
            "clip-002", "001", width=128, height=72,
            fps=15, color="blue", audio=False)
        contract = MovieStore().build_contract(self.project)
        contract["assembly"]["target"]["fps"] = "10;movie=/etc/passwd"

        with self.assertRaisesRegex(MovieStoreError, "contract is invalid"):
            MovieStore().export(self.project, contract, "job-tampered")

    def test_publication_never_overwrites_allocated_version(self):
        self._selected_video(
            "clip-001", "001", width=160, height=96,
            fps=10, color="red", audio=True)
        self.clips.update_clip(self.project, "clip-002", enabled=False)
        movies = MovieStore()
        contract = movies.build_contract(self.project)
        collision = self.project / "final" / "movie-001"
        collision.mkdir()
        sentinel = collision / "keep.txt"
        sentinel.write_text("do not overwrite")

        with self.assertRaises(MovieStoreError):
            movies.export(self.project, contract, "job-collision")

        self.assertEqual(sentinel.read_text(), "do not overwrite")
        self.assertFalse(any(
            path.name.startswith(".movie-")
            for path in (self.project / "final").iterdir()))
