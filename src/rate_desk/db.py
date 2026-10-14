"""SQLite access: connections, transactions and schema setup.

Every unit of work opens its own short-lived connection. SQLite connections
are cheap (a file open), and it means no connection is ever shared between
threads, which FastAPI's threadpool would otherwise make easy to get wrong.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = 1


def to_db_time(moment: datetime) -> str:
    """Fixed-width UTC text, so that comparing strings compares times."""
    if moment.tzinfo is None:
        raise ValueError("refusing a naive datetime; all times are UTC-aware")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def from_db_time(text: str) -> datetime:
    return datetime.fromisoformat(text)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        # autocommit=True: the driver never opens transactions behind our back.
        # Every transaction in this codebase is an explicit BEGIN ... COMMIT.
        conn = sqlite3.connect(self.path, autocommit=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # FULL: a commit is on disk before we say "approved". Rate changes are
        # rare enough that the extra fsync costs nothing that matters.
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction holding SQLite's single write lock from the first statement.

        BEGIN IMMEDIATE takes the lock up front, so two approvals of the same
        request run one after the other, never interleaved. The second sees
        the first one's result.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A read transaction: every query inside sees the same committed snapshot."""
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def migrate(self) -> None:
        """Create the schema if this is a new database. Safe to call on every start."""
        conn = self.connect()
        try:
            # WAL lets readers (the dashboard stream) carry on while a write is
            # in progress. It is a property of the file, so setting it once sticks.
            conn.execute("PRAGMA journal_mode = WAL")
            # Read the version under the write lock, so two processes starting on a
            # new file cannot both decide to create the schema.
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != 0 and version != SCHEMA_VERSION:
                    raise RuntimeError(
                        f"database {self.path} is at schema version {version}; "
                        f"this build knows version {SCHEMA_VERSION}"
                    )
                if version == 0:
                    schema = files("rate_desk").joinpath("schema.sql").read_text(encoding="utf-8")
                    for statement in _split_sql(schema):
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()


def _split_sql(script: str) -> Iterator[str]:
    """Split a schema script into statements, keeping trigger bodies whole.

    ``executescript`` would do this, but it commits first, so it cannot run
    inside our transaction. A schema half-created is worse than none.
    """
    buffer: list[str] = []
    for line in script.splitlines():
        if not buffer and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buffer.append(line)
        candidate = "\n".join(buffer)
        if sqlite3.complete_statement(candidate):
            yield candidate
            buffer = []
    if any(part.strip() for part in buffer):
        raise ValueError("schema.sql ends with an incomplete statement")
