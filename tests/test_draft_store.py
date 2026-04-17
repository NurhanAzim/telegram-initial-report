from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sqlite3

from draft_store import DraftStore
from report_generator import Issue


class DraftStoreTest(unittest.TestCase):
    def test_create_save_and_load_draft(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")

            session = store.create_draft(chat_id=123)
            session.data["date"] = "16/04/2026"
            session.data["project_name"] = "Projek Demo"
            session.stage = "review"
            session.issues = [Issue(description="Kabel belum dirapikan", image_paths=[])]
            store.save_session(session)

            loaded = store.load_session(chat_id=123, draft_id=session.draft_id or 0)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.data["project_name"], "Projek Demo")
            self.assertEqual(loaded.stage, "review")
            self.assertEqual(loaded.issues[0].description, "Kabel belum dirapikan")

    def test_list_drafts_and_generated_file_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")

            session = store.create_draft(chat_id=123)
            session.data["project_name"] = "Projek Demo"
            session.data["project_sub_name"] = "Fasa 1"
            session.data["date"] = "16/04/2026"
            store.save_session(session)

            drafts = store.list_drafts(chat_id=123)
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0].project_name, "Projek Demo")

            store.record_generated_file(
                draft_id=session.draft_id or 0,
                remote_path="InitialReports/report.docx",
                share_id="55",
                share_url="https://cloud.example.com/s/demo",
            )
            expired = store.list_expired_generated_files("9999-01-01T00:00:00+00:00")
            self.assertEqual(len(expired), 1)
            self.assertEqual(expired[0].remote_path, "InitialReports/report.docx")

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

            self.assertEqual([row[0] for row in rows], ["001_init"])

    def test_existing_database_is_backed_up_before_pending_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "bot.db"
            backup_dir = root / "backups"

            connection = sqlite3.connect(db_path)
            try:
                connection.execute("CREATE TABLE drafts (id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()

            store = DraftStore(db_path=db_path, drafts_dir=root / "drafts", backup_dir=backup_dir)
            backups = list(backup_dir.glob("bot-*.sqlite3"))

            self.assertEqual(len(backups), 1)
            connection = sqlite3.connect(store.db_path)
            try:
                versions = connection.execute("SELECT version FROM schema_migrations").fetchall()
            finally:
                connection.close()
            self.assertEqual([row[0] for row in versions], ["001_init"])


if __name__ == "__main__":
    unittest.main()
