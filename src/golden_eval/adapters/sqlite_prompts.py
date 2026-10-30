"""Immutable prompt versions plus an active pointer.

Saving never overwrites: every save inserts a new row keyed by `(prompt_kind, version_label)`. The
built-in prompt ships with the repository and is not stored here; it is resolved at read time by the
prompt registry, so an edited version can always be abandoned by clearing the active pointer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from ..domain.models import (
    PromptKind,
    PromptSource,
    PromptVersion,
    PromptVersionSummary,
)
from .errors import InvalidTransitionError, NotFoundError


class SqlitePromptStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def list_versions(self, prompt_kind: PromptKind) -> list[PromptVersionSummary]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM prompt_versions
                WHERE prompt_kind = ?
                ORDER BY created_at DESC, version_label
                """,
                (prompt_kind.value,),
            ).fetchall()
            active = self._active_label(prompt_kind)
        return [_summary_from_row(row, active) for row in rows]

    def get_version(self, prompt_kind: PromptKind, version_label: str) -> PromptVersion:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM prompt_versions
                WHERE prompt_kind = ? AND version_label = ?
                """,
                (prompt_kind.value, version_label),
            ).fetchone()
            active = self._active_label(prompt_kind)
        if row is None:
            raise NotFoundError(version_label)
        return _version_from_row(row, active)

    def save_version(
        self,
        prompt_kind: PromptKind,
        *,
        version_label: str,
        content: str,
        content_sha256: str,
        notes: str | None,
        activate: bool,
    ) -> PromptVersion:
        now = datetime.now(UTC)
        with self._transaction() as cursor:
            existing = cursor.execute(
                """
                SELECT version_label FROM prompt_versions
                WHERE prompt_kind = ? AND version_label = ?
                """,
                (prompt_kind.value, version_label),
            ).fetchone()
            if existing is not None:
                raise InvalidTransitionError(
                    f"prompt version {version_label} already exists and is immutable"
                )
            cursor.execute(
                """
                INSERT INTO prompt_versions(
                    prompt_kind, version_label, content, content_sha256, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (prompt_kind.value, version_label, content, content_sha256, notes, now.isoformat()),
            )
            if activate:
                self._set_active(cursor, prompt_kind, version_label, now)
            row = cursor.execute(
                """
                SELECT * FROM prompt_versions
                WHERE prompt_kind = ? AND version_label = ?
                """,
                (prompt_kind.value, version_label),
            ).fetchone()
            active = self._active_label(prompt_kind, cursor=cursor)
        return _version_from_row(row, active)

    def activate(self, prompt_kind: PromptKind, version_label: str) -> PromptVersion:
        with self._transaction() as cursor:
            row = cursor.execute(
                """
                SELECT * FROM prompt_versions
                WHERE prompt_kind = ? AND version_label = ?
                """,
                (prompt_kind.value, version_label),
            ).fetchone()
            if row is None:
                raise NotFoundError(version_label)
            self._set_active(cursor, prompt_kind, version_label, datetime.now(UTC))
        return _version_from_row(row, version_label)

    def active_edited_version(self, prompt_kind: PromptKind) -> PromptVersion | None:
        with self._lock:
            active = self._active_label(prompt_kind)
            if active is None:
                return None
            row = self._connection.execute(
                """
                SELECT * FROM prompt_versions
                WHERE prompt_kind = ? AND version_label = ?
                """,
                (prompt_kind.value, active),
            ).fetchone()
        if row is None:
            return None
        return _version_from_row(row, active)

    def clear_active(self, prompt_kind: PromptKind) -> None:
        """Drop the active pointer so resolution falls back to the built-in baseline."""

        with self._transaction() as cursor:
            cursor.execute(
                "DELETE FROM prompt_active WHERE prompt_kind = ?", (prompt_kind.value,)
            )

    def active_version_label(self, prompt_kind: PromptKind) -> str | None:
        with self._lock:
            return self._active_label(prompt_kind)

    def next_version_label(self, prompt_kind: PromptKind) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM prompt_versions WHERE prompt_kind = ?",
                (prompt_kind.value,),
            ).fetchone()
        return f"edit-{int(row['total']) + 1}"

    def _set_active(
        self,
        cursor: sqlite3.Cursor,
        prompt_kind: PromptKind,
        version_label: str,
        moment: datetime,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO prompt_active(prompt_kind, version_label, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(prompt_kind) DO UPDATE SET
                version_label = excluded.version_label,
                updated_at = excluded.updated_at
            """,
            (prompt_kind.value, version_label, moment.isoformat()),
        )

    def _active_label(
        self,
        prompt_kind: PromptKind,
        *,
        cursor: sqlite3.Connection | sqlite3.Cursor | None = None,
    ) -> str | None:
        target = cursor or self._connection
        row = target.execute(
            "SELECT version_label FROM prompt_active WHERE prompt_kind = ?",
            (prompt_kind.value,),
        ).fetchone()
        return None if row is None else str(row["version_label"])

    def _migrate(self) -> None:
        with self._transaction() as cursor:
            cursor.executescript(
                """
                CREATE TABLE IF NOT EXISTS prompt_versions (
                    prompt_kind TEXT NOT NULL,
                    version_label TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(prompt_kind, version_label)
                );

                CREATE TABLE IF NOT EXISTS prompt_active (
                    prompt_kind TEXT PRIMARY KEY,
                    version_label TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                yield cursor
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
            finally:
                cursor.close()


def _summary_from_row(row: sqlite3.Row, active: str | None) -> PromptVersionSummary:
    return PromptVersionSummary(
        prompt_kind=PromptKind(row["prompt_kind"]),
        version_label=row["version_label"],
        source=PromptSource.EDITED,
        content_sha256=row["content_sha256"],
        notes=row["notes"],
        is_active=row["version_label"] == active,
        char_count=len(row["content"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _version_from_row(row: sqlite3.Row, active: str | None) -> PromptVersion:
    summary = _summary_from_row(row, active)
    return PromptVersion(**summary.model_dump(), content=row["content"])
