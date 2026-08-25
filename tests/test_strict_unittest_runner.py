import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class StrictPythonTestRunnerTests(unittest.TestCase):
    def run_fixture(self, source):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_fixture.py").write_text(source, encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    "scripts/run_python_tests.py",
                    "--start-directory", str(root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_clean_suite_passes(self):
        result = self.run_fixture(
            "import unittest\n"
            "class FixtureTests(unittest.TestCase):\n"
            "    def test_clean(self):\n"
            "        self.assertTrue(True)\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_resource_warning_fails(self):
        result = self.run_fixture(
            "import unittest\n"
            "import warnings\n"
            "class FixtureTests(unittest.TestCase):\n"
            "    def test_leak(self):\n"
            "        warnings.warn('leaked resource', ResourceWarning)\n"
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("strict test gate rejected", result.stderr)

    def test_unraisable_diagnostic_fails(self):
        result = self.run_fixture(
            "import unittest\n"
            "class BrokenFinalizer:\n"
            "    def __del__(self):\n"
            "        raise RuntimeError('finalizer failed')\n"
            "class FixtureTests(unittest.TestCase):\n"
            "    def test_unraisable(self):\n"
            "        BrokenFinalizer()\n"
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Exception ignored", result.stdout)


if __name__ == "__main__":
    unittest.main()
