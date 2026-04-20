from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sqlite3

from draft_store import DraftStore
from report_generator import Issue


class DraftStoreTest(unittest.TestCase):
    def test_create_save_and_load_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")

            session = store.create_report(chat_id=123)
            session.data["date"] = "16/04/2026"
            session.data["project_name"] = "Projek Demo"
            session.data["report_action"] = "Tindakan susulan dibuat."
            session.data["report_conclusion"] = "Kerja selesai."
            session.stage = "review"
            session.issues = [Issue(description="Kabel belum dirapikan", images_description="Foto susulan", image_paths=[])]
            store.save_session(session)

            loaded = store.load_report(chat_id=123, report_id=session.draft_id or 0)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.data["project_name"], "Projek Demo")
            self.assertEqual(loaded.stage, "review")
            self.assertEqual(loaded.issues[0].description, "Kabel belum dirapikan")
            self.assertEqual(loaded.issues[0].images_description, "Foto susulan")
            self.assertEqual(loaded.data["report_action"], "Tindakan susulan dibuat.")
            self.assertEqual(loaded.data["report_conclusion"], "Kerja selesai.")
            self.assertEqual(store.list_report_assets(session.draft_id or 0), [])

    def test_list_reports_and_revision_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")

            session = store.create_report(chat_id=123)
            session.data["project_name"] = "Projek Demo"
            session.data["project_sub_name"] = "Fasa 1"
            session.data["date"] = "16/04/2026"
            store.save_session(session)

            reports = store.list_reports(chat_id=123)
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].project_name, "Projek Demo")
            self.assertEqual(reports[0].current_revision, 0)

            revision_number = store.record_revision(
                draft_id=session.draft_id or 0,
                payload_json="{}",
                remote_path="InitialReports/report.docx",
                share_id="55",
                share_url="https://cloud.example.com/s/demo",
            )
            self.assertEqual(revision_number, 1)

            revisions = store.list_report_revisions(session.draft_id or 0)
            self.assertEqual(len(revisions), 1)
            self.assertEqual(revisions[0].revision_number, 1)
            self.assertEqual(revisions[0].remote_path, "InitialReports/report.docx")

            reports = store.list_reports(chat_id=123)
            self.assertEqual(reports[0].current_revision, 1)

            expired = store.list_expired_generated_files("9999-01-01T00:00:00+00:00")
            self.assertEqual(len(expired), 1)
            self.assertEqual(expired[0].revision_number, 1)

    def test_report_assets_are_tracked_and_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")

            session = store.create_report(chat_id=123)
            image1 = session.workspace / "issue-1-1.jpg"
            image2 = session.workspace / "issue-2-1.jpg"
            image1.parent.mkdir(parents=True, exist_ok=True)
            image1.write_text("x", encoding="utf-8")
            image2.write_text("y", encoding="utf-8")
            session.issues = [
                Issue(description="Isu 1", images_description="", image_paths=[image1]),
                Issue(description="Isu 2", images_description="", image_paths=[image2]),
            ]
            store.save_session(session)

            assets = store.list_report_assets(session.draft_id or 0)
            self.assertEqual(len(assets), 2)
            self.assertEqual({asset.local_path for asset in assets}, {str(image1), str(image2)})

            store.archive_report(chat_id=123, report_id=session.draft_id or 0)
            store.cleanup_report_assets(session.draft_id or 0, str(session.workspace))

            self.assertFalse(image1.exists())
            self.assertFalse(image2.exists())
            self.assertEqual(store.list_report_assets(session.draft_id or 0), [])

    def test_archive_and_delete_remove_from_active_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")

            report1 = store.create_report(chat_id=123)
            report1.data["project_name"] = "Report A"
            store.save_session(report1)

            report2 = store.create_report(chat_id=123)
            report2.data["project_name"] = "Report B"
            store.save_session(report2)

            store.archive_report(chat_id=123, report_id=report1.draft_id or 0)
            store.delete_report(chat_id=123, report_id=report2.draft_id or 0)

            reports = store.list_reports(chat_id=123)
            self.assertEqual(reports, [])
            archived = store.list_archived_reports(chat_id=123)
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].project_name, "Report A")

    def test_restore_report_moves_it_back_to_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")

            report = store.create_report(chat_id=123)
            report.data["project_name"] = "Report A"
            store.save_session(report)
            store.archive_report(chat_id=123, report_id=report.draft_id or 0)
            store.restore_report(chat_id=123, report_id=report.draft_id or 0)

            active = store.list_reports(chat_id=123)
            archived = store.list_archived_reports(chat_id=123)
            self.assertEqual(len(active), 1)
            self.assertEqual(len(archived), 0)

    def test_save_session_preserves_archived_status_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")

            report = store.create_report(chat_id=123)
            report.data["project_name"] = "Report A"
            store.save_session(report)
            store.archive_report(chat_id=123, report_id=report.draft_id or 0)

            archived_session = store.load_report_with_status(chat_id=123, report_id=report.draft_id or 0, statuses=("archived",))
            self.assertIsNotNone(archived_session)
            assert archived_session is not None
            archived_session.review_message_id = 99
            store.save_session(archived_session)

            active = store.list_reports(chat_id=123)
            archived = store.list_archived_reports(chat_id=123)
            self.assertEqual(active, [])
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].project_name, "Report A")

    def test_auto_archive_stale_reports_only_targets_generated_active_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")

            stale_generated = store.create_report(chat_id=123)
            stale_generated.data["project_name"] = "Generated"
            store.save_session(stale_generated)
            store.record_revision(
                draft_id=stale_generated.draft_id or 0,
                payload_json="{}",
                remote_path="InitialReports/generated.pdf",
                share_id="1",
                share_url="https://cloud.example.com/s/generated",
            )

            stale_unfinished = store.create_report(chat_id=123)
            stale_unfinished.data["project_name"] = "Unfinished"
            store.save_session(stale_unfinished)

            with store._connection() as connection:
                connection.execute(
                    "UPDATE drafts SET updated_at = ? WHERE id IN (?, ?)",
                    ("2026-04-01T00:00:00+00:00", stale_generated.draft_id, stale_unfinished.draft_id),
                )

            archived_ids = store.auto_archive_stale_reports("2026-04-10T00:00:00+00:00")

            self.assertEqual(archived_ids, [stale_generated.draft_id])
            self.assertEqual(store.list_reports(chat_id=123)[0].project_name, "Unfinished")
            self.assertEqual(store.list_archived_reports(chat_id=123)[0].project_name, "Generated")

    def test_migration_table_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "bot.db"
            DraftStore(db_path=db_path, drafts_dir=root / "drafts", backup_dir=root / "backups")

            connection = sqlite3.connect(db_path)
            try:
                rows = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            finally:
                connection.close()

            self.assertEqual([row[0] for row in rows], ["001_init", "002_reports_and_revisions", "003_report_assets"])

    def test_existing_database_is_backed_up_before_pending_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "bot.db"
            backup_dir = root / "backups"

            connection = sqlite3.connect(db_path)
            try:
                connection.executescript(
                    """
                    CREATE TABLE drafts (
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

                    CREATE TABLE generated_files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        draft_id INTEGER NOT NULL,
                        remote_path TEXT NOT NULL,
                        share_id TEXT,
                        share_url TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        deleted_at TEXT,
                        FOREIGN KEY(draft_id) REFERENCES drafts(id)
                    );

                    CREATE TABLE schema_migrations (
                        version TEXT PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES ('001_init', '2026-04-18T00:00:00+00:00')"
                )
                connection.commit()
            finally:
                connection.close()

            store = DraftStore(db_path=db_path, drafts_dir=root / "drafts", backup_dir=backup_dir)
            backups = list(backup_dir.glob("bot-*.sqlite3"))

            self.assertEqual(len(backups), 1)
            connection = sqlite3.connect(store.db_path)
            try:
                versions = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            finally:
                connection.close()
            self.assertEqual([row[0] for row in versions], ["001_init", "002_reports_and_revisions", "003_report_assets"])


if __name__ == "__main__":
    unittest.main()
