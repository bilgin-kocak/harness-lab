from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from harnesslab.config import Settings
from harnesslab.storage.database import Database

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEMO_SUITE = ROOT / "suites" / "demo" / "suite.yaml"
FAKE_CLI = Path(__file__).resolve().parent / "fake_clis" / "fake_cli.py"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("FAKE_CLI_") or name in ("HARNESSLAB_HOME", "HARNESSLAB_DB_URL"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(home=tmp_path / "home")
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings: Settings) -> Iterator[Database]:
    database = Database(settings.resolved_database_url)
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture
def fake_cli(tmp_path: Path) -> Path:
    """An executable wrapper around tests/fake_clis/fake_cli.py using the current interpreter."""
    wrapper = tmp_path / "bin" / "fake-cli"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


@pytest.fixture
def plain_fixture(tmp_path: Path) -> Path:
    """A tiny plain-directory fixture repository (no .git)."""
    src = tmp_path / "fixture"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("")
    (src / "pkg" / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (src / "tests").mkdir()
    (src / "tests" / "test_calc.py").write_text(
        "import unittest\nfrom pkg.calc import add\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n"
    )
    (src / "README.md").write_text("# fixture\n")
    return src
