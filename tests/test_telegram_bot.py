from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_state import PendingIssue, Session
from report_generator import Issue, ReportData
from telegram_bot import (
    AUTHOR_OPTIONS,
    AUTHOR_BACK_LABEL,
    NO_LABEL,
    YES_LABEL,
    _handle_callback_query,
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
    _revision_keyboard,
    _remove_reply_keyboard,
    _review_keyboard,
    _review_text,
    _show_report_revisions,
    _yes_no_reply_keyboard,
)
from draft_store import DraftStore, DraftSummary, GeneratedFileRecord


class TelegramBotReviewTest(unittest.TestCase):
    class _FakeClient:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str, dict | None]] = []

        def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
            self.messages.append((chat_id, text, reply_markup))
            return {"message_id": len(self.messages)}

        def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
            self.messages.append((chat_id, text, reply_markup))
            return {"message_id": message_id}

        def delete_message(self, chat_id: int, message_id: int) -> dict:
            return {}

    def test_parse_callback_data(self) -> None:
        self.assertEqual(_parse_callback_data("review:generate"), ("generate", None))
        self.assertEqual(_parse_callback_data("review:show_revisions"), ("show_revisions", None))
        self.assertEqual(_parse_callback_data("review:archive"), ("archive", None))
        self.assertEqual(_parse_callback_data("review:restore"), ("restore", None))
        self.assertEqual(_parse_callback_data("review:delete_report"), ("delete_report", None))
        self.assertEqual(_parse_callback_data("review:confirm_delete_report"), ("confirm_delete_report", None))
        self.assertEqual(_parse_callback_data("review:cancel_delete_report"), ("cancel_delete_report", None))
        self.assertEqual(_parse_callback_data("review:expired_revision:3"), ("expired_revision", 3))
        self.assertEqual(_parse_callback_data("review:menu_fields"), ("menu_fields", None))
        self.assertEqual(_parse_callback_data("review:field:date"), ("select_field", "date"))
        self.assertEqual(_parse_callback_data("review:edit_issue:1"), ("select_edit_issue", 1))
        self.assertEqual(_parse_callback_data("review:edit_issue_description:1"), ("edit_issue_description", 1))
        self.assertEqual(_parse_callback_data("review:edit_issue_images_description:1"), ("edit_issue_images_description", 1))
        self.assertEqual(_parse_callback_data("review:edit_issue_add_image:1"), ("edit_issue_add_image", 1))
        self.assertEqual(_parse_callback_data("review:delete_issue:2"), ("select_delete_issue", 2))
        self.assertEqual(_parse_callback_data("review:confirm_delete_issue:2"), ("confirm_delete_issue", 2))
        self.assertEqual(_parse_callback_data("review:cancel_delete_issue"), ("cancel_delete_issue", None))
        self.assertEqual(_parse_callback_data("review:menu_remove_issue_image:1"), ("menu_remove_issue_image", 1))
        self.assertEqual(_parse_callback_data("review:remove_issue_image:1:0"), ("remove_issue_image", (1, 0)))
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
                    "report_action": "Pemeriksaan semula dan pembetulan asas dibuat.",
                    "report_conclusion": "Semua tindakan selesai.",
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
            self.assertIn("Tindakan: Pemeriksaan semula dan pembetulan asas dibuat.", text)
            self.assertIn("Kesimpulan laporan: Semua tindakan selesai.", text)
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
        self.assertEqual(keyboard["inline_keyboard"][6][0]["text"], "7. Tindakan")
        self.assertEqual(keyboard["inline_keyboard"][7][0]["text"], "8. Kesimpulan laporan")

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
            _match_author_option("AHMAD FARHAN"),
            ("AHMAD FARHAN", "PROJECT ENGINEER"),
        )
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

    def test_revision_keyboard_marks_expired_revisions_clearly(self) -> None:
        revisions = [
            GeneratedFileRecord(
                record_id=1,
                draft_id=7,
                revision_number=2,
                remote_path="InitialReports/report-2.pdf",
                share_id="share-2",
                share_url="https://cloud.example.com/s/rev2",
                created_at="2026-04-18T10:00:00+00:00",
                status="available",
            ),
            GeneratedFileRecord(
                record_id=2,
                draft_id=7,
                revision_number=1,
                remote_path="InitialReports/report-1.pdf",
                share_id="share-1",
                share_url="https://cloud.example.com/s/rev1",
                created_at="2026-04-17T10:00:00+00:00",
                status="expired",
            ),
        ]

        keyboard = _revision_keyboard(revisions)

        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "Revision 2")
        self.assertEqual(keyboard["inline_keyboard"][0][0]["url"], "https://cloud.example.com/s/rev2")
        self.assertEqual(keyboard["inline_keyboard"][1][0]["text"], "Revision 1 (luput)")
        self.assertEqual(keyboard["inline_keyboard"][1][0]["callback_data"], "review:expired_revision:1")

    def test_show_report_revisions_renders_timestamps_without_name_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.data.update(
                {
                    "date": "16/04/2026",
                    "project_name": "Projek Demo",
                    "project_sub_name": "Fasa 1",
                }
            )
            store.save_session(session)
            store.record_revision(
                draft_id=session.draft_id or 0,
                payload_json="{}",
                remote_path="InitialReports/report-1.pdf",
                share_id="share-1",
                share_url="https://cloud.example.com/s/rev1",
            )
            client = self._FakeClient()

            _show_report_revisions(client, store, session)

            self.assertEqual(len(client.messages), 1)
            self.assertIn("PDF revision untuk laporan R-", client.messages[0][1])
            self.assertIn("Revision 1 | Tersedia |", client.messages[0][1])

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
                report_action="Pemeriksaan semula dibuat.",
                report_conclusion="Selesai.",
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

    def test_delete_report_requires_confirmation_before_removal(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str, dict | None]] = []
                self.callback_answers: list[tuple[str, str | None]] = []
                self.deleted_messages: list[tuple[int, int]] = []

            def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": len(self.messages)}

            def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": message_id}

            def delete_message(self, chat_id: int, message_id: int) -> dict:
                self.deleted_messages.append((chat_id, message_id))
                return {}

            def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
                self.callback_answers.append((callback_query_id, text))
                return {}

        class FakeNextcloud:
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.review_message_id = 44
            store.save_session(session)
            sessions = {1: session}
            client = FakeClient()

            _handle_callback_query(
                client,
                FakeNextcloud(),
                store,
                {
                    "id": "cb-1",
                    "data": "review:delete_report",
                    "message": {"message_id": 44, "chat": {"id": 1}},
                },
                sessions,
                archived_report_retention_days=30,
            )

            self.assertIn(1, sessions)
            self.assertEqual(len(store.list_reports(chat_id=1)), 1)
            self.assertEqual(session.stage, "confirm_delete_report")
            self.assertIn("Anda pasti mahu padam laporan ini?", client.messages[-1][1])

            _handle_callback_query(
                client,
                FakeNextcloud(),
                store,
                {
                    "id": "cb-2",
                    "data": "review:confirm_delete_report",
                    "message": {"message_id": 44, "chat": {"id": 1}},
                },
                sessions,
                archived_report_retention_days=30,
            )

            self.assertNotIn(1, sessions)
            self.assertEqual(len(store.list_reports(chat_id=1)), 0)
            self.assertEqual(client.messages[-1][1], "Laporan dipadam. Hantar /start untuk mula semula.")

    def test_delete_issue_requires_confirmation_before_removal(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str, dict | None]] = []
                self.callback_answers: list[tuple[str, str | None]] = []

            def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": len(self.messages)}

            def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": message_id}

            def delete_message(self, chat_id: int, message_id: int) -> dict:
                return {}

            def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
                self.callback_answers.append((callback_query_id, text))
                return {}

        class FakeNextcloud:
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.review_message_id = 55
            session.issues = [Issue(description="Kabel belum dirapikan", image_paths=[])]
            store.save_session(session)
            sessions = {1: session}
            client = FakeClient()

            _handle_callback_query(
                client,
                FakeNextcloud(),
                store,
                {
                    "id": "cb-1",
                    "data": "review:delete_issue:0",
                    "message": {"message_id": 55, "chat": {"id": 1}},
                },
                sessions,
                archived_report_retention_days=30,
            )

            self.assertEqual(len(session.issues), 1)
            self.assertEqual(session.stage, "confirm_delete_issue")
            self.assertEqual(session.delete_issue_index, 0)
            self.assertIn("Anda pasti mahu padam isu 1?", client.messages[-1][1])

            persisted = store.load_report(chat_id=1, report_id=session.draft_id or 0)
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.stage, "confirm_delete_issue")
            self.assertEqual(persisted.delete_issue_index, 0)

            _handle_callback_query(
                client,
                FakeNextcloud(),
                store,
                {
                    "id": "cb-2",
                    "data": "review:confirm_delete_issue:0",
                    "message": {"message_id": 55, "chat": {"id": 1}},
                },
                sessions,
                archived_report_retention_days=30,
            )

            self.assertEqual(len(session.issues), 0)
            self.assertEqual(session.stage, "review")
            self.assertIsNone(session.delete_issue_index)
            self.assertIn('Isu 1 dibuang: "Kabel belum dirapikan"', client.messages[-1][1])

    def test_archived_edit_rejects_archive_past_retention_window(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str, dict | None]] = []
                self.callback_answers: list[tuple[str, str | None]] = []

            def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": len(self.messages)}

            def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": message_id}

            def delete_message(self, chat_id: int, message_id: int) -> dict:
                return {}

            def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
                self.callback_answers.append((callback_query_id, text))
                return {}

        class FakeNextcloud:
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.data["project_name"] = "Archive Lama"
            store.save_session(session)
            store.archive_report(chat_id=1, report_id=session.draft_id or 0)
            with store._connection() as connection:
                connection.execute(
                    "UPDATE drafts SET archived_at = ?, updated_at = ? WHERE id = ?",
                    ("2026-03-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", session.draft_id),
                )

            sessions: dict[int, Session] = {}
            client = FakeClient()

            _handle_callback_query(
                client,
                FakeNextcloud(),
                store,
                {
                    "id": "cb-1",
                    "data": f"archived:edit:{session.draft_id}",
                    "message": {"message_id": 44, "chat": {"id": 1}},
                },
                sessions,
                archived_report_retention_days=30,
            )

            self.assertEqual(client.callback_answers, [("cb-1", "Laporan arkib tidak dijumpai.")])
            self.assertEqual(client.messages[-1][1], "Laporan arkib itu tidak lagi wujud.")
            self.assertEqual(sessions, {})

    def test_edit_issue_description_prompt_includes_current_value(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str, dict | None]] = []
                self.callback_answers: list[tuple[str, str | None]] = []

            def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": len(self.messages)}

            def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": message_id}

            def delete_message(self, chat_id: int, message_id: int) -> dict:
                return {}

            def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
                self.callback_answers.append((callback_query_id, text))
                return {}

        class FakeNextcloud:
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.review_message_id = 88
            session.issues = [Issue(description="Kabel belum dirapikan", image_paths=[])]
            store.save_session(session)
            sessions = {1: session}
            client = FakeClient()

            _handle_callback_query(
                client,
                FakeNextcloud(),
                store,
                {
                    "id": "cb-1",
                    "data": "review:edit_issue_description:0",
                    "message": {"message_id": 88, "chat": {"id": 1}},
                },
                sessions,
                archived_report_retention_days=30,
            )

            self.assertIn("Semasa: Kabel belum dirapikan", client.messages[-1][1])

    def test_edit_issue_image_description_prompt_includes_current_value(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str, dict | None]] = []
                self.callback_answers: list[tuple[str, str | None]] = []

            def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": len(self.messages)}

            def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": message_id}

            def delete_message(self, chat_id: int, message_id: int) -> dict:
                return {}

            def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
                self.callback_answers.append((callback_query_id, text))
                return {}

        class FakeNextcloud:
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.review_message_id = 89
            session.issues = [Issue(description="Kabel belum dirapikan", images_description="Foto asal", image_paths=[])]
            store.save_session(session)
            sessions = {1: session}
            client = FakeClient()

            _handle_callback_query(
                client,
                FakeNextcloud(),
                store,
                {
                    "id": "cb-1",
                    "data": "review:edit_issue_images_description:0",
                    "message": {"message_id": 89, "chat": {"id": 1}},
                },
                sessions,
                archived_report_retention_days=30,
            )

            self.assertIn("Semasa: Foto asal", client.messages[-1][1])

    def test_remove_issue_image_deletes_file_and_updates_issue(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.messages: list[tuple[int, str, dict | None]] = []
                self.callback_answers: list[tuple[str, str | None]] = []

            def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": len(self.messages)}

            def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
                self.messages.append((chat_id, text, reply_markup))
                return {"message_id": message_id}

            def delete_message(self, chat_id: int, message_id: int) -> dict:
                return {}

            def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
                self.callback_answers.append((callback_query_id, text))
                return {}

        class FakeNextcloud:
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.review_message_id = 77
            image_path = session.workspace / "issue-1-1.jpg"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_text("x", encoding="utf-8")
            session.issues = [Issue(description="Kabel belum dirapikan", image_paths=[image_path])]
            store.save_session(session)
            sessions = {1: session}
            client = FakeClient()

            _handle_callback_query(
                client,
                FakeNextcloud(),
                store,
                {
                    "id": "cb-1",
                    "data": "review:remove_issue_image:0:0",
                    "message": {"message_id": 77, "chat": {"id": 1}},
                },
                sessions,
                archived_report_retention_days=30,
            )

            self.assertEqual(session.issues[0].image_paths, [])
            self.assertFalse(image_path.exists())
            self.assertIn("Tiada gambar lagi untuk isu ini.", client.messages[-1][1])

from telegram_flow import ConversationHooks
from telegram_bot import _handle_issue_description


class IssueFlowOrderTest(unittest.TestCase):
    """Tests for the reordered issue flow: tambah isu → lampiran → butiran isu."""

    class _FakeClient:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str, dict | None]] = []

        def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
            self.messages.append((chat_id, text, reply_markup))
            return {"message_id": len(self.messages)}

        def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict:
            self.messages.append((chat_id, text, reply_markup))
            return {"message_id": message_id}

        def delete_message(self, chat_id: int, message_id: int) -> dict:
            return {}

        def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
            return {}

        def download_file(self, file_id: str, file_path: Path) -> None:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(b"fake-image")

    def _hooks(self) -> ConversationHooks:
        return ConversationHooks(
            show_review=lambda *a, **kw: None,
            dismiss_reply_keyboard=lambda *a, **kw: None,
        )

    def test_issue_description_transitions_to_issue_images(self) -> None:
        """After entering issue description, bot should ask for images (lampiran), not description."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.stage = "issue_description"
            store.save_session(session)

            client = self._FakeClient()
            _handle_issue_description(
                client, store, session, "Paip bocor di tingkat 3",
                max_issues_per_report=10, hooks=self._hooks(),
            )

            # Stage should now be issue_images (lampiran), NOT issue_images_description
            self.assertEqual(session.stage, "issue_images")
            # The prompt should mention gambar (images), not keterangan lampiran
            last_message = client.messages[-1][1]
            self.assertIn("gambar", last_message.lower())
            # current_issue should have the description stored
            self.assertEqual(session.current_issue.description, "Paip bocor di tingkat 3")

    def test_issue_images_done_transitions_to_images_description_without_finalizing(self) -> None:
        """After /done on image step, bot should ask for attachment description (butiran isu),
        and the issue should NOT yet be finalized into session.issues."""
        from telegram_bot import _handle_issue_images

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.stage = "issue_images"
            session.current_issue = PendingIssue(
                description="Paip bocor",
                image_paths=[root / "fake-img.jpg"],
            )
            store.save_session(session)

            client = self._FakeClient()
            _handle_issue_images(
                client, store, session,
                message={"photo": [{"file_id": "x", "file_size": 100}]},
                text="/done",
                max_images_per_issue=5,
                max_total_images_per_report=20,
                max_image_file_size_bytes=10 * 1024 * 1024,
            )

            # Stage should be issue_images_description (butiran isu), NOT more_issues
            self.assertEqual(session.stage, "issue_images_description")
            # Issue should NOT be finalized yet — still in current_issue
            self.assertEqual(len(session.issues), 0)
            self.assertEqual(session.current_issue.description, "Paip bocor")
            # Prompt should ask for keterangan lampiran, not "Tambah isu lain?"
            last_message = client.messages[-1][1]
            self.assertIn("keterangan lampiran", last_message.lower())

    def test_issue_images_description_finalizes_and_transitions_to_more_issues(self) -> None:
        """After entering attachment description (or /skip), the issue should be finalized
        and bot should ask 'Tambah isu lain?'"""
        from telegram_bot import _handle_issue_images_description

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.stage = "issue_images_description"
            session.current_issue = PendingIssue(
                description="Paip bocor",
                images_description="",
                image_paths=[root / "fake-img.jpg"],
            )
            store.save_session(session)

            client = self._FakeClient()
            _handle_issue_images_description(client, store, session, "Gambar selepas pembaikan")

            # Issue should be finalized into session.issues
            self.assertEqual(len(session.issues), 1)
            self.assertEqual(session.issues[0].description, "Paip bocor")
            self.assertEqual(session.issues[0].images_description, "Gambar selepas pembaikan")
            # current_issue should be reset
            self.assertEqual(session.current_issue.description, "")
            # Stage should be more_issues
            self.assertEqual(session.stage, "more_issues")
            # Prompt should ask "Tambah isu lain?" with yes/no keyboard
            last_message = client.messages[-1]
            self.assertIn("Tambah isu lain?", last_message[1])
            self.assertIsNotNone(last_message[2])  # reply_markup (yes/no keyboard)

    def test_issue_images_description_skip_finalizes_with_empty_description(self) -> None:
        """When user sends /skip at the description step, issue finalizes with empty images_description."""
        from telegram_bot import _handle_issue_images_description

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.stage = "issue_images_description"
            session.current_issue = PendingIssue(
                description="Paip bocor",
                image_paths=[root / "fake-img.jpg"],
            )
            store.save_session(session)

            client = self._FakeClient()
            _handle_issue_images_description(client, store, session, "/skip")

            self.assertEqual(len(session.issues), 1)
            self.assertEqual(session.issues[0].images_description, "")
            self.assertEqual(session.stage, "more_issues")

    def test_no_photos_still_asks_for_description(self) -> None:
        """If user sends /done at image step with zero photos, bot should still
        transition to issue_images_description and ask for the attachment description."""
        from telegram_bot import _handle_issue_images

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = DraftStore(db_path=root / "bot.db", drafts_dir=root / "drafts")
            session = store.create_report(chat_id=1)
            session.stage = "issue_images"
            session.current_issue = PendingIssue(description="Paip bocor")
            store.save_session(session)

            client = self._FakeClient()
            _handle_issue_images(
                client, store, session,
                message={"photo": [{"file_id": "x", "file_size": 100}]},
                text="/done",
                max_images_per_issue=5,
                max_total_images_per_report=20,
                max_image_file_size_bytes=10 * 1024 * 1024,
            )

            # Should still go to description step even with no photos
            self.assertEqual(session.stage, "issue_images_description")
            self.assertEqual(len(session.current_issue.image_paths), 0)
            last_message = client.messages[-1][1]
            self.assertIn("keterangan lampiran", last_message.lower())


if __name__ == "__main__":
    unittest.main()
