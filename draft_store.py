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
    current_revision: int


@dataclass(slots=True)
class GeneratedFileRecord:
    record_id: int
    draft_id: int
    revision_number: int
    remote_path: str
    share_id: str | None
    share_url: str
    created_at: str
    status: str


@dataclass(slots=True)
class CleanupTarget:
    draft_id: int
    workspace: str
    status: str
    effective_at: str


@dataclass(slots=True)
class ReportAsset:
    report_id: int
    issue_index: int
    asset_kind: str
    local_path: str
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
    (
        "002_reports_and_revisions",
        """
        ALTER TABLE drafts ADD COLUMN current_revision INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE drafts ADD COLUMN archived_at TEXT;
        ALTER TABLE drafts ADD COLUMN deleted_at TEXT;

        ALTER TABLE generated_files ADD COLUMN revision_number INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE generated_files ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE generated_files ADD COLUMN status TEXT NOT NULL DEFAULT 'available';
        ALTER TABLE generated_files ADD COLUMN expired_at TEXT;

        UPDATE drafts SET status = 'active' WHERE status IN ('draft', 'generated');
        UPDATE drafts SET status = 'deleted', deleted_at = COALESCE(deleted_at, updated_at) WHERE status = 'cancelled';

        UPDATE generated_files
        SET revision_number = (
            SELECT seq
            FROM (
                SELECT gf2.id AS id,
                       ROW_NUMBER() OVER (
                           PARTITION BY gf2.draft_id
                           ORDER BY gf2.created_at, gf2.id
                       ) AS seq
                FROM generated_files gf2
            ) ranked
            WHERE ranked.id = generated_files.id
        )
        WHERE revision_number = 0;

        UPDATE generated_files
        SET status = CASE
            WHEN deleted_at IS NOT NULL THEN 'expired'
            ELSE 'available'
        END
        WHERE status = 'available';

        UPDATE generated_files
        SET expired_at = deleted_at
        WHERE deleted_at IS NOT NULL AND expired_at IS NULL;

        UPDATE drafts
        SET current_revision = (
            SELECT COALESCE(MAX(gf.revision_number), 0)
            FROM generated_files gf
            WHERE gf.draft_id = drafts.id
        );
        """,
    ),
    (
        "003_report_assets",
        """
        CREATE TABLE IF NOT EXISTS report_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            issue_index INTEGER NOT NULL,
            asset_kind TEXT NOT NULL,
            local_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT,
            FOREIGN KEY(report_id) REFERENCES drafts(id)
        );

        CREATE INDEX IF NOT EXISTS idx_report_assets_report_id
        ON report_assets(report_id);
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
        return self.create_report(chat_id)

    def create_report(self, chat_id: int) -> Session:
        now = _now_iso()
        state = {
            "field_index": 0,
            "data": {},
            "issues": [],
            "current_issue": {"description": "", "images_description": "", "image_paths": []},
            "stage": "field",
            "edit_field_key": None,
            "edit_issue_index": None,
            "delete_issue_index": None,
            "review_message_id": None,
            "workspace": "",
        }
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO drafts (
                    chat_id, status, date, project_name, project_sub_name,
                    state_json, created_at, updated_at, current_revision
                ) VALUES (?, 'active', '', '', '', ?, ?, ?, 0)
                """,
                (chat_id, json.dumps(state), now, now),
            )
            report_id = int(cursor.lastrowid)

        workspace = self._workspace_for(report_id)
        workspace.mkdir(parents=True, exist_ok=True)
        session = Session(chat_id=chat_id, draft_id=report_id, workspace=workspace)
        self.save_session(session)
        return session

    def save_session(self, session: Session, status: str | None = None) -> None:
        if session.draft_id is None:
            raise ValueError("Cannot save a session without draft_id.")

        session.workspace.mkdir(parents=True, exist_ok=True)
        now = _now_iso()
        data = session.data
        effective_status = status or session.report_status
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
                    effective_status,
                    data.get("date", ""),
                    data.get("project_name", ""),
                    data.get("project_sub_name", ""),
                    json.dumps(self._serialize_session(session)),
                    now,
                    session.draft_id,
                    session.chat_id,
                ),
            )
            self._replace_report_assets(connection, session)
        session.report_status = effective_status

    def load_session(self, chat_id: int, draft_id: int) -> Session | None:
        return self.load_report(chat_id, draft_id)

    def load_report(self, chat_id: int, report_id: int) -> Session | None:
        return self.load_report_with_status(chat_id, report_id, statuses=("active",))

    def load_report_with_status(self, chat_id: int, report_id: int, statuses: tuple[str, ...]) -> Session | None:
        placeholders = ", ".join("?" for _ in statuses)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT id, chat_id, status, state_json
                FROM drafts
                WHERE id = ? AND chat_id = ? AND status IN ({placeholders})
                """,
                (report_id, chat_id, *statuses),
            ).fetchone()

        if row is None:
            return None
        state = json.loads(row["state_json"])
        return self._deserialize_session(chat_id=chat_id, draft_id=report_id, state=state, report_status=row["status"])

    def list_drafts(self, chat_id: int, limit: int = 10) -> list[DraftSummary]:
        return self.list_reports(chat_id, limit=limit)

    def list_reports(self, chat_id: int, limit: int = 10) -> list[DraftSummary]:
        return self._list_reports_by_status(chat_id, "active", limit)

    def list_archived_reports(self, chat_id: int, limit: int = 10) -> list[DraftSummary]:
        return self._list_reports_by_status(chat_id, "archived", limit)

    def _list_reports_by_status(self, chat_id: int, status: str, limit: int) -> list[DraftSummary]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id, date, project_name, project_sub_name, updated_at, created_at, current_revision
                FROM drafts
                WHERE chat_id = ? AND status = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (chat_id, status, limit),
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
                current_revision=int(row["current_revision"]),
            )
            for row in rows
        ]

    def cancel_draft(self, chat_id: int, draft_id: int) -> None:
        self.delete_report(chat_id, draft_id)

    def delete_report(self, chat_id: int, report_id: int) -> None:
        now = _now_iso()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE drafts
                SET status = 'deleted', deleted_at = ?, updated_at = ?
                WHERE id = ? AND chat_id = ?
                """,
                (now, now, report_id, chat_id),
            )

    def archive_report(self, chat_id: int, report_id: int) -> None:
        now = _now_iso()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE drafts
                SET status = 'archived', archived_at = ?, updated_at = ?
                WHERE id = ? AND chat_id = ?
                """,
                (now, now, report_id, chat_id),
            )

    def restore_report(self, chat_id: int, report_id: int) -> None:
        now = _now_iso()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE drafts
                SET status = 'active', archived_at = NULL, updated_at = ?
                WHERE id = ? AND chat_id = ?
                """,
                (now, report_id, chat_id),
            )

    def auto_archive_stale_reports(self, cutoff_iso: str) -> list[int]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM drafts
                WHERE status = 'active'
                  AND current_revision > 0
                  AND updated_at < ?
                """,
                (cutoff_iso,),
            ).fetchall()
            report_ids = [int(row["id"]) for row in rows]
            if not report_ids:
                return []

            now = _now_iso()
            connection.executemany(
                """
                UPDATE drafts
                SET status = 'archived', archived_at = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                [(now, now, report_id) for report_id in report_ids],
            )

        return report_ids

    def mark_generated(self, chat_id: int, draft_id: int) -> None:
        # compatibility no-op: reports remain active after generation
        return

    def record_generated_file(
        self,
        draft_id: int,
        remote_path: str,
        share_id: str | None,
        share_url: str,
    ) -> None:
        self.record_revision(draft_id=draft_id, payload_json="{}", remote_path=remote_path, share_id=share_id, share_url=share_url)

    def record_revision(
        self,
        draft_id: int,
        payload_json: str,
        remote_path: str,
        share_id: str | None,
        share_url: str,
    ) -> int:
        now = _now_iso()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT current_revision FROM drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Report {draft_id} not found.")
            revision_number = int(row["current_revision"]) + 1
            connection.execute(
                """
                INSERT INTO generated_files (
                    draft_id, revision_number, payload_json, remote_path, share_id, share_url, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'available')
                """,
                (draft_id, revision_number, payload_json, remote_path, share_id, share_url, now),
            )
            connection.execute(
                """
                UPDATE drafts
                SET current_revision = ?, updated_at = ?, status = 'active'
                WHERE id = ?
                """,
                (revision_number, now, draft_id),
            )
        return revision_number

    def list_report_revisions(self, draft_id: int, limit: int = 10) -> list[GeneratedFileRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, draft_id, revision_number, remote_path, share_id, share_url, created_at, status
                FROM generated_files
                WHERE draft_id = ?
                ORDER BY revision_number DESC
                LIMIT ?
                """,
                (draft_id, limit),
            ).fetchall()

        return [
            GeneratedFileRecord(
                record_id=int(row["id"]),
                draft_id=int(row["draft_id"]),
                revision_number=int(row["revision_number"]),
                remote_path=row["remote_path"],
                share_id=row["share_id"],
                share_url=row["share_url"],
                created_at=row["created_at"],
                status=row["status"],
            )
            for row in rows
        ]

    def list_expired_generated_files(self, cutoff_iso: str) -> list[GeneratedFileRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, draft_id, revision_number, remote_path, share_id, share_url, created_at, status
                FROM generated_files
                WHERE status = 'available' AND created_at < ?
                ORDER BY created_at ASC
                """,
                (cutoff_iso,),
            ).fetchall()

        return [
            GeneratedFileRecord(
                record_id=int(row["id"]),
                draft_id=int(row["draft_id"]),
                revision_number=int(row["revision_number"]),
                remote_path=row["remote_path"],
                share_id=row["share_id"],
                share_url=row["share_url"],
                created_at=row["created_at"],
                status=row["status"],
            )
            for row in rows
        ]

    def mark_generated_file_deleted(self, record_id: int) -> None:
        now = _now_iso()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE generated_files
                SET status = 'expired', expired_at = ?, deleted_at = ?
                WHERE id = ?
                """,
                (now, now, record_id),
            )

    def list_reports_for_asset_cleanup(self, cutoff_iso: str) -> list[CleanupTarget]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, status, archived_at, deleted_at, state_json
                FROM drafts
                WHERE status IN ('archived', 'deleted')
                  AND COALESCE(deleted_at, archived_at, updated_at) < ?
                """,
                (cutoff_iso,),
            ).fetchall()

        targets: list[CleanupTarget] = []
        for row in rows:
            state = json.loads(row["state_json"])
            workspace = str(state.get("workspace", ""))
            effective_at = row["deleted_at"] or row["archived_at"] or ""
            targets.append(
                CleanupTarget(
                    draft_id=int(row["id"]),
                    workspace=workspace,
                    status=row["status"],
                    effective_at=effective_at,
                )
            )
        return targets

    def cleanup_report_assets(self, report_id: int, workspace: str) -> None:
        with self._connection() as connection:
            assets = connection.execute(
                """
                SELECT local_path
                FROM report_assets
                WHERE report_id = ? AND deleted_at IS NULL
                """,
                (report_id,),
            ).fetchall()
            for row in assets:
                path = Path(row["local_path"])
                try:
                    if path.exists():
                        path.unlink()
                except IsADirectoryError:
                    shutil.rmtree(path, ignore_errors=True)
                except Exception:
                    pass
            connection.execute(
                "UPDATE report_assets SET deleted_at = ? WHERE report_id = ? AND deleted_at IS NULL",
                (_now_iso(), report_id),
            )
            row = connection.execute("SELECT state_json FROM drafts WHERE id = ?", (report_id,)).fetchone()
            if row is None:
                return
            state = json.loads(row["state_json"])
            state["workspace"] = ""
            state["current_issue"] = {"description": "", "images_description": "", "image_paths": []}
            state["issues"] = [
                {
                    "description": issue.get("description", ""),
                    "images_description": issue.get("images_description", ""),
                    "image_paths": [],
                }
                for issue in state.get("issues", [])
            ]
            connection.execute(
                "UPDATE drafts SET state_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(state), _now_iso(), report_id),
            )

        if workspace:
            path = Path(workspace)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)

    def list_report_assets(self, report_id: int) -> list[ReportAsset]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT report_id, issue_index, asset_kind, local_path, created_at
                FROM report_assets
                WHERE report_id = ? AND deleted_at IS NULL
                ORDER BY issue_index, local_path
                """,
                (report_id,),
            ).fetchall()
        return [
            ReportAsset(
                report_id=int(row["report_id"]),
                issue_index=int(row["issue_index"]),
                asset_kind=row["asset_kind"],
                local_path=row["local_path"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _serialize_session(self, session: Session) -> dict:
        return {
            "field_index": session.field_index,
            "data": session.data,
            "issues": [
                {
                    "description": issue.description,
                    "images_description": issue.images_description,
                    "image_paths": [str(path) for path in issue.image_paths],
                }
                for issue in session.issues
            ],
            "current_issue": {
                "description": session.current_issue.description,
                "images_description": session.current_issue.images_description,
                "image_paths": [str(path) for path in session.current_issue.image_paths],
            },
            "stage": session.stage,
            "edit_field_key": session.edit_field_key,
            "edit_issue_index": session.edit_issue_index,
            "delete_issue_index": session.delete_issue_index,
            "review_message_id": session.review_message_id,
            "list_message_id": session.list_message_id,
            "workspace": str(session.workspace),
        }

    def _replace_report_assets(self, connection: sqlite3.Connection, session: Session) -> None:
        if session.draft_id is None:
            return

        connection.execute("DELETE FROM report_assets WHERE report_id = ?", (session.draft_id,))
        now = _now_iso()
        asset_rows: list[tuple[int, int, str, str, str]] = []
        for issue_index, issue in enumerate(session.issues):
            for path in issue.image_paths:
                asset_rows.append((session.draft_id, issue_index, "issue_image", str(path), now))
        for path in session.current_issue.image_paths:
            asset_rows.append((session.draft_id, -1, "issue_image", str(path), now))

        if asset_rows:
            connection.executemany(
                """
                INSERT INTO report_assets (report_id, issue_index, asset_kind, local_path, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                asset_rows,
            )

    def _deserialize_session(self, chat_id: int, draft_id: int, state: dict, report_status: str = "active") -> Session:
        workspace_raw = state.get("workspace") or str(self._workspace_for(draft_id))
        workspace = Path(workspace_raw)
        workspace.mkdir(parents=True, exist_ok=True)
        current_issue = state.get("current_issue", {})
        return Session(
            chat_id=chat_id,
            draft_id=draft_id,
            report_status=report_status,
            field_index=int(state.get("field_index", 0)),
            data=dict(state.get("data", {})),
            issues=[
                Issue(
                    description=item["description"],
                    images_description=item.get("images_description", ""),
                    image_paths=[Path(path) for path in item.get("image_paths", [])],
                )
                for item in state.get("issues", [])
            ],
            current_issue=PendingIssue(
                description=current_issue.get("description", ""),
                images_description=current_issue.get("images_description", ""),
                image_paths=[Path(path) for path in current_issue.get("image_paths", [])],
            ),
            stage=state.get("stage", "field"),
            edit_field_key=state.get("edit_field_key"),
            edit_issue_index=state.get("edit_issue_index"),
            delete_issue_index=state.get("delete_issue_index"),
            review_message_id=state.get("review_message_id"),
            list_message_id=state.get("list_message_id"),
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
        return [(version, sql) for version, sql in SCHEMA_MIGRATIONS if version not in applied_versions]

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
