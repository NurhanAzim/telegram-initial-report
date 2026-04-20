from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot_state import PendingIssue, Session
from draft_store import DraftStore, DraftSummary
from report_generator import Issue
from telegram_ui import (
    AUTHOR_BACK_LABEL,
    FIELD_GUIDANCE,
    FIELD_LABELS,
    FIELDS,
    NO_LABEL,
    YES_LABEL,
    _author_reply_keyboard,
    _field_prompt,
    _match_author_option,
    _remove_reply_keyboard,
    _yes_no_reply_keyboard,
)


@dataclass(frozen=True, slots=True)
class ConversationHooks:
    show_review: Callable[..., None]
    dismiss_reply_keyboard: Callable[..., None]


def _handle_edit_command(
    client: Any,
    store: DraftStore,
    sessions: dict[int, Session],
    chat_id: int,
    text: str,
    hooks: ConversationHooks,
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
    _resume_draft(client, store, session, hooks, prefix=f"Laporan #{report_number} dibuka.\n\n")


def _handle_field_input(client: Any, store: DraftStore, session: Session, text: str) -> None:
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
    client: Any,
    store: DraftStore,
    session: Session,
    text: str,
    max_issues_per_report: int,
    hooks: ConversationHooks,
) -> None:
    if text == "/done":
        _enter_report_action_flow(client, store, session, hooks)
        return
    if not text:
        client.send_message(session.chat_id, "Keterangan isu tidak boleh kosong.")
        return
    if len(session.issues) >= max_issues_per_report:
        _enter_report_action_flow(client, store, session, hooks)
        client.send_message(
            session.chat_id,
            f"Had isu per laporan telah dicapai ({max_issues_per_report}). Teruskan dengan bahagian tindakan.",
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


def _handle_issue_images_description(client: Any, store: DraftStore, session: Session, text: str) -> None:
    _ensure_persisted_session(store, session)
    normalized = text.strip()
    if normalized.lower() == "/skip":
        session.current_issue.images_description = ""
    else:
        session.current_issue.images_description = normalized

    session.stage = "issue_images"
    store.save_session(session)
    client.send_message(session.chat_id, "Hantar gambar untuk isu ini satu demi satu. Bila selesai, balas /done.")


def _handle_author_selection(
    client: Any,
    store: DraftStore,
    session: Session,
    text: str,
    hooks: ConversationHooks,
) -> None:
    normalized = text.strip()
    if session.stage == "edit_author" and normalized == AUTHOR_BACK_LABEL:
        session.stage = "review"
        session.edit_field_key = None
        store.save_session(session)
        hooks.show_review(client, store, session)
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
    hooks.show_review(client, store, session, prefix=f"Penyedia laporan telah dikemas kini kepada {author_name}.\n\n")


def _handle_issue_images(
    client: Any,
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
    client: Any,
    store: DraftStore,
    session: Session,
    text: str,
    max_issues_per_report: int,
    hooks: ConversationHooks,
) -> None:
    normalized = text.lower()
    if normalized in {YES_LABEL.lower(), "y", "yes"}:
        if len(session.issues) >= max_issues_per_report:
            _enter_report_action_flow(client, store, session, hooks)
            client.send_message(
                session.chat_id,
                f"Had isu per laporan telah dicapai ({max_issues_per_report}). Teruskan dengan bahagian tindakan.",
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
        _enter_report_action_flow(client, store, session, hooks)
        return

    client.send_message(
        session.chat_id,
        "Pilih Ya atau Tidak menggunakan papan kekunci yang disediakan.",
        reply_markup=_yes_no_reply_keyboard(),
    )


def _handle_edit_field(
    client: Any,
    store: DraftStore,
    session: Session,
    text: str,
    hooks: ConversationHooks,
) -> None:
    field_key = session.edit_field_key
    if not field_key:
        session.stage = "review"
        store.save_session(session)
        hooks.show_review(client, store, session)
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
    hooks.show_review(client, store, session, prefix=f"{FIELD_LABELS[field_key]} telah dikemas kini.\n\n")


def _handle_edit_issue_description(
    client: Any,
    store: DraftStore,
    session: Session,
    text: str,
    hooks: ConversationHooks,
) -> None:
    issue_index = session.edit_issue_index
    if issue_index is None or issue_index >= len(session.issues):
        session.edit_issue_index = None
        session.stage = "review"
        store.save_session(session)
        hooks.show_review(client, store, session)
        return

    if not text:
        client.send_message(session.chat_id, "Keterangan isu tidak boleh kosong.")
        return

    issue_number = issue_index + 1
    session.issues[issue_index].description = text
    session.edit_issue_index = None
    session.stage = "review"
    store.save_session(session)
    hooks.show_review(client, store, session, prefix=f"Keterangan isu {issue_number} telah dikemas kini.\n\n")


def _handle_report_action(client: Any, store: DraftStore, session: Session, text: str, hooks: ConversationHooks) -> None:
    if not text:
        client.send_message(session.chat_id, "Tindakan laporan tidak boleh kosong.")
        return

    _ensure_persisted_session(store, session)
    session.data["report_action"] = text
    store.save_session(session)
    _enter_report_conclusion_flow(client, store, session, hooks)


def _handle_report_conclusion(client: Any, store: DraftStore, session: Session, text: str, hooks: ConversationHooks) -> None:
    if not text:
        client.send_message(session.chat_id, "Kesimpulan laporan tidak boleh kosong.")
        return

    _ensure_persisted_session(store, session)
    session.data["report_conclusion"] = text
    store.save_session(session)
    _enter_review(client, store, session, hooks)


def _resume_draft(
    client: Any,
    store: DraftStore,
    session: Session,
    hooks: ConversationHooks,
    prefix: str = "",
) -> None:
    session.stage = "review"
    session.edit_field_key = None
    session.edit_issue_index = None
    session.delete_issue_index = None
    store.save_session(session)
    hooks.show_review(client, store, session, prefix=prefix)


def _enter_review(client: Any, store: DraftStore, session: Session, hooks: ConversationHooks) -> None:
    session.stage = "review"
    session.edit_field_key = None
    session.edit_issue_index = None
    session.delete_issue_index = None
    store.save_session(session)
    hooks.dismiss_reply_keyboard(client, session.chat_id)
    hooks.show_review(client, store, session)


def _enter_report_action_flow(client: Any, store: DraftStore, session: Session, hooks: ConversationHooks) -> None:
    if session.data.get("report_action"):
        _enter_report_conclusion_flow(client, store, session, hooks)
        return
    session.stage = "report_action"
    store.save_session(session)
    hooks.dismiss_reply_keyboard(client, session.chat_id)
    client.send_message(session.chat_id, "Masukkan tindakan yang diambil.")


def _enter_report_conclusion_flow(client: Any, store: DraftStore, session: Session, hooks: ConversationHooks) -> None:
    if session.data.get("report_conclusion"):
        _enter_review(client, store, session, hooks)
        return
    session.stage = "report_conclusion"
    store.save_session(session)
    hooks.dismiss_reply_keyboard(client, session.chat_id)
    client.send_message(session.chat_id, "Masukkan kesimpulan laporan.")


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


def _is_valid_date(value: str) -> bool:
    return bool(re.fullmatch(r"(0[1-9]|[12][0-9]|3[01])/(0[1-9]|1[0-2])/\d{4}", value.strip()))
