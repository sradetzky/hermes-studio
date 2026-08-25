import ast
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ImportBoundaryTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
