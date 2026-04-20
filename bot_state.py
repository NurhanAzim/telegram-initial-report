from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from report_generator import Issue


@dataclass(slots=True)
class PendingIssue:
    description: str = ""
    images_description: str = ""
    image_paths: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class Session:
    chat_id: int
    draft_id: int | None = None
    display_number: int | None = None
    list_message_id: int | None = None
    report_status: str = "active"
    field_index: int = 0
    data: dict[str, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    current_issue: PendingIssue = field(default_factory=PendingIssue)
    stage: str = "field"
    edit_field_key: str | None = None
    edit_issue_index: int | None = None
    delete_issue_index: int | None = None
    review_message_id: int | None = None
    workspace: Path = Path("runtime")
