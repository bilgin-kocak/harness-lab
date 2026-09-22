"""SQLite database setup (SQLAlchemy 2.x, synchronous sessions)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from harnesslab.storage.models import Base


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        kwargs: dict[str, object] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url or url.endswith("sqlite://"):
                kwargs["poolclass"] = StaticPool
            else:
                path = url.replace("sqlite:///", "", 1)
                if path and path != ":memory:":
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def _set_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.execute("PRAGMA busy_timeout=5000")
                    if ":memory:" not in url:
                        cursor.execute("PRAGMA journal_mode=WAL")
                        cursor.execute("PRAGMA synchronous=NORMAL")
                finally:
                    cursor.close()

        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Forward-only schema migration: add columns that newer versions introduced.

        SQLite cannot add constraints in ``ALTER TABLE``, so only nullable/defaulted
        columns are ever added this way (all additions so far qualify).
        """
        inspector = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                existing = {col["name"] for col in inspector.get_columns(table.name)}
                for column in table.columns:
                    if column.name in existing:
                        continue
                    col_type = column.type.compile(dialect=self.engine.dialect)
                    conn.execute(
                        text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}')
                    )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
