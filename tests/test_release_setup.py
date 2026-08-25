import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_tool_versions


REPO_ROOT = Path(__file__).resolve().parent.parent


class ReleaseSetupTests(unittest.TestCase):
    def test_profile_contracts_reserve_web_generation_for_worker(self):
        soul = (REPO_ROOT / "hermes/profiles/studio/SOUL.md").read_text()
        skill = (REPO_ROOT / "hermes/skills/design-studio/SKILL.md").read_text()
        illustrator = (
            REPO_ROOT / "hermes/profiles/studio-illustrator/SOUL.md").read_text()
        prompt_engineer = (
            REPO_ROOT / "hermes/profiles/studio-prompt-engineer/SOUL.md").read_text()

        for obsolete in (
            "## Web Generate Contract",
            "you execute every GPU job",
            "For a web generation request",
            "Only the `studio` orchestrator may execute these steps",
        ):
            self.assertNotIn(obsolete, soul)
            self.assertNotIn(obsolete, skill)
        self.assertIn("sole owner of web H3 rendering", soul)
        self.assertIn("Web profile toolsets intentionally exclude ComfyUI/MCP", skill)
        self.assertIn("use the `clarify` tool", soul)
        self.assertIn("Web profile jobs include the `clarify` tool", skill)
        self.assertIn("Web profiles do not execute local ComfyUI jobs", illustrator)
        self.assertIn("deterministic worker own web H3 execution", prompt_engineer)

    def test_service_uses_stable_launcher_instead_of_checkout_path(self):
        unit = (REPO_ROOT / "webapp/hermes-studio.service").read_text()
        self.assertIn("ExecStart=%h/.local/bin/hermes-studio-web", unit)
        self.assertNotIn("repos/hermes-studio", unit)
        self.assertNotIn("WorkingDirectory=", unit)

    def test_installer_renders_exact_alternate_checkout_and_reads_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "alternate checkout"
            home = root / "home"
            config_home = root / "config"
            fake_bin = root / "bin"
            (checkout / "scripts").mkdir(parents=True)
            (checkout / "webapp").mkdir()
            fake_bin.mkdir()
            for relative in (
                "scripts/install-web-service.sh",
                "webapp/hermes-studio.service",
                "webapp/run.sh",
            ):
                source = REPO_ROOT / relative
                target = checkout / relative
                shutil.copy2(source, target)
            systemctl_log = root / "systemctl.log"
            fake_systemctl = fake_bin / "systemctl"
            fake_systemctl.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n",
                encoding="utf-8",
            )
            fake_systemctl.chmod(
                fake_systemctl.stat().st_mode | stat.S_IXUSR)
            environment = {
                **os.environ,
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(config_home),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "SYSTEMCTL_LOG": str(systemctl_log),
            }

            result = subprocess.run(
                [checkout / "scripts/install-web-service.sh", "--enable"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            launcher = home / ".local/bin/hermes-studio-web"
            installed_unit = config_home / "systemd/user/hermes-studio.service"
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertIn(str(checkout / "webapp/run.sh"), launcher.read_text())
            self.assertEqual(
                installed_unit.read_bytes(),
                (checkout / "webapp/hermes-studio.service").read_bytes(),
            )
            self.assertEqual(
                systemctl_log.read_text().splitlines(),
                ["--user daemon-reload", "--user enable --now hermes-studio.service"],
            )
            self.assertIn("verified launcher", result.stdout)

    def test_tool_contract_rejects_unsupported_hermes(self):
        def completed(arguments, **_kwargs):
            if arguments == ["hermes", "--version"]:
                return subprocess.CompletedProcess(
                    arguments, 0, "Hermes Agent v0.19.9\n", "")
            self.fail(f"unexpected command: {arguments}")

        with (
            patch.object(check_tool_versions.subprocess, "run", side_effect=completed),
            self.assertRaisesRegex(RuntimeError, "Hermes Agent >= 0.20.5"),
        ):
            check_tool_versions.check_hermes()

    def test_tool_contract_accepts_required_cli_and_pinned_mcp_versions(self):
        responses = {
            ("hermes", "--version"): "Hermes Agent v0.20.5 (build)\n",
            ("hermes", "chat", "--help"): (
                "--query --quiet --toolsets --resume --source\n"),
            ("npm", "view", "mcporter@0.13.7", "version", "--json"): '"0.13.7"\n',
            ("npm", "view", "comfyui-mcp@0.52.61", "version", "--json"): '"0.52.61"\n',
        }

        def completed(arguments, **_kwargs):
            key = tuple(arguments)
            return subprocess.CompletedProcess(arguments, 0, responses[key], "")

        with patch.object(
                check_tool_versions.subprocess, "run", side_effect=completed):
            check_tool_versions.check_hermes()
            check_tool_versions.check_mcp_packages()


if __name__ == "__main__":
    unittest.main()
