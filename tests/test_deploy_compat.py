"""Exercise the shared deploy recipe against the checkout layouts on bee."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "host" / "deploy.sh"


class SportsbookDeployCompatibility(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.project = self.home / "services" / "sportsbook"
        (self.project / ".git").mkdir(parents=True)
        (self.project / "scripts").mkdir()
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.log = self.home / "calls"
        self._fake("docker", "exit 0")
        self._fake("python3", 'printf "%s\\n" "$*" >> "$CALL_LOG"')
        self._fake("systemctl", 'test "${COLLECTOR_ACTIVE:-0}" = 1')

    def _fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def _run(self, *, image: str = "", collector_active: bool = False) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "CALL_LOG": str(self.log),
            "COLLECTOR_ACTIVE": "1" if collector_active else "0",
        }
        if image:
            env["DEPLOY_IMAGE"] = image
        else:
            env.pop("DEPLOY_IMAGE", None)
        return subprocess.run(["bash", str(SCRIPT), "sportsbook", "a" * 40], env=env, text=True, capture_output=True)

    def test_legacy_checkout_keeps_collector_restore(self) -> None:
        (self.project / "scripts" / "setup_wsl.py").touch()
        result = self._run(collector_active=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), [
            f"scripts/setup_wsl.py update --ref {'a' * 40} --wait",
            "scripts/setup_wsl.py activate-collector",
            "scripts/setup_wsl.py status",
        ])

    def test_modern_checkout_uses_loaded_release_image(self) -> None:
        (self.project / "scripts" / "deploy.py").touch()
        (self.project / "scripts" / "setup_wsl.py").touch()
        result = self._run(image=f"sportsbook:{'a' * 40}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), [
            f"scripts/deploy.py update --ref {'a' * 40} --wait --image sportsbook:{'a' * 40}",
            "scripts/deploy.py status",
        ])

    def test_legacy_checkout_refuses_release_image(self) -> None:
        (self.project / "scripts" / "setup_wsl.py").touch()
        result = self._run(image=f"sportsbook:{'a' * 40}")
        self.assertEqual(result.returncode, 4)
        self.assertIn("lacks a compatible updater", result.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
