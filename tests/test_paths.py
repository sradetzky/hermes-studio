import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from studio_core.paths import StudioPaths
from webapp.config import Settings


REPO_ROOT = Path(__file__).resolve().parent.parent


class StudioPathTests(unittest.TestCase):
    def test_normal_and_profile_isolated_environments_share_account_resources(self):
        account = Path("/srv/studio-user")
        fleet = account / ".hermes"
        normal = StudioPaths.from_environment({
            "HERMES_REAL_HOME": str(account),
            "HERMES_HOME": str(fleet),
        })
        isolated = StudioPaths.from_environment({
            "HOME": str(fleet / "profiles/studio/home"),
            "HERMES_REAL_HOME": str(account),
            "HERMES_HOME": str(fleet / "profiles/studio"),
        })

        self.assertEqual(normal.real_home, account)
        self.assertEqual(isolated.real_home, account)
        self.assertEqual(normal.hermes_root, fleet)
        self.assertEqual(isolated.hermes_root, fleet)
        self.assertEqual(normal.comfy_root, account / "ComfyUI")
        self.assertEqual(isolated.comfy_root, account / "ComfyUI")
        self.assertEqual(
            isolated.active_profile_home, fleet / "profiles/studio")

    def test_explicit_paths_remain_literal(self):
        paths = StudioPaths.from_environment({
            "HERMES_REAL_HOME": "relative/real/../home",
            "HERMES_HOME": "fleet/profiles/studio",
            "COMFYUI_PATH": "relative/ComfyUI/../ComfyUI",
        })

        self.assertEqual(paths.real_home, Path("relative/real/../home"))
        self.assertEqual(paths.hermes_root, Path("fleet"))
        self.assertEqual(paths.active_profile_home, Path("fleet/profiles/studio"))
        self.assertEqual(
            paths.comfy_root, Path("relative/ComfyUI/../ComfyUI"))

    def test_empty_explicit_path_is_rejected_instead_of_repaired(self):
        with self.assertRaisesRegex(ValueError, "HERMES_HOME may not be empty"):
            StudioPaths.from_environment({
                "HERMES_REAL_HOME": "/home/studio",
                "HERMES_HOME": "",
            })

    def test_settings_use_canonical_fleet_and_comfy_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account = root / "account"
            fleet = root / "fleet"
            profile = fleet / "profiles/studio"
            environment = {
                "HOME": str(profile / "home"),
                "HERMES_REAL_HOME": str(account),
                "HERMES_HOME": str(profile),
                "COMFYUI_PATH": str(root / "custom-comfy"),
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_environment()

            self.assertEqual(settings.real_home, account)
            self.assertEqual(settings.hermes_root, fleet)
            self.assertEqual(settings.active_profile_home, profile)
            self.assertEqual(settings.comfy_root, root / "custom-comfy")
            self.assertEqual(settings.comfy_output, root / "custom-comfy/output")
            self.assertEqual(
                settings.profile_state_path("studio"),
                fleet / "profiles/studio/state.db",
            )
            self.assertEqual(
                settings.profile_state_path("default"), fleet / "state.db")

    def test_sync_profiles_uses_fleet_root_from_isolated_profile_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fleet = root / ".hermes"
            profile_home = fleet / "profiles/studio"
            for profile in (
                "studio", "studio-storyboarder", "studio-prompt-engineer",
                "studio-reviewer", "studio-illustrator",
            ):
                config = fleet / "profiles" / profile / "config.yaml"
                config.parent.mkdir(parents=True)
                config.write_text("model: {}\n", encoding="utf-8")
            result = subprocess.run(
                [REPO_ROOT / "scripts/sync-profiles.sh"],
                cwd=REPO_ROOT,
                env={
                    **os.environ,
                    "HOME": str(profile_home / "home"),
                    "HERMES_REAL_HOME": str(root),
                    "HERMES_HOME": str(profile_home),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((
                fleet / "profiles/studio/skills/design-studio/SKILL.md"
            ).is_file())
            self.assertFalse((
                profile_home / "profiles/studio/skills/design-studio/SKILL.md"
            ).exists())

    def test_switch_model_reports_exact_changes_and_fails_without_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fleet = root / ".hermes"
            profile_home = fleet / "profiles/studio"
            config = profile_home / "config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "model:\n  provider: old-provider\n  default: old-model\n",
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "HOME": str(profile_home / "home"),
                "HERMES_REAL_HOME": str(root),
                "HERMES_HOME": str(profile_home),
            }
            script = REPO_ROOT / "scripts/switch-model.sh"
            changed = subprocess.run(
                [script, "new-provider", "new-model", "studio"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            missing = subprocess.run(
                [script, "new-provider", "new-model", "missing-profile"],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(changed.returncode, 0, changed.stderr)
            self.assertIn("Changed profiles: studio", changed.stdout)
            self.assertIn("provider: new-provider", config.read_text())
            self.assertIn("default: new-model", config.read_text())
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("no intended profiles could be updated", missing.stderr)
            self.assertIn("missing-profile", missing.stderr)


if __name__ == "__main__":
    unittest.main()
