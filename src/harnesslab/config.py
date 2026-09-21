"""Harness Lab runtime settings.

Everything Harness Lab writes lives under one *home* directory (default
``./.harnesslab``) so a benchmark checkout stays clean and the whole state can
be deleted with a single ``rm -rf``.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_HOME = ".harnesslab"


class Settings(BaseModel):
    home: Path = Field(default_factory=lambda: Path.cwd() / DEFAULT_HOME)
    database_url: str | None = None

    @classmethod
    def from_env(cls, home: str | os.PathLike[str] | None = None) -> Settings:
        raw_home = home or os.environ.get("HARNESSLAB_HOME") or DEFAULT_HOME
        settings = cls(home=Path(raw_home).expanduser().resolve())
        db_url = os.environ.get("HARNESSLAB_DB_URL")
        if db_url:
            settings.database_url = db_url
        return settings

    # -- derived paths -----------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.home / "harnesslab.db"

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{self.db_path}"

    @property
    def worktrees_dir(self) -> Path:
        return self.home / "worktrees"

    @property
    def artifacts_dir(self) -> Path:
        return self.home / "artifacts"

    @property
    def repos_dir(self) -> Path:
        return self.home / "repos"

    @property
    def fixtures_dir(self) -> Path:
        return self.home / "fixtures"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    def ensure_dirs(self) -> None:
        for path in (
            self.home,
            self.worktrees_dir,
            self.artifacts_dir,
            self.repos_dir,
            self.fixtures_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
