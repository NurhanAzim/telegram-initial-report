from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_state import Session
from report_generator import Issue, ReportData
from telegram_bot import (
    _build_output_paths,
    _drafts_keyboard,
    _drafts_text,
    _field_selection_keyboard,
    _issue_selection_keyboard,
    _parse_callback_data,
    _review_keyboard,
    _review_text,
)
from draft_store import DraftSummary


class TelegramBotReviewTest(unittest.TestCase):
    def test_parse_callback_data(self) -> None:
        self.assertEqual(_parse_callback_data("review:generate"), ("generate", None))
        self.assertEqual(_parse_callback_data("review:menu_fields"), ("menu_fields", None))
        self.assertEqual(_parse_callback_data("review:field:date"), ("select_field", "date"))
        self.assertEqual(_parse_callback_data("review:edit_issue:1"), ("select_edit_issue", 1))
        self.assertEqual(_parse_callback_data("review:delete_issue:2"), ("select_delete_issue", 2))
        self.assertEqual(_parse_callback_data("draft:edit:9"), ("draft_edit", 9))
        self.assertEqual(_parse_callback_data("draft:list"), ("draft_list", None))

    def test_review_text_contains_summary_and_button_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = Session(chat_id=1, draft_id=7, workspace=Path(temp_dir))
            session.data.update(
                {
                    "date": "16/04/2026",
                    "project_name": "Projek Demo",
                    "project_sub_name": "Fasa 1",
                    "report_title": "Bilik Server",
                    "report_purpose": "Pemeriksaan awal",
                    "project_location": "Petaling Jaya",
                }
            )
            session.issues = [
                Issue(description="Kabel belum dirapikan", image_paths=[Path("a.jpg"), Path("b.jpg")]),
                Issue(description="Label rack belum lengkap", image_paths=[]),
            ]

            text = _review_text(session)

            self.assertIn("Semakan laporan draf #7:", text)
            self.assertIn("1. Tarikh laporan: 16/04/2026", text)
            self.assertIn("1. Kabel belum dirapikan (2 gambar)", text)
            self.assertIn("2. Label rack belum lengkap (0 gambar)", text)
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

    def test_field_selection_keyboard_uses_numbered_buttons(self) -> None:
        keyboard = _field_selection_keyboard()
        first_row = keyboard["inline_keyboard"][0][0]
        self.assertEqual(first_row["text"], "1. Tarikh laporan")
        self.assertEqual(first_row["callback_data"], "review:field:date")

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
            )
        ]
        text = _drafts_text(drafts)
        keyboard = _drafts_keyboard(drafts)
        self.assertIn("#3 | Projek Demo | Fasa 1 | 16/04/2026", text)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "draft:edit:3")

    def test_build_output_paths_uses_pdf_and_docx(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = Session(chat_id=1, draft_id=7, workspace=Path(temp_dir))
            payload = ReportData(
                date="16/04/2026",
                project_name="Projek Demo",
                project_sub_name="Fasa 1",
                report_title="Bilik Server",
                report_purpose="Pemeriksaan awal",
                project_location="Petaling Jaya",
                issues=[],
            )
            docx_path, pdf_path = _build_output_paths(session.workspace, payload)
            self.assertEqual(docx_path.suffix, ".docx")
            self.assertEqual(pdf_path.suffix, ".pdf")


if __name__ == "__main__":
    unittest.main()
