from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from bot_state import PendingIssue, Session
from draft_store import DraftStore, DraftSummary
from nextcloud_client import NextcloudClient, sanitize_filename_part
from report_generator import Issue, ReportData, render_report


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)

POLL_TIMEOUT_SECONDS = 30
HOUSEKEEPING_INTERVAL_SECONDS = 3600
ENV_PATH = Path(".env")
TEMPLATE_PATH = Path("Template Initial Report.docx")
ARCHIVED_REPORT_RETENTION_DAYS_DEFAULT = 30
AUTO_ARCHIVE_ACTIVE_REPORT_DAYS_DEFAULT = 0
MAX_IMAGES_PER_ISSUE_DEFAULT = 10
MAX_ISSUES_PER_REPORT_DEFAULT = 20
MAX_TOTAL_IMAGES_PER_REPORT_DEFAULT = 40
MAX_IMAGE_FILE_SIZE_MB_DEFAULT = 10

FIELDS: list[tuple[str, str, str]] = [
    ("date", "Tarikh laporan", "Format: DD/MM/YYYY. Contoh: 16/04/2026"),
    ("project_name", "Nama projek", "Contoh: Naik taraf rangkaian HQ"),
    ("project_sub_name", "Sub-projek", "Contoh: Fasa 1"),
    ("report_title", "Tajuk laporan", "Contoh: Pemeriksaan bilik server"),
    ("report_purpose", "Tujuan laporan", "Contoh: Pemeriksaan awal tapak"),
    ("report_author", "Penyedia laporan", "Pilih nama daripada butang yang disediakan"),
]
FIELD_LABELS = {key: label for key, label, _ in FIELDS}
FIELD_GUIDANCE = {key: guidance for key, _, guidance in FIELDS}
AUTHOR_OPTIONS: list[tuple[str, str]] = [
    ("MUHAMMAD ADAM BIN JAFFRY", "DEVOPS ENGINEER"),
    ("DZAHIRUDDIN BIN DZULKIFLEE", "ASSOCIATE ENGINEER"),
    ("SYAMSUL RIZAL BIN BAKRI", "SENIOR ENGINEER"),
    ("ZAILAH BINTI BUANG", "PROJECT EXECUTIVE"),
    ("KHAIRUL ANUAR JOHARI", "TECHNICAL DIRECTOR"),
]
AUTHOR_BACK_LABEL = "Kembali ke Semakan"
YES_LABEL = "Ya"
NO_LABEL = "Tidak"

BOT_COMMANDS = [
    {"command": "start", "description": "Mula laporan baharu"},
    {"command": "reports", "description": "Senarai laporan aktif"},
    {"command": "archived", "description": "Senarai laporan arkib"},
    {"command": "done", "description": "Selesai untuk langkah semasa"},
    {"command": "cancel", "description": "Padam laporan semasa"},
    {"command": "help", "description": "Tunjuk panduan ringkas"},
]

REVIEW_CALLBACK_PREFIX = "review"
DRAFT_CALLBACK_PREFIX = "draft"
ARCHIVED_CALLBACK_PREFIX = "archived"


class TelegramBotClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_url = f"https://api.telegram.org/file/bot{token}"

    def request(self, method: str, **kwargs) -> dict:
        response = requests.post(f"{self.base_url}/{method}", timeout=60, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", f"Telegram API error on {method}"))
        return payload["result"]

    def get_updates(self, offset: int | None) -> list[dict]:
        payload = {
            "timeout": POLL_TIMEOUT_SECONDS,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", json=payload)

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("sendMessage", json=payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> dict:
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageText", json=payload)

    def delete_message(self, chat_id: int, message_id: int) -> dict:
        return self.request("deleteMessage", json={"chat_id": chat_id, "message_id": message_id})

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self.request("answerCallbackQuery", json=payload)

    def download_file(self, file_id: str, destination: Path) -> Path:
        file_info = self.request("getFile", json={"file_id": file_id})
        file_path = file_info["file_path"]
        response = requests.get(f"{self.file_url}/{file_path}", timeout=60)
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
        return destination

    def set_my_commands(self) -> None:
        self.request("setMyCommands", json={"commands": BOT_COMMANDS})


def main() -> None:
    _load_dotenv(ENV_PATH)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required.")
    if not TEMPLATE_PATH.exists():
        raise SystemExit(f"Template not found: {TEMPLATE_PATH}")

    data_dir = Path(os.getenv("DATA_DIR", "data")).resolve()
    runtime_dir = Path(os.getenv("RUNTIME_DIR", "runtime")).resolve()
    db_path = Path(os.getenv("DATABASE_PATH", str(data_dir / "bot.db"))).resolve()
    drafts_dir = Path(os.getenv("DRAFTS_DIR", str(data_dir / "drafts"))).resolve()
    backup_dir = Path(os.getenv("BACKUP_DIR", str(data_dir / "backups"))).resolve()
    retention_days = int(os.getenv("RETENTION_PERIOD_DAYS", "14"))
    archived_report_retention_days = int(os.getenv("ARCHIVED_REPORT_RETENTION_DAYS", str(ARCHIVED_REPORT_RETENTION_DAYS_DEFAULT)))
    auto_archive_active_report_days = int(
        os.getenv("AUTO_ARCHIVE_ACTIVE_REPORT_DAYS", str(AUTO_ARCHIVE_ACTIVE_REPORT_DAYS_DEFAULT))
    )
    max_images_per_issue = int(os.getenv("MAX_IMAGES_PER_ISSUE", str(MAX_IMAGES_PER_ISSUE_DEFAULT)))
    max_issues_per_report = int(os.getenv("MAX_ISSUES_PER_REPORT", str(MAX_ISSUES_PER_REPORT_DEFAULT)))
    max_total_images_per_report = int(os.getenv("MAX_TOTAL_IMAGES_PER_REPORT", str(MAX_TOTAL_IMAGES_PER_REPORT_DEFAULT)))
    max_image_file_size_bytes = int(float(os.getenv("MAX_IMAGE_FILE_SIZE_MB", str(MAX_IMAGE_FILE_SIZE_MB_DEFAULT))) * 1024 * 1024)

    data_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    store = DraftStore(db_path=db_path, drafts_dir=drafts_dir, backup_dir=backup_dir)
    nextcloud = _load_nextcloud_client()
    client = TelegramBotClient(token)
    client.set_my_commands()

    _run_housekeeping(
        store,
        nextcloud,
        retention_days,
        archived_report_retention_days,
        auto_archive_active_report_days,
    )
    next_housekeeping_at = time.monotonic() + HOUSEKEEPING_INTERVAL_SECONDS
    sessions: dict[int, Session] = {}
    next_offset: int | None = None

    while True:
        try:
            if time.monotonic() >= next_housekeeping_at:
                _run_housekeeping(
                    store,
                    nextcloud,
                    retention_days,
                    archived_report_retention_days,
                    auto_archive_active_report_days,
                )
                next_housekeeping_at = time.monotonic() + HOUSEKEEPING_INTERVAL_SECONDS

            updates = client.get_updates(next_offset)
            for update in updates:
                next_offset = update["update_id"] + 1
                _handle_update(
                    client,
                    nextcloud,
                    store,
                    update,
                    sessions,
                    max_images_per_issue,
                    max_issues_per_report,
                    max_total_images_per_report,
                    max_image_file_size_bytes,
                )
        except KeyboardInterrupt:
            raise
        except Exception:
            LOGGER.exception("Polling loop failed; retrying shortly.")
            time.sleep(3)


def _handle_update(
    client: TelegramBotClient,
    nextcloud: NextcloudClient,
    store: DraftStore,
    update: dict,
    sessions: dict[int, Session],
    max_images_per_issue: int,
    max_issues_per_report: int,
    max_total_images_per_report: int,
    max_image_file_size_bytes: int,
) -> None:
    callback_query = update.get("callback_query")
    if callback_query:
        _handle_callback_query(client, nextcloud, store, callback_query, sessions)
        return

    message = update.get("message")
    if not message:
        return

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    if text == "/start":
        session = Session(chat_id=chat_id)
        sessions[chat_id] = session
        client.send_message(chat_id, f"Laporan baharu dimulakan.\n\n{_field_prompt(0)}")
        return

    if text in {"/drafts", "/reports"}:
        _show_drafts(client, store, chat_id)
        return

    if text == "/archived":
        _show_archived_reports(client, store, chat_id)
        return

    if text.startswith("/edit"):
        _handle_edit_command(client, store, sessions, chat_id, text)
        return

    if text == "/help":
        client.send_message(chat_id, _help_text())
        return

    session = sessions.get(chat_id)
    if session is None:
        client.send_message(chat_id, "Tiada laporan aktif. Hantar /start atau /reports.")
        return

    if text == "/cancel":
        _cancel_session(client, store, sessions, session)
        return

    if session.stage == "field":
        _handle_field_input(client, store, session, text)
        return

    if session.stage == "author_select":
        _handle_author_selection(client, store, session, text)
        return

    if session.stage == "issue_description":
        _handle_issue_description(client, store, session, text, max_issues_per_report)
        return

    if session.stage == "issue_images_description":
        _handle_issue_images_description(client, store, session, text)
        return

    if session.stage == "issue_images":
        _handle_issue_images(
            client,
            store,
            session,
            message,
            text,
            max_images_per_issue,
            max_total_images_per_report,
            max_image_file_size_bytes,
        )
        return

    if session.stage == "more_issues":
        _handle_more_issues(client, store, session, text, max_issues_per_report)
        return

    if session.stage == "review":
        client.send_message(chat_id, "Semakan menggunakan butang. Gunakan panel semakan yang dihantar bot.")
        return

    if session.stage == "edit_field":
        _handle_edit_field(client, store, session, text)
        return

    if session.stage == "edit_author":
        _handle_author_selection(client, store, session, text)
        return

    if session.stage == "edit_issue_description":
        _handle_edit_issue_description(client, store, session, text)
        return

    client.send_message(chat_id, "Keadaan sesi tidak dikenali. Hantar /cancel dan mula semula.")


def _handle_edit_command(
    client: TelegramBotClient,
    store: DraftStore,
    sessions: dict[int, Session],
    chat_id: int,
    text: str,
) -> None:
    match = re.fullmatch(r"/edit\s+(\d+)", text)
    if not match:
        client.send_message(chat_id, "Format: /edit <nombor laporan>. Contoh: /edit 1")
        return

    report_number = int(match.group(1))
    resolved = _resolve_draft_by_number(store, chat_id, report_number)
    if resolved is None:
        client.send_message(chat_id, f"Laporan #{report_number} tidak dijumpai.")
        return

    report_id, _summary = resolved
    session = store.load_report(chat_id, report_id)
    if session is None:
        client.send_message(chat_id, f"Laporan #{report_number} tidak dijumpai.")
        return

    sessions[chat_id] = session
    _resume_draft(client, store, session, prefix=f"Laporan #{report_number} dibuka.\n\n")


def _handle_field_input(client: TelegramBotClient, store: DraftStore, session: Session, text: str) -> None:
    if not text:
        client.send_message(session.chat_id, "Medan ini perlu diisi dengan teks.")
        return

    key, _, guidance = FIELDS[session.field_index]
    if key == "report_author":
        session.stage = "author_select"
        store.save_session(session)
        client.send_message(
            session.chat_id,
            "Pilih penyedia laporan:",
            reply_markup=_author_reply_keyboard(back_to_review=False),
        )
        return
    if key == "date" and not _is_valid_date(text):
        client.send_message(session.chat_id, f"Tarikh tidak sah. {guidance}")
        return

    _ensure_persisted_session(store, session)
    session.data[key] = text
    session.field_index += 1
    store.save_session(session)

    if session.field_index < len(FIELDS):
        next_key = FIELDS[session.field_index][0]
        if next_key == "report_author":
            session.stage = "author_select"
            store.save_session(session)
            client.send_message(
                session.chat_id,
                "Pilih penyedia laporan:",
                reply_markup=_author_reply_keyboard(back_to_review=False),
            )
        else:
            client.send_message(session.chat_id, _field_prompt(session.field_index))
        return

    session.stage = "issue_description"
    store.save_session(session)
    client.send_message(session.chat_id, "Hantar keterangan isu pertama. Jika tiada isu, balas /done.")


def _handle_issue_description(
    client: TelegramBotClient,
    store: DraftStore,
    session: Session,
    text: str,
    max_issues_per_report: int,
) -> None:
    if text == "/done":
        _enter_review(client, store, session)
        return
    if not text:
        client.send_message(session.chat_id, "Keterangan isu tidak boleh kosong.")
        return
    if len(session.issues) >= max_issues_per_report:
        _enter_review(client, store, session)
        client.send_message(
            session.chat_id,
            f"Had isu per laporan telah dicapai ({max_issues_per_report}). Sila semak laporan semasa.",
        )
        return

    _ensure_persisted_session(store, session)
    session.current_issue = PendingIssue(description=text)
    session.stage = "issue_images_description"
    store.save_session(session)
    client.send_message(
        session.chat_id,
        "Masukkan keterangan lampiran untuk isu ini jika perlu. Jika tiada, balas /skip.",
    )


def _handle_issue_images_description(client: TelegramBotClient, store: DraftStore, session: Session, text: str) -> None:
    _ensure_persisted_session(store, session)
    normalized = text.strip()
    if normalized.lower() == "/skip":
        session.current_issue.images_description = ""
    else:
        session.current_issue.images_description = normalized

    session.stage = "issue_images"
    store.save_session(session)
    client.send_message(session.chat_id, "Hantar gambar untuk isu ini satu demi satu. Bila selesai, balas /done.")


def _handle_author_selection(client: TelegramBotClient, store: DraftStore, session: Session, text: str) -> None:
    normalized = text.strip()
    if session.stage == "edit_author" and normalized == AUTHOR_BACK_LABEL:
        session.stage = "review"
        session.edit_field_key = None
        store.save_session(session)
        _show_review(client, store, session)
        return

    match = _match_author_option(normalized)
    if match is None:
        client.send_message(
            session.chat_id,
            "Pilih nama menggunakan papan kekunci yang disediakan.",
            reply_markup=_author_reply_keyboard(back_to_review=session.stage == "edit_author"),
        )
        return

    author_name, author_role = match
    _ensure_persisted_session(store, session)
    session.data["report_author"] = author_name
    session.data["report_author_role"] = author_role

    if session.stage == "author_select":
        session.field_index = len(FIELDS)
        session.stage = "issue_description"
        store.save_session(session)
        client.send_message(
            session.chat_id,
            f"Penyedia laporan dipilih: {author_name}\n\nHantar keterangan isu pertama. Jika tiada isu, balas /done.",
            reply_markup=_remove_reply_keyboard(),
        )
        return

    session.stage = "review"
    session.edit_field_key = None
    store.save_session(session)
    _show_review(client, store, session, prefix=f"Penyedia laporan telah dikemas kini kepada {author_name}.\n\n")


def _handle_issue_images(
    client: TelegramBotClient,
    store: DraftStore,
    session: Session,
    message: dict,
    text: str,
    max_images_per_issue: int,
    max_total_images_per_report: int,
    max_image_file_size_bytes: int,
) -> None:
    if text == "/done":
        _ensure_persisted_session(store, session)
        session.issues.append(
            Issue(
                description=session.current_issue.description,
                images_description=session.current_issue.images_description,
                image_paths=list(session.current_issue.image_paths),
            )
        )
        session.current_issue = PendingIssue()
        session.stage = "more_issues"
        store.save_session(session)
        client.send_message(
            session.chat_id,
            "Tambah isu lain?",
            reply_markup=_yes_no_reply_keyboard(),
        )
        return

    image_file_id, suffix, file_size = _extract_image_file(message)
    if not image_file_id:
        client.send_message(session.chat_id, "Hantar gambar sebagai photo atau dokumen imej, atau balas /done.")
        return
    if len(session.current_issue.image_paths) >= max_images_per_issue:
        client.send_message(
            session.chat_id,
            f"Had gambar bagi satu isu telah dicapai ({max_images_per_issue}). Balas /done untuk teruskan.",
        )
        return
    if _count_total_images(session) >= max_total_images_per_report:
        client.send_message(
            session.chat_id,
            f"Had jumlah gambar bagi satu laporan telah dicapai ({max_total_images_per_report}). Balas /done untuk teruskan.",
        )
        return
    if file_size is not None and file_size > max_image_file_size_bytes:
        limit_mb = max_image_file_size_bytes / (1024 * 1024)
        client.send_message(
            session.chat_id,
            f"Saiz fail gambar melebihi had {limit_mb:.0f} MB. Sila hantar fail lebih kecil.",
        )
        return

    _ensure_persisted_session(store, session)
    issue_number = len(session.issues) + 1
    image_number = len(session.current_issue.image_paths) + 1
    file_path = session.workspace / f"issue-{issue_number}-{image_number}{suffix}"
    client.download_file(image_file_id, file_path)
    session.current_issue.image_paths.append(file_path)
    store.save_session(session)
    client.send_message(session.chat_id, f"Gambar diterima: {file_path.name}")


def _handle_more_issues(
    client: TelegramBotClient,
    store: DraftStore,
    session: Session,
    text: str,
    max_issues_per_report: int,
) -> None:
    normalized = text.lower()
    if normalized in {YES_LABEL.lower(), "y", "yes"}:
        if len(session.issues) >= max_issues_per_report:
            _enter_review(client, store, session)
            client.send_message(
                session.chat_id,
                f"Had isu per laporan telah dicapai ({max_issues_per_report}). Sila semak laporan semasa.",
            )
            return
        session.stage = "issue_description"
        store.save_session(session)
        client.send_message(
            session.chat_id,
            "Hantar keterangan isu seterusnya.",
            reply_markup=_remove_reply_keyboard(),
        )
        return
    if normalized in {NO_LABEL.lower(), "tak", "t", "no", "n"}:
        _enter_review(client, store, session)
        return

    client.send_message(
        session.chat_id,
        "Pilih Ya atau Tidak menggunakan papan kekunci yang disediakan.",
        reply_markup=_yes_no_reply_keyboard(),
    )


def _handle_edit_field(client: TelegramBotClient, store: DraftStore, session: Session, text: str) -> None:
    field_key = session.edit_field_key
    if not field_key:
        session.stage = "review"
        store.save_session(session)
        _show_review(client, store, session)
        return

    if not text:
        client.send_message(session.chat_id, "Nilai ini tidak boleh kosong.")
        return

    if field_key == "date" and not _is_valid_date(text):
        client.send_message(session.chat_id, f"Tarikh tidak sah. {FIELD_GUIDANCE[field_key]}")
        return

    session.data[field_key] = text
    session.edit_field_key = None
    session.stage = "review"
    store.save_session(session)
    _show_review(client, store, session, prefix=f"{FIELD_LABELS[field_key]} telah dikemas kini.\n\n")


def _handle_edit_issue_description(client: TelegramBotClient, store: DraftStore, session: Session, text: str) -> None:
    issue_index = session.edit_issue_index
    if issue_index is None or issue_index >= len(session.issues):
        session.edit_issue_index = None
        session.stage = "review"
        store.save_session(session)
        _show_review(client, store, session)
        return

    if not text:
        client.send_message(session.chat_id, "Keterangan isu tidak boleh kosong.")
        return

    issue_number = issue_index + 1
    session.issues[issue_index].description = text
    session.edit_issue_index = None
    session.stage = "review"
    store.save_session(session)
    _show_review(client, store, session, prefix=f"Keterangan isu {issue_number} telah dikemas kini.\n\n")


def _handle_callback_query(
    client: TelegramBotClient,
    nextcloud: NextcloudClient,
    store: DraftStore,
    callback_query: dict,
    sessions: dict[int, Session],
) -> None:
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    callback_id = callback_query["id"]
    data = callback_query.get("data", "")

    if not chat_id:
        client.answer_callback_query(callback_id)
        return

    action, value = _parse_callback_data(data)

    if action == "draft_edit" and isinstance(value, int):
        draft_number = _draft_display_number(store, chat_id, value)
        if draft_number is None:
            client.answer_callback_query(callback_id, "Laporan tidak dijumpai.")
            client.send_message(chat_id, "Laporan itu tidak lagi wujud dalam senarai semasa.")
            return
        client.answer_callback_query(callback_id, f"Membuka laporan #{draft_number}...")
        session = store.load_report(chat_id, value)
        if session is None:
            client.send_message(chat_id, f"Laporan #{draft_number} tidak dijumpai.")
            return
        _delete_message_if_possible(client, chat_id, message.get("message_id"))
        sessions[chat_id] = session
        _resume_draft(client, store, session, prefix=f"Laporan #{draft_number} dibuka.\n\n")
        return

    if action == "archived_edit" and isinstance(value, int):
        session = store.load_report_with_status(chat_id, value, statuses=("archived",))
        if session is None:
            client.answer_callback_query(callback_id, "Laporan arkib tidak dijumpai.")
            client.send_message(chat_id, "Laporan arkib itu tidak lagi wujud.")
            return
        client.answer_callback_query(callback_id, f"Membuka R-{value}...")
        _delete_message_if_possible(client, chat_id, message.get("message_id"))
        sessions[chat_id] = session
        _resume_draft(client, store, session, prefix=f"Laporan arkib R-{value} dibuka.\n\n")
        return

    if action == "draft_list":
        client.answer_callback_query(callback_id)
        _show_drafts(client, store, chat_id)
        return

    if action == "archived_list":
        client.answer_callback_query(callback_id)
        _show_archived_reports(client, store, chat_id)
        return

    if not data.startswith(f"{REVIEW_CALLBACK_PREFIX}:"):
        client.answer_callback_query(callback_id)
        return

    session = sessions.get(chat_id)
    if session is None:
        client.answer_callback_query(callback_id, "Sesi sudah tamat.")
        return

    session.review_message_id = message.get("message_id") or session.review_message_id
    store.save_session(session)

    if action == "generate":
        client.answer_callback_query(callback_id, "Menjana PDF...")
        _set_review_message(client, store, session, "Menjana PDF...", None)
        revision_number = _finish_report(client, nextcloud, store, session)
        _show_report_revisions(
            client,
            store,
            session,
            prefix=f"PDF revision {revision_number} telah dijana.\n\n",
        )
        return

    if action == "back":
        client.answer_callback_query(callback_id)
        session.stage = "review"
        session.edit_field_key = None
        session.edit_issue_index = None
        store.save_session(session)
        _show_review(client, store, session)
        return

    if action == "show":
        client.answer_callback_query(callback_id)
        _show_review(client, store, session)
        return

    if action == "show_revisions":
        client.answer_callback_query(callback_id)
        _show_report_revisions(client, store, session)
        return

    if action == "expired_revision" and isinstance(value, int):
        client.answer_callback_query(callback_id, "Revision itu sudah luput.")
        _show_report_revisions(client, store, session, prefix=_expired_revision_prefix(value))
        return

    if action == "archive":
        client.answer_callback_query(callback_id, "Mengarkib laporan...")
        if session.draft_id is not None:
            store.archive_report(session.chat_id, session.draft_id)
        _delete_message_if_possible(client, session.chat_id, session.review_message_id)
        sessions.pop(chat_id, None)
        client.send_message(chat_id, "Laporan telah diarkibkan.")
        return

    if action == "restore":
        client.answer_callback_query(callback_id, "Memulihkan laporan...")
        if session.draft_id is not None:
            store.restore_report(session.chat_id, session.draft_id)
            session.report_status = "active"
            store.save_session(session, status="active")
        _show_review(client, store, session, prefix="Laporan telah dipulihkan ke senarai aktif.\n\n")
        return

    if action == "delete_report":
        client.answer_callback_query(callback_id, "Memadam laporan...")
        _cancel_session(client, store, sessions, session)
        return

    if action == "add_issue":
        client.answer_callback_query(callback_id)
        session.stage = "issue_description"
        store.save_session(session)
        _set_review_message(client, store, session, "Hantar keterangan isu baharu.", _back_to_review_keyboard())
        return

    if action == "menu_fields":
        client.answer_callback_query(callback_id)
        _set_review_message(client, store, session, "Pilih butiran laporan yang mahu diubah:", _field_selection_keyboard())
        return

    if action == "menu_edit_issues":
        client.answer_callback_query(callback_id)
        _show_issue_selection_menu(client, store, session, "edit")
        return

    if action == "menu_delete_issues":
        client.answer_callback_query(callback_id)
        _show_issue_selection_menu(client, store, session, "delete")
        return

    if action == "select_field" and isinstance(value, str):
        client.answer_callback_query(callback_id)
        session.edit_field_key = value
        if value == "report_author":
            session.stage = "edit_author"
            store.save_session(session)
            client.send_message(
                session.chat_id,
                "Pilih penyedia laporan:",
                reply_markup=_author_reply_keyboard(back_to_review=True),
            )
            _set_review_message(
                client,
                store,
                session,
                "Pilih penyedia laporan menggunakan papan kekunci.",
                _back_to_review_keyboard(),
            )
            return

        session.stage = "edit_field"
        store.save_session(session)
        _set_review_message(
            client,
            store,
            session,
            f"Masukkan nilai baharu untuk {FIELD_LABELS[value]}.\n{FIELD_GUIDANCE[value]}",
            _back_to_review_keyboard(),
        )
        return

    if action == "select_edit_issue" and isinstance(value, int):
        client.answer_callback_query(callback_id)
        if value >= len(session.issues):
            _show_review(client, store, session, prefix=f"Isu {value + 1} tidak wujud.\n\n")
            return
        session.edit_issue_index = value
        session.stage = "edit_issue_description"
        store.save_session(session)
        _set_review_message(
            client,
            store,
            session,
            f"Masukkan keterangan baharu untuk isu {value + 1}.",
            _back_to_review_keyboard(),
        )
        return

    if action == "select_delete_issue" and isinstance(value, int):
        client.answer_callback_query(callback_id)
        if value >= len(session.issues):
            _show_review(client, store, session, prefix=f"Isu {value + 1} tidak wujud.\n\n")
            return
        removed = session.issues.pop(value)
        session.stage = "review"
        store.save_session(session)
        _show_review(client, store, session, prefix=f'Isu {value + 1} dibuang: "{removed.description}"\n\n')
        return

    client.answer_callback_query(callback_id)
    _show_review(client, store, session)


def _resume_draft(client: TelegramBotClient, store: DraftStore, session: Session, prefix: str = "") -> None:
    session.stage = "review"
    session.edit_field_key = None
    session.edit_issue_index = None
    store.save_session(session)
    _show_review(client, store, session, prefix=prefix)


def _enter_review(client: TelegramBotClient, store: DraftStore, session: Session) -> None:
    session.stage = "review"
    session.edit_field_key = None
    session.edit_issue_index = None
    store.save_session(session)
    _dismiss_reply_keyboard(client, session.chat_id)
    _show_review(client, store, session)


def _finish_report(client: TelegramBotClient, nextcloud: NextcloudClient, store: DraftStore, session: Session) -> int:
    report = ReportData(
        date=session.data["date"],
        project_name=session.data["project_name"],
        project_sub_name=session.data["project_sub_name"],
        report_title=session.data["report_title"],
        report_purpose=session.data["report_purpose"],
        report_author=session.data["report_author"],
        report_author_role=session.data["report_author_role"],
        issues=session.issues,
    )

    docx_path, pdf_path = _build_output_paths(session.workspace, report)
    render_report(TEMPLATE_PATH, docx_path, report)
    _convert_docx_to_pdf(docx_path, pdf_path)
    share = nextcloud.upload_and_share(pdf_path, pdf_path.name)
    revision_number = store.record_revision(
        draft_id=session.draft_id or 0,
        payload_json=_report_payload_json(report),
        remote_path=share.remote_path,
        share_id=share.share_id,
        share_url=share.share_url,
    )
    _delete_transient_outputs(docx_path, pdf_path)
    return revision_number


def _cancel_session(
    client: TelegramBotClient,
    store: DraftStore,
    sessions: dict[int, Session],
    session: Session,
) -> None:
    if session.draft_id is not None:
        store.delete_report(session.chat_id, session.draft_id)
    _delete_message_if_possible(client, session.chat_id, session.review_message_id)
    sessions.pop(session.chat_id, None)
    client.send_message(session.chat_id, "Laporan dipadam. Hantar /start untuk mula semula.")


def _show_drafts(client: TelegramBotClient, store: DraftStore, chat_id: int) -> None:
    drafts = store.list_reports(chat_id)
    if not drafts:
        client.send_message(chat_id, "Tiada laporan aktif. Hantar /start untuk mula baharu.")
        return
    client.send_message(chat_id, _drafts_text(drafts), reply_markup=_drafts_keyboard(drafts))


def _show_archived_reports(client: TelegramBotClient, store: DraftStore, chat_id: int) -> None:
    reports = store.list_archived_reports(chat_id)
    if not reports:
        client.send_message(chat_id, "Tiada laporan arkib.")
        return
    client.send_message(chat_id, _archived_reports_text(reports), reply_markup=_archived_reports_keyboard(reports))


def _drafts_text(drafts: list[DraftSummary]) -> str:
    lines = ["Laporan aktif:"]
    for draft in drafts:
        project = draft.project_name or "(belum diisi)"
        sub_project = draft.project_sub_name or "-"
        date = draft.date or "-"
        lines.append(f"R-{draft.draft_id} | {project} | {sub_project} | {date}")
    lines.append("")
    lines.append("Tekan butang Buka atau guna /edit <nombor laporan>.")
    return "\n".join(lines)


def _archived_reports_text(reports: list[DraftSummary]) -> str:
    lines = ["Laporan arkib:"]
    for report in reports:
        project = report.project_name or "(belum diisi)"
        sub_project = report.project_sub_name or "-"
        date = report.date or "-"
        lines.append(f"R-{report.draft_id} | {project} | {sub_project} | {date}")
    lines.append("")
    lines.append("Tekan butang Buka untuk lihat dan pulihkan laporan arkib.")
    return "\n".join(lines)


def _field_prompt(index: int) -> str:
    key, label, guidance = FIELDS[index]
    return f"{index + 1}/{len(FIELDS)}. Masukkan {label} ({key}).\n{guidance}"


def _review_text(session: Session) -> str:
    draft_label = _draft_label_for_session(session)
    status_line = "Status: Diarkibkan" if session.report_status == "archived" else "Status: Aktif"
    header_lines = [
        f"Semakan {draft_label}:",
        status_line,
        *[
            f"{index}. {label}: {session.data.get(key, '-')}"
            for index, (key, label, _) in enumerate(FIELDS, start=1)
        ],
        "",
        "Isu:",
    ]
    if session.issues:
        issue_lines = [
            f"{index}. {issue.description}"
            + (f" | Lampiran: {issue.images_description}" if issue.images_description else "")
            + f" ({len(issue.image_paths)} gambar)"
            for index, issue in enumerate(session.issues, start=1)
        ]
    else:
        issue_lines = ["Tiada isu."]
    return "\n".join(header_lines + issue_lines + ["", "Gunakan butang di bawah untuk semak, ubah, atau jana laporan."])


def _show_report_revisions(
    client: TelegramBotClient,
    store: DraftStore,
    session: Session,
    prefix: str = "",
) -> None:
    if session.draft_id is None:
        _show_review(client, store, session)
        return

    revisions = store.list_report_revisions(session.draft_id)
    if not revisions:
        _set_review_message(
            client,
            store,
            session,
            "Belum ada PDF revision untuk laporan ini.",
            _back_to_review_keyboard(),
        )
        return

    lines = [f"PDF revision untuk {_draft_label_for_session(session)}:"]
    for revision in revisions:
        lines.append(
            f"Revision {revision.revision_number} | {_revision_status_label(revision.status)} | {_format_timestamp(revision.created_at)}"
        )
    _set_review_message(
        client,
        store,
        session,
        f"{prefix}" + "\n".join(lines),
        _revision_keyboard(revisions),
    )


def _show_review(client: TelegramBotClient, store: DraftStore, session: Session, prefix: str = "") -> None:
    session.display_number = _draft_display_number(store, session.chat_id, session.draft_id)
    old_message_id = session.review_message_id
    result = client.send_message(
        session.chat_id,
        f"{prefix}{_review_text(session)}",
        reply_markup=_review_keyboard(session),
    )
    session.review_message_id = result["message_id"]
    store.save_session(session)

    if old_message_id and old_message_id != session.review_message_id:
        try:
            client.delete_message(session.chat_id, old_message_id)
        except Exception:
            LOGGER.debug("Failed to delete previous review message %s", old_message_id, exc_info=True)


def _set_review_message(
    client: TelegramBotClient,
    store: DraftStore,
    session: Session,
    text: str,
    reply_markup: dict | None,
) -> None:
    if session.review_message_id is None:
        result = client.send_message(session.chat_id, text, reply_markup=reply_markup)
        session.review_message_id = result["message_id"]
        store.save_session(session)
        return

    try:
        client.edit_message_text(session.chat_id, session.review_message_id, text, reply_markup=reply_markup)
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
        result = client.send_message(session.chat_id, text, reply_markup=reply_markup)
        session.review_message_id = result["message_id"]
        store.save_session(session)


def _review_keyboard(session: Session) -> dict:
    if session.report_status == "archived":
        return {
            "inline_keyboard": [
                [
                    _button("Lihat PDF", f"{REVIEW_CALLBACK_PREFIX}:show_revisions"),
                    _button("Pulih", f"{REVIEW_CALLBACK_PREFIX}:restore"),
                ],
                [_button("Padam Laporan", f"{REVIEW_CALLBACK_PREFIX}:delete_report")],
                [_button("Muat Semula", f"{REVIEW_CALLBACK_PREFIX}:show")],
            ]
        }

    rows = [
        [
            _button("Jana Laporan", f"{REVIEW_CALLBACK_PREFIX}:generate"),
            _button("Tambah Isu", f"{REVIEW_CALLBACK_PREFIX}:add_issue"),
        ],
        [_button("Edit Butiran", f"{REVIEW_CALLBACK_PREFIX}:menu_fields")],
    ]
    if session.issues:
        rows.append(
            [
                _button("Edit Isu", f"{REVIEW_CALLBACK_PREFIX}:menu_edit_issues"),
                _button("Padam Isu", f"{REVIEW_CALLBACK_PREFIX}:menu_delete_issues"),
            ]
        )
    rows.append(
        [
            _button("Lihat PDF", f"{REVIEW_CALLBACK_PREFIX}:show_revisions"),
            _button("Arkib", f"{REVIEW_CALLBACK_PREFIX}:archive"),
        ]
    )
    rows.append([_button("Padam Laporan", f"{REVIEW_CALLBACK_PREFIX}:delete_report")])
    rows.append([_button("Muat Semula", f"{REVIEW_CALLBACK_PREFIX}:show")])
    return {"inline_keyboard": rows}


def _field_selection_keyboard() -> dict:
    rows = [
        [_button(f"{index}. {label}", f"{REVIEW_CALLBACK_PREFIX}:field:{key}")]
        for index, (key, label, _) in enumerate(FIELDS, start=1)
    ]
    rows.append([_button("Kembali", f"{REVIEW_CALLBACK_PREFIX}:back")])
    return {"inline_keyboard": rows}


def _author_reply_keyboard(back_to_review: bool) -> dict:
    rows = [[{"text": name}] for name, _ in AUTHOR_OPTIONS]
    if back_to_review:
        rows.append([{"text": AUTHOR_BACK_LABEL}])
    return {"keyboard": rows, "resize_keyboard": True, "one_time_keyboard": True}


def _yes_no_reply_keyboard() -> dict:
    return {
        "keyboard": [[{"text": YES_LABEL}, {"text": NO_LABEL}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def _remove_reply_keyboard() -> dict:
    return {"remove_keyboard": True}


def _show_issue_selection_menu(client: TelegramBotClient, store: DraftStore, session: Session, mode: str) -> None:
    if not session.issues:
        _show_review(client, store, session, prefix="Tiada isu untuk dipilih.\n\n")
        return
    title = "Pilih isu yang mahu diubah:" if mode == "edit" else "Pilih isu yang mahu dibuang:"
    _set_review_message(client, store, session, title, _issue_selection_keyboard(session, mode))


def _issue_selection_keyboard(session: Session, mode: str) -> dict:
    action = "edit_issue" if mode == "edit" else "delete_issue"
    rows = []
    for index, issue in enumerate(session.issues, start=1):
        preview = issue.description.strip() or "(tanpa keterangan)"
        preview = preview[:36] + "..." if len(preview) > 36 else preview
        rows.append([_button(f"{index}. {preview}", f"{REVIEW_CALLBACK_PREFIX}:{action}:{index - 1}")])
    rows.append([_button("Kembali", f"{REVIEW_CALLBACK_PREFIX}:back")])
    return {"inline_keyboard": rows}


def _drafts_keyboard(drafts: list[DraftSummary]) -> dict:
    rows = [[_button(f"Buka R-{draft.draft_id}", f"{DRAFT_CALLBACK_PREFIX}:edit:{draft.draft_id}")] for draft in drafts]
    rows.append([_button("Muat Semula", f"{DRAFT_CALLBACK_PREFIX}:list")])
    return {"inline_keyboard": rows}


def _archived_reports_keyboard(reports: list[DraftSummary]) -> dict:
    rows = [[_button(f"Buka R-{report.draft_id}", f"{ARCHIVED_CALLBACK_PREFIX}:edit:{report.draft_id}")] for report in reports]
    rows.append([_button("Muat Semula", f"{ARCHIVED_CALLBACK_PREFIX}:list")])
    return {"inline_keyboard": rows}


def _back_to_review_keyboard() -> dict:
    return {"inline_keyboard": [[_button("Kembali ke Semakan", f"{REVIEW_CALLBACK_PREFIX}:back")]]}


def _button(text: str, callback_data: str) -> dict:
    return {"text": text, "callback_data": callback_data}


def _url_button(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def _match_author_option(text: str) -> tuple[str, str] | None:
    normalized = text.strip()
    for name, role in AUTHOR_OPTIONS:
        if normalized == name:
            return name, role
    return None


def _dismiss_reply_keyboard(client: TelegramBotClient, chat_id: int) -> None:
    try:
        result = client.send_message(chat_id, "\u2060", reply_markup=_remove_reply_keyboard())
        client.delete_message(chat_id, result["message_id"])
    except Exception:
        LOGGER.debug("Failed to dismiss reply keyboard for chat %s", chat_id, exc_info=True)


def _delete_message_if_possible(client: TelegramBotClient, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        client.delete_message(chat_id, message_id)
    except Exception:
        LOGGER.debug("Failed to delete message %s in chat %s", message_id, chat_id, exc_info=True)


def _ensure_persisted_session(store: DraftStore, session: Session) -> None:
    if session.draft_id is not None:
        return

    persisted = store.create_report(session.chat_id)
    session.draft_id = persisted.draft_id
    session.workspace = persisted.workspace
    store.save_session(session)


def _resolve_draft_by_number(store: DraftStore, chat_id: int, display_number: int) -> tuple[int, DraftSummary] | None:
    drafts = store.list_drafts(chat_id)
    if display_number < 1 or display_number > len(drafts):
        return None
    summary = drafts[display_number - 1]
    return summary.draft_id, summary


def _draft_display_number(store: DraftStore, chat_id: int, draft_id: int | None) -> int | None:
    if draft_id is None:
        return None
    drafts = store.list_drafts(chat_id)
    for number, draft in enumerate(drafts, start=1):
        if draft.draft_id == draft_id:
            return number
    return None


def _draft_label_for_session(session: Session) -> str:
    if session.draft_id is not None:
        ref = f"R-{session.draft_id}"
    else:
        ref = "laporan"
    if session.display_number is not None:
        return f"laporan {ref}"
    return f"laporan {ref}"


def _format_timestamp(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local_dt = dt.astimezone()
    return local_dt.strftime("%d/%m/%Y %H:%M")


def _revision_keyboard(revisions: list[GeneratedFileRecord]) -> dict:
    rows = []
    for revision in revisions:
        if revision.status == "available":
            rows.append([_url_button(f"Revision {revision.revision_number}", revision.share_url)])
        else:
            rows.append(
                [
                    _button(
                        f"Revision {revision.revision_number} (luput)",
                        f"{REVIEW_CALLBACK_PREFIX}:expired_revision:{revision.revision_number}",
                    )
                ]
            )
    rows.append([_button("Kembali", f"{REVIEW_CALLBACK_PREFIX}:back")])
    return {"inline_keyboard": rows}


def _revision_status_label(status: str) -> str:
    return {
        "available": "Tersedia",
        "expired": "Luput",
    }.get(status, status)


def _expired_revision_prefix(revision_number: int) -> str:
    return (
        f"Revision {revision_number} telah luput dan fail PDF itu sudah dipadam.\n"
        "Pilih revision lain yang masih tersedia, atau tekan Kembali untuk jana PDF baharu.\n\n"
    )


def _parse_callback_data(data: str) -> tuple[str, str | int | None]:
    parts = data.split(":")
    if len(parts) < 2:
        return "unknown", None

    prefix, action = parts[0], parts[1]
    if prefix == REVIEW_CALLBACK_PREFIX:
        if action in {"generate", "back", "show", "add_issue", "menu_fields", "menu_edit_issues", "menu_delete_issues", "show_revisions", "archive", "restore", "delete_report"}:
            return action, None
        if action == "expired_revision" and len(parts) >= 3 and parts[2].isdigit():
            return "expired_revision", int(parts[2])
        if action == "field" and len(parts) >= 3:
            return "select_field", parts[2]
        if action == "edit_issue" and len(parts) >= 3 and parts[2].isdigit():
            return "select_edit_issue", int(parts[2])
        if action == "delete_issue" and len(parts) >= 3 and parts[2].isdigit():
            return "select_delete_issue", int(parts[2])
        return "unknown", None

    if prefix == DRAFT_CALLBACK_PREFIX:
        if action == "list":
            return "draft_list", None
        if action == "edit" and len(parts) >= 3 and parts[2].isdigit():
            return "draft_edit", int(parts[2])

    if prefix == ARCHIVED_CALLBACK_PREFIX:
        if action == "list":
            return "archived_list", None
        if action == "edit" and len(parts) >= 3 and parts[2].isdigit():
            return "archived_edit", int(parts[2])

    return "unknown", None


def _extract_image_file(message: dict) -> tuple[str | None, str, int | None]:
    photos = message.get("photo") or []
    if photos:
        largest = photos[-1]
        return largest["file_id"], ".jpg", largest.get("file_size")
    document = message.get("document")
    if document and (document.get("mime_type") or "").startswith("image/"):
        extension = Path(document.get("file_name") or "image.bin").suffix or ".bin"
        return document["file_id"], extension, document.get("file_size")
    return None, "", None


def _count_total_images(session: Session) -> int:
    return sum(len(issue.image_paths) for issue in session.issues) + len(session.current_issue.image_paths)


def _build_output_name(report: ReportData, extension: str) -> str:
    date = sanitize_filename_part(report.date.replace("/", "-"))
    project = sanitize_filename_part(report.project_name)
    sub_project = sanitize_filename_part(report.project_sub_name)
    return f"initial-report-{date}-{project}-{sub_project}.{extension}"


def _build_output_paths(workspace: Path, report: ReportData) -> tuple[Path, Path]:
    docx_path = workspace / _build_output_name(report, "docx")
    pdf_path = workspace / _build_output_name(report, "pdf")
    return docx_path, pdf_path


def _convert_docx_to_pdf(docx_path: Path, pdf_path: Path) -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "libreoffice",
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(pdf_path.parent),
        str(docx_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "Unknown LibreOffice conversion error."
        raise RuntimeError(f"Failed to convert DOCX to PDF: {stderr}")
    generated_pdf = docx_path.with_suffix(".pdf")
    if not generated_pdf.exists():
        raise RuntimeError("LibreOffice conversion did not produce a PDF file.")
    if generated_pdf != pdf_path:
        generated_pdf.replace(pdf_path)


def _report_payload_json(report: ReportData) -> str:
    payload = {
        "date": report.date,
        "project_name": report.project_name,
        "project_sub_name": report.project_sub_name,
        "report_title": report.report_title,
        "report_purpose": report.report_purpose,
        "report_author": report.report_author,
        "report_author_role": report.report_author_role,
        "issues": [
            {
                "description": issue.description,
                "images_description": issue.images_description,
                "image_paths": [str(path) for path in issue.image_paths],
            }
            for issue in report.issues
        ],
    }
    return json.dumps(payload)


def _delete_transient_outputs(*paths: Path) -> None:
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            LOGGER.debug("Failed to delete transient output %s", path, exc_info=True)


def _run_housekeeping(
    store: DraftStore,
    nextcloud: NextcloudClient,
    retention_days: int,
    archived_report_retention_days: int,
    auto_archive_active_report_days: int,
) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    expired = store.list_expired_generated_files(cutoff)
    if not expired:
        LOGGER.debug("No expired revisions found")
    else:
        LOGGER.info("Running Nextcloud housekeeping for %s revision(s)", len(expired))
    for record in expired:
        try:
            if record.share_id:
                nextcloud.delete_share(record.share_id)
            nextcloud.delete_file(record.remote_path)
            store.mark_generated_file_deleted(record.record_id)
        except Exception:
            LOGGER.exception("Failed housekeeping for generated file record %s", record.record_id)

    if auto_archive_active_report_days > 0:
        archive_active_cutoff = (datetime.now(timezone.utc) - timedelta(days=auto_archive_active_report_days)).isoformat()
        archived_ids = store.auto_archive_stale_reports(archive_active_cutoff)
        if archived_ids:
            LOGGER.info("Auto-archived %s stale active report(s)", len(archived_ids))

    archive_cutoff = (datetime.now(timezone.utc) - timedelta(days=archived_report_retention_days)).isoformat()
    cleanup_targets = store.list_reports_for_asset_cleanup(archive_cutoff)
    for target in cleanup_targets:
        try:
            store.cleanup_report_assets(target.draft_id, target.workspace)
        except Exception:
            LOGGER.exception("Failed local asset cleanup for report %s", target.draft_id)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _load_nextcloud_client() -> NextcloudClient:
    base_url = os.getenv("NEXTCLOUD_BASE_URL", "").strip()
    username = os.getenv("NEXTCLOUD_USERNAME", "").strip()
    password = os.getenv("NEXTCLOUD_APP_PASSWORD", "").strip()
    upload_dir = os.getenv("NEXTCLOUD_UPLOAD_DIR", "InitialReports").strip()
    missing = [
        name
        for name, value in {
            "NEXTCLOUD_BASE_URL": base_url,
            "NEXTCLOUD_USERNAME": username,
            "NEXTCLOUD_APP_PASSWORD": password,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing Nextcloud config: {', '.join(missing)}")
    return NextcloudClient(base_url=base_url, username=username, password=password, upload_dir=upload_dir)


def _help_text() -> str:
    return (
        "Arahan bot:\n"
        "/start - mula laporan baharu\n"
        "/reports - senarai laporan aktif\n"
        "/archived - senarai laporan arkib\n"
        "/done - siap untuk langkah semasa\n"
        "/cancel - padam laporan semasa\n\n"
        "Format penting:\n"
        "- Tarikh: DD/MM/YYYY\n"
        "- Nama projek: teks ringkas\n"
        "- Sub-projek: teks ringkas\n"
        "- Tajuk laporan: teks ringkas\n"
        "- Tujuan laporan: ayat ringkas\n"
        "- Penyedia laporan: pilih daripada butang nama\n\n"
        "Nota isu:\n"
        "- Selepas keterangan isu, bot akan minta keterangan lampiran\n"
        "- Balas /skip jika tiada keterangan lampiran tambahan\n\n"
        "Had lalai:\n"
        f"- Max gambar per isu: {MAX_IMAGES_PER_ISSUE_DEFAULT}\n"
        f"- Max isu per laporan: {MAX_ISSUES_PER_REPORT_DEFAULT}\n"
        f"- Max jumlah gambar per laporan: {MAX_TOTAL_IMAGES_PER_REPORT_DEFAULT}\n"
        f"- Max saiz gambar: {MAX_IMAGE_FILE_SIZE_MB_DEFAULT} MB\n\n"
        "Semakan akhir menggunakan butang, bukan arahan teks. Setiap jana PDF akan mencipta revision baharu, dan laporan arkib boleh dilihat semula melalui /archived."
    )


def _is_valid_date(value: str) -> bool:
    return bool(re.fullmatch(r"(0[1-9]|[12][0-9]|3[01])/(0[1-9]|1[0-2])/\d{4}", value.strip()))


if __name__ == "__main__":
    main()
