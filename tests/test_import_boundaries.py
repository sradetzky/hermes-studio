import ast
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ImportBoundaryTests(unittest.TestCase):
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
        from webapp import clip_store

        self.assertIs(clip_store, projects)
        self.assertIs(design_studio.ClipStore, projects.ClipStore)
        self.assertIs(design_studio.project_path, projects.project_path)

    def test_runtime_and_generation_owners_are_in_studio_core(self):
        from studio_core import generation_archive, job_store, models
        from webapp import generation_contract
        from webapp import job_store as web_job_store
        from webapp import models as web_models

        self.assertIs(generation_contract, generation_archive)
        self.assertIs(web_job_store, job_store)
        self.assertIs(web_models, models)

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


if __name__ == "__main__":
    unittest.main()
