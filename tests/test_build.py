"""Builds the zipapp via scripts/build.sh and runs it, so a break in the
build recipe (see README's "Share it" section) fails the suite instead of
being discovered only when someone tries to share the tool."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class BuildScriptTests(unittest.TestCase):
    def test_build_then_help_exits_0(self) -> None:
        if shutil.which("uv") is None:
            self.skipTest("uv not on PATH; can't run the built zipapp")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pr-outcomes"
            build = subprocess.run(
                ["bash", str(REPO_ROOT / "scripts" / "build.sh"), str(out)],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            self.assertTrue(out.exists())

            run = subprocess.run([str(out), "--help"], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("usage: pr-outcomes", run.stdout)
