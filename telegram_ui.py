from __future__ import annotations

from datetime import datetime, timezone

from bot_state import Session
from draft_store import DraftSummary, GeneratedFileRecord


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


def _help_text(
    max_images_per_issue_default: int,
    max_issues_per_report_default: int,
    max_total_images_per_report_default: int,
    max_image_file_size_mb_default: int,
) -> str:
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
        f"- Max gambar per isu: {max_images_per_issue_default}\n"
        f"- Max isu per laporan: {max_issues_per_report_default}\n"
        f"- Max jumlah gambar per laporan: {max_total_images_per_report_default}\n"
        f"- Max saiz gambar: {max_image_file_size_mb_default} MB\n\n"
        "Semakan akhir menggunakan butang, bukan arahan teks. Setiap jana PDF akan mencipta revision baharu, dan laporan arkib boleh dilihat semula melalui /archived."
    )
