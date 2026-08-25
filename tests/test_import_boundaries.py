import ast
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ImportBoundaryTests(unittest.TestCase):
    def test_case_modules_stay_below_the_split_threshold(self):
        oversized = {
            path.name: len(path.read_text(encoding="utf-8").splitlines())
            for path in (ROOT / "tests").glob("*_cases.py")
            if len(path.read_text(encoding="utf-8").splitlines()) > 1000
        }
        self.assertEqual(oversized, {})

    def test_cohesive_core_modules_keep_their_explicit_size_ceiling(self):
        limits = {
            ROOT / "studio_core" / "safe_files.py": 1000,
            ROOT / "studio_core" / "migration.py": 2200,
        }
        oversized = {
            path.name: {"lines": len(path.read_text(encoding="utf-8").splitlines()),
                        "limit": limit}
            for path, limit in limits.items()
            if len(path.read_text(encoding="utf-8").splitlines()) > limit
        }
        self.assertEqual(oversized, {})

    def test_job_manager_protocol_covers_every_route_submission(self):
        routes = ast.parse(
            (ROOT / "webapp" / "routes.py").read_text(encoding="utf-8"))
        app = ast.parse(
            (ROOT / "webapp" / "app.py").read_text(encoding="utf-8"))
        route_calls = {
            node.func.attr
            for node in ast.walk(routes)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "_manager"
        }
        protocol = next(
            node for node in app.body
            if isinstance(node, ast.ClassDef) and node.name == "JobManager")
        protocol_methods = {
            node.name for node in protocol.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertEqual(route_calls, {
            "submit_project_chat",
            "submit_chat",
            "submit_generation",
            "submit_movie_export",
        })
        self.assertLessEqual(route_calls, protocol_methods)

    def test_project_domain_is_owned_by_studio_core(self):
        from scripts import design_studio
        from studio_core import projects

        self.assertIs(design_studio.ClipStore, projects.ClipStore)
        self.assertIs(design_studio.project_path, projects.project_path)

    def test_deprecated_webapp_domain_aliases_are_removed(self):
        aliases = {
            "clip_store.py",
            "generation_contract.py",
            "hermes_events.py",
            "identifiers.py",
            "job_store.py",
            "models.py",
            "runtime_schema.py",
            "safe_files.py",
        }
        self.assertEqual(
            {path.name for path in (ROOT / "webapp").glob("*.py")} & aliases,
            set(),
        )

    def test_studio_core_does_not_import_webapp_or_scripts(self):
        forbidden = []
        for path in (ROOT / "studio_core").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name == "webapp" or name.startswith("webapp."):
                        forbidden.append(
                            f"{path.name}:{getattr(node, 'lineno', 0)}:{name}")
                    if name == "scripts" or name.startswith("scripts."):
                        forbidden.append(
                            f"{path.name}:{getattr(node, 'lineno', 0)}:{name}")
        self.assertEqual(forbidden, [])

    def test_scripts_do_not_import_webapp(self):
        forbidden = []
        for path in (ROOT / "scripts").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name == "webapp" or name.startswith("webapp."):
                        forbidden.append(
                            f"{path.name}:{getattr(node, 'lineno', 0)}:{name}")
        self.assertEqual(forbidden, [])

    def test_normal_webapp_import_does_not_load_migration_engine(self):
        migration = ROOT / "studio_core/migration.py"
        self.assertTrue(migration.is_file())
        design_source = (ROOT / "scripts/design_studio.py").read_text(
            encoding="utf-8")
        self.assertNotIn("def migrate_clips(", design_source)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import webapp.app; "
                "assert 'studio_core.migration' not in sys.modules",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_job_execution_is_split_behind_one_dispatch_boundary(self):
        files = {
            name: (ROOT / "webapp" / name).read_text(encoding="utf-8")
            for name in (
                "studio_manager.py",
                "process_runner.py",
                "agent_runner.py",
                "generation_job_service.py",
                "generation_runner.py",
                "movie_runner.py",
            )
        }
        self.assertTrue(all(text.count("\n") + 1 <= 1000
                            for text in files.values()))
        manager = files["studio_manager.py"]
        self.assertIn("runner = self._runners[job.kind]", manager)
        self.assertNotIn("subprocess.Popen", manager)
        self.assertNotIn("os.killpg", manager)
        self.assertNotIn('Path("/proc")', manager)
        self.assertIn("subprocess.Popen", files["process_runner.py"])
        self.assertIn("os.killpg", files["process_runner.py"])
        self.assertIn('Path("/proc")', files["process_runner.py"])
        self.assertNotIn("cleanup_comfyui", files["agent_runner.py"])
        self.assertNotIn("owns_gpu", files["agent_runner.py"])
        self.assertNotIn("cleanup_comfyui", files["movie_runner.py"])
        self.assertIn("cleanup_comfyui", files["generation_runner.py"])
        self.assertIn("job.kind is JobKind.GENERATE", manager)
        for generation_owner in (
                "GenerationInputStore", "GenerationSettingsStore", "MediaReviewStore"):
            self.assertNotIn(generation_owner, manager)
            self.assertIn(generation_owner, files["generation_job_service.py"])

    def test_movie_execution_keeps_the_typed_contract(self):
        contracts = (ROOT / "studio_core" / "movie_contracts.py").read_text(
            encoding="utf-8")
        store = (ROOT / "webapp" / "movie_store.py").read_text(encoding="utf-8")
        runner = (ROOT / "webapp" / "movie_runner.py").read_text(encoding="utf-8")
        self.assertIn("def build_movie_contract(", contracts)
        self.assertNotIn("def _copy_compatible(", store)
        self.assertNotIn("def _target_for_sources(", store)
        self.assertNotIn("contract = job.payload.contract.to_dict()", runner)
        self.assertNotIn('"--contract"', runner)

    def test_generation_archival_uses_an_explicit_typed_context(self):
        archive = (ROOT / "studio_core" / "generation_archive.py").read_text(
            encoding="utf-8")
        runner = (ROOT / "webapp" / "generation_runner.py").read_text(
            encoding="utf-8")
        worker = (ROOT / "webapp" / "generation_worker.py").read_text(
            encoding="utf-8")
        self.assertIn("class GenerationArchiveContext:", archive)
        self.assertNotIn("import sqlite3", archive)
        self.assertNotIn("load_running_generation_contract", archive)
        self.assertNotIn("HERMES_STUDIO_JOB_ID", archive)
        self.assertIn("generation_context=GenerationArchiveContext(", runner)
        self.assertNotIn("os.environ.update", worker)


if __name__ == "__main__":
    unittest.main()
