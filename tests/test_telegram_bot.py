from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_state import Session
from report_generator import Issue, ReportData
from telegram_bot import (
    AUTHOR_OPTIONS,
    AUTHOR_BACK_LABEL,
    NO_LABEL,
    YES_LABEL,
    _count_total_images,
    _extract_image_file,
    _author_reply_keyboard,
    _archived_reports_keyboard,
    _archived_reports_text,
    _build_output_paths,
    _drafts_keyboard,
    _drafts_text,
    _ensure_persisted_session,
    _field_selection_keyboard,
    _issue_selection_keyboard,
    _match_author_option,
    _parse_callback_data,
    _remove_reply_keyboard,
    _review_keyboard,
    _review_text,
    _yes_no_reply_keyboard,
)
from draft_store import DraftStore, DraftSummary


class TelegramBotReviewTest(unittest.TestCase):
    def test_parse_callback_data(self) -> None:
        self.assertEqual(_parse_callback_data("review:generate"), ("generate", None))
        self.assertEqual(_parse_callback_data("review:show_revisions"), ("show_revisions", None))
        self.assertEqual(_parse_callback_data("review:archive"), ("archive", None))
        self.assertEqual(_parse_callback_data("review:restore"), ("restore", None))
        self.assertEqual(_parse_callback_data("review:delete_report"), ("delete_report", None))
        self.assertEqual(_parse_callback_data("review:menu_fields"), ("menu_fields", None))
        self.assertEqual(_parse_callback_data("review:field:date"), ("select_field", "date"))
        self.assertEqual(_parse_callback_data("review:edit_issue:1"), ("select_edit_issue", 1))
        self.assertEqual(_parse_callback_data("review:delete_issue:2"), ("select_delete_issue", 2))
        self.assertEqual(_parse_callback_data("draft:edit:9"), ("draft_edit", 9))
        self.assertEqual(_parse_callback_data("draft:list"), ("draft_list", None))
        self.assertEqual(_parse_callback_data("archived:edit:9"), ("archived_edit", 9))
        self.assertEqual(_parse_callback_data("archived:list"), ("archived_list", None))

    def test_review_text_contains_summary_and_button_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = Session(chat_id=1, draft_id=7, workspace=Path(temp_dir))
            session.display_number = 1
            session.data.update(
                {
                    "date": "16/04/2026",
                    "project_name": "Projek Demo",
                    "project_sub_name": "Fasa 1",
                    "report_title": "Bilik Server",
                    "report_purpose": "Pemeriksaan awal",
                    "report_author": "MUHAMMAD ADAM BIN JAFFRY",
                    "report_author_role": "DEVOPS ENGINEER",
                }
            )
            session.issues = [
                Issue(description="Kabel belum dirapikan", images_description="Foto server rack", image_paths=[Path("a.jpg"), Path("b.jpg")]),
                Issue(description="Label rack belum lengkap", image_paths=[]),
            ]

            text = _review_text(session)

            self.assertIn("Semakan laporan R-7:", text)
            self.assertIn("1. Tarikh laporan: 16/04/2026", text)
            self.assertIn("1. Kabel belum dirapikan | Lampiran: Foto server rack (2 gambar)", text)
            self.assertIn("2. Label rack belum lengkap (0 gambar)", text)
            self.assertIn("6. Penyedia laporan: MUHAMMAD ADAM BIN JAFFRY", text)
            self.assertIn("Gunakan butang di bawah", text)

    def test_review_keyboard_includes_nested_menu_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = Session(chat_id=1, workspace=Path(temp_dir))
            session.issues = [Issue(description="Kabel belum dirapikan", image_paths=[])]

            keyboard = _review_keyboard(session)
            labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]

            self.assertIn("Jana Laporan", labels)
            self.assertIn("Tambah Isu", labels)
            self.assertIn("Edit Butiran", labels)
            self.assertIn("Edit Isu", labels)
            self.assertIn("Padam Isu", labels)
            self.assertIn("Lihat PDF", labels)
            self.assertIn("Arkib", labels)
            self.assertIn("Padam Laporan", labels)

    def test_review_keyboard_for_archived_report_includes_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = Session(chat_id=1, workspace=Path(temp_dir), report_status="archived")
            keyboard = _review_keyboard(session)
            labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
            self.assertIn("Lihat PDF", labels)
            self.assertIn("Pulih", labels)
            self.assertIn("Padam Laporan", labels)

    def test_field_selection_keyboard_uses_numbered_buttons(self) -> None:
        keyboard = _field_selection_keyboard()
        first_row = keyboard["inline_keyboard"][0][0]
        self.assertEqual(first_row["text"], "1. Tarikh laporan")
        self.assertEqual(first_row["callback_data"], "review:field:date")
        self.assertEqual(keyboard["inline_keyboard"][5][0]["text"], "6. Penyedia laporan")

    def test_author_reply_keyboard_uses_name_only(self) -> None:
        keyboard = _author_reply_keyboard(back_to_review=True)
        labels = [row[0]["text"] for row in keyboard["keyboard"][:-1]]
        self.assertEqual(labels, [name for name, _ in AUTHOR_OPTIONS])
        self.assertEqual(keyboard["keyboard"][-1][0]["text"], AUTHOR_BACK_LABEL)

    def test_yes_no_keyboard_and_remove_keyboard(self) -> None:
        keyboard = _yes_no_reply_keyboard()
        self.assertEqual(keyboard["keyboard"][0][0]["text"], YES_LABEL)
        self.assertEqual(keyboard["keyboard"][0][1]["text"], NO_LABEL)
        self.assertEqual(_remove_reply_keyboard(), {"remove_keyboard": True})

    def test_match_author_option(self) -> None:
        self.assertEqual(
            _match_author_option("KHAIRUL ANUAR JOHARI"),
            ("KHAIRUL ANUAR JOHARI", "TECHNICAL DIRECTOR"),
        )
        self.assertIsNone(_match_author_option("UNKNOWN"))

    def test_issue_selection_keyboard_uses_numbered_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = Session(chat_id=1, workspace=Path(temp_dir))
            session.issues = [
                Issue(description="Kabel belum dirapikan", image_paths=[]),
                Issue(description="Label rack belum lengkap", image_paths=[]),
            ]

            keyboard = _issue_selection_keyboard(session, "edit")
            self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "1. Kabel belum dirapikan")
            self.assertEqual(keyboard["inline_keyboard"][1][0]["text"], "2. Label rack belum lengkap")

    def test_drafts_keyboard_and_text(self) -> None:
        drafts = [
            DraftSummary(
                draft_id=3,
                chat_id=99,
                date="16/04/2026",
                project_name="Projek Demo",
                project_sub_name="Fasa 1",
                updated_at="2026-04-16T10:00:00+00:00",
                created_at="2026-04-16T09:00:00+00:00",
                current_revision=0,
            )
        ]
        text = _drafts_text(drafts)
        keyboard = _drafts_keyboard(drafts)
        self.assertIn("R-3 | Projek Demo | Fasa 1 | 16/04/2026", text)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "Buka R-3")
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "draft:edit:3")

    def test_archived_reports_keyboard_and_text(self) -> None:
        reports = [
            DraftSummary(
                draft_id=7,
                chat_id=99,
                date="18/04/2026",
                project_name="Projek Lama",
                project_sub_name="Fasa Arkib",
                updated_at="2026-04-18T10:00:00+00:00",
                created_at="2026-04-18T09:00:00+00:00",
                current_revision=2,
            )
        ]
        text = _archived_reports_text(reports)
        keyboard = _archived_reports_keyboard(reports)
        self.assertIn("R-7 | Projek Lama | Fasa Arkib | 18/04/2026", text)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "Buka R-7")
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "archived:edit:7")

    def test_count_total_images_counts_saved_and_current_issue(self) -> None:
        session = Session(chat_id=1)
        session.issues = [
            Issue(description="Isu 1", image_paths=[Path("a.jpg"), Path("b.jpg")]),
            Issue(description="Isu 2", image_paths=[Path("c.jpg")]),
        ]
        session.current_issue.image_paths = [Path("d.jpg")]
        self.assertEqual(_count_total_images(session), 4)

    def test_extract_image_file_returns_file_size(self) -> None:
        file_id, suffix, file_size = _extract_image_file(
            {"document": {"file_id": "123", "file_name": "image.png", "mime_type": "image/png", "file_size": 456}}
        )
        self.assertEqual(file_id, "123")
        self.assertEqual(suffix, ".png")
        self.assertEqual(file_size, 456)

    def test_build_output_paths_uses_pdf_and_docx(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = Session(chat_id=1, draft_id=7, workspace=Path(temp_dir))
            payload = ReportData(
                date="16/04/2026",
                project_name="Projek Demo",
                project_sub_name="Fasa 1",
                report_title="Bilik Server",
                report_purpose="Pemeriksaan awal",
                report_author="MUHAMMAD ADAM BIN JAFFRY",
                report_author_role="DEVOPS ENGINEER",
                issues=[],
            )
            docx_path, pdf_path = _build_output_paths(session.workspace, payload)
            self.assertEqual(docx_path.suffix, ".docx")
            self.assertEqual(pdf_path.suffix, ".pdf")

    def test_start_session_is_not_persisted_until_first_real_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = Session(chat_id=1, workspace=root / "runtime")

            self.assertEqual(store.list_reports(chat_id=1), [])
            _ensure_persisted_session(store, session)
            reports = store.list_reports(chat_id=1)

            self.assertEqual(len(reports), 1)
            self.assertIsNotNone(session.draft_id)


if __name__ == "__main__":
    unittest.main()
