"""Runs pyright over the repo so `python3 -m unittest discover -s tests` is
the single gate for both tests and static types."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestTypes(unittest.TestCase):
    def test_pyright_clean(self) -> None:
        if shutil.which("uvx") is None:
            self.skipTest("uvx not on PATH; can't run pyright")
        proc = subprocess.run(
            ["uvx", "pyright"], cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            self.fail(f"pyright found issues:\n{proc.stdout}\n{proc.stderr}")
