from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot_state import PendingIssue, Session
from report_generator import Issue


@dataclass(slots=True)
class DraftSummary:
    draft_id: int
    chat_id: int
    date: str
    project_name: str
    project_sub_name: str
    updated_at: str
    created_at: str


@dataclass(slots=True)
class GeneratedFileRecord:
    record_id: int
    draft_id: int
    remote_path: str
    share_id: str | None
    share_url: str
    created_at: str


SCHEMA_MIGRATIONS: list[tuple[str, str]] = [
    (
        "001_init",
        """
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            date TEXT NOT NULL DEFAULT '',
            project_name TEXT NOT NULL DEFAULT '',
            project_sub_name TEXT NOT NULL DEFAULT '',
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            generated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS generated_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL,
            remote_path TEXT NOT NULL,
            share_id TEXT,
            share_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT,
            FOREIGN KEY(draft_id) REFERENCES drafts(id)
        );
        """,
    ),
]


class DraftStore:
    def __init__(self, db_path: Path, drafts_dir: Path, backup_dir: Path | None = None) -> None:
        self.db_path = db_path
        self.drafts_dir = drafts_dir
        self.backup_dir = backup_dir or self.db_path.parent / "backups"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def create_draft(self, chat_id: int) -> Session:
        now = _now_iso()
        state = {
            "field_index": 0,
            "data": {},
            "issues": [],
            "current_issue": {"description": "", "image_paths": []},
            "stage": "field",
            "edit_field_key": None,
            "edit_issue_index": None,
            "review_message_id": None,
            "workspace": "",
        }
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO drafts (
                    chat_id, status, date, project_name, project_sub_name,
                    state_json, created_at, updated_at
                ) VALUES (?, 'draft', '', '', '', ?, ?, ?)
                """,
                (chat_id, json.dumps(state), now, now),
            )
            draft_id = int(cursor.lastrowid)

        workspace = self._workspace_for(draft_id)
        workspace.mkdir(parents=True, exist_ok=True)
        session = Session(chat_id=chat_id, draft_id=draft_id, workspace=workspace)
        self.save_session(session)
        return session

    def save_session(self, session: Session, status: str = "draft") -> None:
        if session.draft_id is None:
            raise ValueError("Cannot save a session without draft_id.")

        session.workspace.mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        data = session.data
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE drafts
                SET status = ?,
                    date = ?,
                    project_name = ?,
                    project_sub_name = ?,
                    state_json = ?,
                    updated_at = ?
                WHERE id = ? AND chat_id = ?
                """,
                (
                    status,
                    data.get("date", ""),
                    data.get("project_name", ""),
                    data.get("project_sub_name", ""),
                    json.dumps(self._serialize_session(session)),
                    now,
                    session.draft_id,
                    session.chat_id,
                ),
            )

    def load_session(self, chat_id: int, draft_id: int) -> Session | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, chat_id, state_json
                FROM drafts
                WHERE id = ? AND chat_id = ? AND status = 'draft'
                """,
                (draft_id, chat_id),
            ).fetchone()

        if row is None:
            return None
        state = json.loads(row["state_json"])
        return self._deserialize_session(chat_id=chat_id, draft_id=draft_id, state=state)

    def list_drafts(self, chat_id: int, limit: int = 10) -> list[DraftSummary]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id, date, project_name, project_sub_name, updated_at, created_at
                FROM drafts
                WHERE chat_id = ? AND status = 'draft'
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()

        return [
            DraftSummary(
                draft_id=int(row["id"]),
                chat_id=int(row["chat_id"]),
                date=row["date"],
                project_name=row["project_name"],
                project_sub_name=row["project_sub_name"],
                updated_at=row["updated_at"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def cancel_draft(self, chat_id: int, draft_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE drafts SET status = 'cancelled', updated_at = ? WHERE id = ? AND chat_id = ?",
                (_now_iso(), draft_id, chat_id),
            )

    def mark_generated(self, chat_id: int, draft_id: int) -> None:
        now = _now_iso()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE drafts
                SET status = 'generated', generated_at = ?, updated_at = ?
                WHERE id = ? AND chat_id = ?
                """,
                (now, now, draft_id, chat_id),
            )

    def record_generated_file(
        self,
        draft_id: int,
        remote_path: str,
        share_id: str | None,
        share_url: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO generated_files (
                    draft_id, remote_path, share_id, share_url, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (draft_id, remote_path, share_id, share_url, _now_iso()),
            )

    def list_expired_generated_files(self, cutoff_iso: str) -> list[GeneratedFileRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, draft_id, remote_path, share_id, share_url, created_at
                FROM generated_files
                WHERE deleted_at IS NULL AND created_at < ?
                ORDER BY created_at ASC
                """,
                (cutoff_iso,),
            ).fetchall()

        return [
            GeneratedFileRecord(
                record_id=int(row["id"]),
                draft_id=int(row["draft_id"]),
                remote_path=row["remote_path"],
                share_id=row["share_id"],
                share_url=row["share_url"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def mark_generated_file_deleted(self, record_id: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE generated_files SET deleted_at = ? WHERE id = ?",
                (_now_iso(), record_id),
            )

    def _serialize_session(self, session: Session) -> dict:
        return {
            "field_index": session.field_index,
            "data": session.data,
            "issues": [
                {
                    "description": issue.description,
                    "image_paths": [str(path) for path in issue.image_paths],
                }
                for issue in session.issues
            ],
            "current_issue": {
                "description": session.current_issue.description,
                "image_paths": [str(path) for path in session.current_issue.image_paths],
            },
            "stage": session.stage,
            "edit_field_key": session.edit_field_key,
            "edit_issue_index": session.edit_issue_index,
            "review_message_id": session.review_message_id,
            "workspace": str(session.workspace),
        }

    def _deserialize_session(self, chat_id: int, draft_id: int, state: dict) -> Session:
        workspace_raw = state.get("workspace") or str(self._workspace_for(draft_id))
        workspace = Path(workspace_raw)
        workspace.mkdir(parents=True, exist_ok=True)
        current_issue = state.get("current_issue", {})
        return Session(
            chat_id=chat_id,
            draft_id=draft_id,
            field_index=int(state.get("field_index", 0)),
            data=dict(state.get("data", {})),
            issues=[
                Issue(
                    description=item["description"],
                    image_paths=[Path(path) for path in item.get("image_paths", [])],
                )
                for item in state.get("issues", [])
            ],
            current_issue=PendingIssue(
                description=current_issue.get("description", ""),
                image_paths=[Path(path) for path in current_issue.get("image_paths", [])],
            ),
            stage=state.get("stage", "field"),
            edit_field_key=state.get("edit_field_key"),
            edit_issue_index=state.get("edit_issue_index"),
            review_message_id=state.get("review_message_id"),
            workspace=workspace,
        )

    def _workspace_for(self, draft_id: int) -> Path:
        return self.drafts_dir / f"draft-{draft_id}"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(self) -> sqlite3.Connection:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        pending = self._pending_migrations()
        if pending and self.db_path.exists() and self.db_path.stat().st_size > 0:
            self._backup_database()

        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            for version, sql in pending:
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, _now_iso()),
                )

    def _pending_migrations(self) -> list[tuple[str, str]]:
        applied_versions = self._applied_migration_versions()
        return [
            (version, sql)
            for version, sql in SCHEMA_MIGRATIONS
            if version not in applied_versions
        ]

    def _applied_migration_versions(self) -> set[str]:
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return set()

        with self._connection() as connection:
            table = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_migrations'
                """
            ).fetchone()
            if table is None:
                return set()

            rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
        return {row["version"] for row in rows}

    def _backup_database(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.backup_dir / f"{self.db_path.stem}-{timestamp}.sqlite3"
        shutil.copy2(self.db_path, backup_path)
        return backup_path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
