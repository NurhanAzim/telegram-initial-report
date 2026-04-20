from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from bot_state import Session
from draft_store import DraftStore
from nextcloud_client import NextcloudClient, sanitize_filename_part
from report_generator import ReportData, render_report
from telegram_flow import (
    ConversationHooks,
    _count_total_images,
    _ensure_persisted_session,
    _extract_image_file,
    _handle_author_selection,
    _handle_edit_command,
    _handle_edit_field,
    _handle_edit_issue_description,
    _handle_edit_issue_images_description,
    _handle_edit_issue_add_images,
    _handle_field_input,
    _handle_issue_description,
    _handle_issue_images,
    _handle_issue_images_description,
    _handle_more_issues,
    _handle_report_action,
    _handle_report_conclusion,
    _resume_draft,
)
from telegram_ui import (
    ARCHIVED_CALLBACK_PREFIX,
    AUTHOR_BACK_LABEL,
    AUTHOR_OPTIONS,
    BOT_COMMANDS,
    DRAFT_CALLBACK_PREFIX,
    FIELD_GUIDANCE,
    FIELD_LABELS,
    FIELDS,
    NO_LABEL,
    REVIEW_CALLBACK_PREFIX,
    YES_LABEL,
    _archived_reports_keyboard,
    _archived_reports_text,
    _author_reply_keyboard,
    _back_to_review_keyboard,
    _draft_label_for_session,
    _drafts_keyboard,
    _drafts_text,
    _delete_issue_confirmation_keyboard,
    _delete_report_confirmation_keyboard,
    _expired_revision_prefix,
    _field_prompt,
    _field_selection_keyboard,
    _format_timestamp,
    _help_text,
    _issue_edit_options_keyboard,
    _issue_image_selection_keyboard,
    _issue_selection_keyboard,
    _match_author_option,
    _parse_callback_data,
    _remove_reply_keyboard,
    _revision_keyboard,
    _revision_status_label,
    _review_keyboard,
    _review_text,
    _yes_no_reply_keyboard,
)


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

__all__ = [
    "AUTHOR_BACK_LABEL",
    "AUTHOR_OPTIONS",
    "NO_LABEL",
    "YES_LABEL",
    "_archived_reports_keyboard",
    "_archived_reports_text",
    "_author_reply_keyboard",
    "_build_output_paths",
    "_count_total_images",
    "_drafts_keyboard",
    "_drafts_text",
    "_ensure_persisted_session",
    "_extract_image_file",
    "_field_selection_keyboard",
    "_issue_selection_keyboard",
    "_match_author_option",
    "_parse_callback_data",
    "_remove_reply_keyboard",
    "_revision_keyboard",
    "_review_keyboard",
    "_review_text",
    "_yes_no_reply_keyboard",
]


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
                    retention_days,
                    max_images_per_issue,
                    max_issues_per_report,
                    max_total_images_per_report,
                    max_image_file_size_bytes,
                    archived_report_retention_days,
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
    retention_days: int,
    max_images_per_issue: int,
    max_issues_per_report: int,
    max_total_images_per_report: int,
    max_image_file_size_bytes: int,
    archived_report_retention_days: int,
) -> None:
    callback_query = update.get("callback_query")
    if callback_query:
        _handle_callback_query(client, nextcloud, store, callback_query, sessions, archived_report_retention_days)
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
        _show_archived_reports(client, store, chat_id, archived_report_retention_days)
        return

    if text.startswith("/edit"):
        _handle_edit_command(client, store, sessions, chat_id, text, CONVERSATION_HOOKS)
        return

    if text == "/help":
        client.send_message(
            chat_id,
            _help_text(
                MAX_IMAGES_PER_ISSUE_DEFAULT,
                MAX_ISSUES_PER_REPORT_DEFAULT,
                MAX_TOTAL_IMAGES_PER_REPORT_DEFAULT,
                MAX_IMAGE_FILE_SIZE_MB_DEFAULT,
                retention_days,
                archived_report_retention_days,
            ),
        )
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
        _handle_author_selection(client, store, session, text, CONVERSATION_HOOKS)
        return

    if session.stage == "issue_description":
        _handle_issue_description(client, store, session, text, max_issues_per_report, CONVERSATION_HOOKS)
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
        _handle_more_issues(client, store, session, text, max_issues_per_report, CONVERSATION_HOOKS)
        return

    if session.stage == "report_action":
        _handle_report_action(client, store, session, text, CONVERSATION_HOOKS)
        return

    if session.stage == "report_conclusion":
        _handle_report_conclusion(client, store, session, text, CONVERSATION_HOOKS)
        return

    if session.stage == "review":
        client.send_message(chat_id, "Semakan menggunakan butang. Gunakan panel semakan yang dihantar bot.")
        return

    if session.stage == "edit_field":
        _handle_edit_field(client, store, session, text, CONVERSATION_HOOKS)
        return

    if session.stage == "edit_author":
        _handle_author_selection(client, store, session, text, CONVERSATION_HOOKS)
        return

    if session.stage == "edit_issue_description":
        _handle_edit_issue_description(client, store, session, text, CONVERSATION_HOOKS)
        return

    if session.stage == "edit_issue_images_description":
        _handle_edit_issue_images_description(client, store, session, text, CONVERSATION_HOOKS)
        return

    if session.stage == "edit_issue_add_images":
        _handle_edit_issue_add_images(
            client,
            store,
            session,
            message,
            text,
            max_images_per_issue,
            max_total_images_per_report,
            max_image_file_size_bytes,
            CONVERSATION_HOOKS,
        )
        return

    client.send_message(chat_id, "Keadaan sesi tidak dikenali. Hantar /cancel dan mula semula.")

def _handle_callback_query(
    client: TelegramBotClient,
    nextcloud: NextcloudClient,
    store: DraftStore,
    callback_query: dict,
    sessions: dict[int, Session],
    archived_report_retention_days: int,
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
        _resume_draft(client, store, session, CONVERSATION_HOOKS, prefix=f"Laporan #{draft_number} dibuka.\n\n")
        return

    if action == "archived_edit" and isinstance(value, int):
        session = store.load_report_with_status(
            chat_id,
            value,
            statuses=("archived",),
            archived_visible_cutoff_iso=_archived_visible_cutoff_iso(archived_report_retention_days),
        )
        if session is None:
            client.answer_callback_query(callback_id, "Laporan arkib tidak dijumpai.")
            client.send_message(chat_id, "Laporan arkib itu tidak lagi wujud.")
            return
        client.answer_callback_query(callback_id, f"Membuka R-{value}...")
        _delete_message_if_possible(client, chat_id, message.get("message_id"))
        sessions[chat_id] = session
        _resume_draft(client, store, session, CONVERSATION_HOOKS, prefix=f"Laporan arkib R-{value} dibuka.\n\n")
        return

    if action == "draft_list":
        client.answer_callback_query(callback_id)
        _show_drafts(client, store, chat_id)
        return

    if action == "archived_list":
        client.answer_callback_query(callback_id)
        _show_archived_reports(client, store, chat_id, archived_report_retention_days)
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
        session.delete_issue_index = None
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
        client.answer_callback_query(callback_id)
        session.stage = "confirm_delete_report"
        session.delete_issue_index = None
        store.save_session(session)
        _set_review_message(
            client,
            store,
            session,
            "Anda pasti mahu padam laporan ini?\nTindakan ini tidak boleh dibatalkan.",
            _delete_report_confirmation_keyboard(),
        )
        return

    if action == "confirm_delete_report":
        client.answer_callback_query(callback_id, "Memadam laporan...")
        _cancel_session(client, store, sessions, session)
        return

    if action == "cancel_delete_report":
        client.answer_callback_query(callback_id)
        session.stage = "review"
        session.delete_issue_index = None
        store.save_session(session)
        _show_review(client, store, session, prefix="Padam laporan dibatalkan.\n\n")
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
        session.stage = "review"
        session.delete_issue_index = None
        store.save_session(session)
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
        _show_issue_edit_menu(client, store, session, value)
        return

    if action == "edit_issue_description" and isinstance(value, int):
        client.answer_callback_query(callback_id)
        if value >= len(session.issues):
            _show_review(client, store, session, prefix=f"Isu {value + 1} tidak wujud.\n\n")
            return
        issue = session.issues[value]
        session.edit_issue_index = value
        session.stage = "edit_issue_description"
        store.save_session(session)
        _set_review_message(
            client,
            store,
            session,
            f"Masukkan keterangan baharu untuk isu {value + 1}.\nSemasa: {issue.description}",
            _back_to_review_keyboard(),
        )
        return

    if action == "edit_issue_images_description" and isinstance(value, int):
        client.answer_callback_query(callback_id)
        if value >= len(session.issues):
            _show_review(client, store, session, prefix=f"Isu {value + 1} tidak wujud.\n\n")
            return
        issue = session.issues[value]
        current_value = issue.images_description or "(kosong)"
        session.edit_issue_index = value
        session.stage = "edit_issue_images_description"
        store.save_session(session)
        _set_review_message(
            client,
            store,
            session,
            f"Masukkan keterangan lampiran baharu untuk isu {value + 1}. Balas /skip untuk kosongkan.\nSemasa: {current_value}",
            _back_to_review_keyboard(),
        )
        return

    if action == "edit_issue_add_image" and isinstance(value, int):
        client.answer_callback_query(callback_id)
        if value >= len(session.issues):
            _show_review(client, store, session, prefix=f"Isu {value + 1} tidak wujud.\n\n")
            return
        session.edit_issue_index = value
        session.stage = "edit_issue_add_images"
        store.save_session(session)
        _set_review_message(
            client,
            store,
            session,
            f"Hantar gambar baharu untuk isu {value + 1}. Balas /done apabila selesai.",
            _back_to_review_keyboard(),
        )
        return

    if action == "menu_remove_issue_image" and isinstance(value, int):
        client.answer_callback_query(callback_id)
        _show_issue_image_selection_menu(client, store, session, value)
        return

    if action == "select_delete_issue" and isinstance(value, int):
        client.answer_callback_query(callback_id)
        if value >= len(session.issues):
            _show_review(client, store, session, prefix=f"Isu {value + 1} tidak wujud.\n\n")
            return
        session.stage = "confirm_delete_issue"
        session.delete_issue_index = value
        store.save_session(session)
        _set_review_message(
            client,
            store,
            session,
            f'Anda pasti mahu padam isu {value + 1}?\n"{session.issues[value].description}"',
            _delete_issue_confirmation_keyboard(value),
        )
        return

    if action == "confirm_delete_issue" and isinstance(value, int):
        client.answer_callback_query(callback_id)
        if session.delete_issue_index != value or value >= len(session.issues):
            session.stage = "review"
            session.delete_issue_index = None
            store.save_session(session)
            _show_review(client, store, session, prefix="Isu itu tidak lagi tersedia untuk dipadam.\n\n")
            return
        removed = session.issues.pop(value)
        session.stage = "review"
        session.delete_issue_index = None
        store.save_session(session)
        _show_review(client, store, session, prefix=f'Isu {value + 1} dibuang: "{removed.description}"\n\n')
        return

    if action == "cancel_delete_issue":
        client.answer_callback_query(callback_id)
        session.stage = "review"
        session.delete_issue_index = None
        store.save_session(session)
        _show_issue_selection_menu(client, store, session, "delete", prefix="Padam isu dibatalkan.\n\n")
        return

    if action == "remove_issue_image" and isinstance(value, tuple) and len(value) == 2:
        client.answer_callback_query(callback_id)
        issue_index, image_index = value
        if issue_index >= len(session.issues):
            _show_review(client, store, session, prefix=f"Isu {issue_index + 1} tidak wujud.\n\n")
            return
        issue = session.issues[issue_index]
        if image_index >= len(issue.image_paths):
            _show_issue_image_selection_menu(client, store, session, issue_index, prefix="Gambar itu tidak lagi wujud.\n\n")
            return
        removed_path = issue.image_paths.pop(image_index)
        try:
            if removed_path.exists():
                removed_path.unlink()
        except Exception:
            LOGGER.debug("Failed to delete removed issue image %s", removed_path, exc_info=True)
        store.save_session(session)
        if issue.image_paths:
            _show_issue_image_selection_menu(
                client,
                store,
                session,
                issue_index,
                prefix=f"Gambar {removed_path.name} telah dipadam.\n\n",
            )
        else:
            _show_issue_edit_menu(
                client,
                store,
                session,
                issue_index,
                prefix=f"Gambar {removed_path.name} telah dipadam. Tiada gambar lagi untuk isu ini.\n\n",
            )
        return

    client.answer_callback_query(callback_id)
    _show_review(client, store, session)

def _finish_report(client: TelegramBotClient, nextcloud: NextcloudClient, store: DraftStore, session: Session) -> int:
    report = ReportData(
        date=session.data["date"],
        project_name=session.data["project_name"],
        project_sub_name=session.data["project_sub_name"],
        report_title=session.data["report_title"],
        report_purpose=session.data["report_purpose"],
        report_action=session.data.get("report_action", ""),
        report_conclusion=session.data.get("report_conclusion", ""),
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


def _show_archived_reports(
    client: TelegramBotClient,
    store: DraftStore,
    chat_id: int,
    archived_report_retention_days: int,
) -> None:
    reports = store.list_archived_reports(
        chat_id,
        visible_cutoff_iso=_archived_visible_cutoff_iso(archived_report_retention_days),
    )
    if not reports:
        client.send_message(chat_id, "Tiada laporan arkib.")
        return
    client.send_message(chat_id, _archived_reports_text(reports), reply_markup=_archived_reports_keyboard(reports))


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


def _show_issue_selection_menu(
    client: TelegramBotClient,
    store: DraftStore,
    session: Session,
    mode: str,
    prefix: str = "",
) -> None:
    if not session.issues:
        _show_review(client, store, session, prefix="Tiada isu untuk dipilih.\n\n")
        return
    title = "Pilih isu yang mahu diubah:" if mode == "edit" else "Pilih isu yang mahu dibuang:"
    _set_review_message(client, store, session, f"{prefix}{title}", _issue_selection_keyboard(session, mode))


def _show_issue_edit_menu(
    client: TelegramBotClient,
    store: DraftStore,
    session: Session,
    issue_index: int,
    prefix: str = "",
) -> None:
    if issue_index >= len(session.issues):
        _show_review(client, store, session, prefix=f"Isu {issue_index + 1} tidak wujud.\n\n")
        return
    _set_review_message(
        client,
        store,
        session,
        f"{prefix}Pilih bahagian yang mahu diubah untuk isu {issue_index + 1}:",
        _issue_edit_options_keyboard(issue_index),
    )


def _show_issue_image_selection_menu(
    client: TelegramBotClient,
    store: DraftStore,
    session: Session,
    issue_index: int,
    prefix: str = "",
) -> None:
    if issue_index >= len(session.issues):
        _show_review(client, store, session, prefix=f"Isu {issue_index + 1} tidak wujud.\n\n")
        return
    issue = session.issues[issue_index]
    if not issue.image_paths:
        _show_issue_edit_menu(client, store, session, issue_index, prefix=f"{prefix}Tiada gambar untuk dipadam.\n\n")
        return
    _set_review_message(
        client,
        store,
        session,
        f"{prefix}Pilih gambar yang mahu dipadam daripada isu {issue_index + 1}:",
        _issue_image_selection_keyboard(issue_index, [str(path) for path in issue.image_paths]),
    )


def _dismiss_reply_keyboard(client: TelegramBotClient, chat_id: int) -> None:
    try:
        result = client.send_message(chat_id, "\u2060", reply_markup=_remove_reply_keyboard())
        client.delete_message(chat_id, result["message_id"])
    except Exception:
        LOGGER.debug("Failed to dismiss reply keyboard for chat %s", chat_id, exc_info=True)


CONVERSATION_HOOKS = ConversationHooks(
    show_review=_show_review,
    dismiss_reply_keyboard=_dismiss_reply_keyboard,
)


def _delete_message_if_possible(client: TelegramBotClient, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        client.delete_message(chat_id, message_id)
    except Exception:
        LOGGER.debug("Failed to delete message %s in chat %s", message_id, chat_id, exc_info=True)


def _archived_visible_cutoff_iso(archived_report_retention_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=archived_report_retention_days)).isoformat()

def _draft_display_number(store: DraftStore, chat_id: int, draft_id: int | None) -> int | None:
    if draft_id is None:
        return None
    drafts = store.list_drafts(chat_id)
    for number, draft in enumerate(drafts, start=1):
        if draft.draft_id == draft_id:
            return number
    return None


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
        "report_action": report.report_action,
        "report_conclusion": report.report_conclusion,
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

if __name__ == "__main__":
    main()
