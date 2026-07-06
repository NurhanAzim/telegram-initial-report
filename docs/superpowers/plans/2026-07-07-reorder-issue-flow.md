# Reorder Issue Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorder the per-issue entry flow from (tambah isu → butiran isu → lampiran) to (tambah isu → lampiran → butiran isu) by swapping transition targets and moving issue finalization to the last step.

**Architecture:** Swap transitions only — keep existing stage names (`issue_images`, `issue_images_description`) and handler functions. Move the issue-finalization logic (appending `current_issue` to `session.issues` and resetting it) from `_handle_issue_images` to `_handle_issue_images_description`, since the description step is now last. No data model, storage, or dispatcher changes.

**Tech Stack:** Python 3, python-telegram-bot, SQLite (DraftStore), unittest

**Spec:** `docs/superpowers/specs/2026-07-07-reorder-issue-flow-design.md`

---

## File Structure

- **Modify:** `telegram_flow.py` — three handler functions get transition/prompt/finalization changes
- **Modify:** `tests/test_telegram_bot.py` — add tests for new transition sequence and no-photo edge case
- **No changes:** `telegram_bot.py` (dispatcher routes by stage name, unchanged), `bot_state.py`, `draft_store.py`, `telegram_ui.py`, `report_generator.py`

---

### Task 1: Write failing test for new transition `issue_description` → `issue_images`

**Files:**
- Modify: `tests/test_telegram_bot.py` (add new test class after line 682)

- [ ] **Step 1: Write the failing test**

Add this import and test class at the end of `tests/test_telegram_bot.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_telegram_bot.py::IssueFlowOrderTest::test_issue_description_transitions_to_issue_images -v`
Expected: FAIL — `session.stage` is `"issue_images_description"` not `"issue_images"`, and prompt contains "keterangan lampiran" not "gambar"

- [ ] **Step 3: Implement the transition change in `_handle_issue_description`**

In `telegram_flow.py`, find the `_handle_issue_description` function (line ~127-134). Change the transition target and prompt:

**Before (lines 127-134):**
```python
    _ensure_persisted_session(store, session)
    session.current_issue = PendingIssue(description=text)
    session.stage = "issue_images_description"
    store.save_session(session)
    client.send_message(
        session.chat_id,
        "Masukkan keterangan lampiran untuk isu ini jika perlu. Jika tiada, balas /skip.",
    )
```

**After:**
```python
    _ensure_persisted_session(store, session)
    session.current_issue = PendingIssue(description=text)
    session.stage = "issue_images"
    store.save_session(session)
    client.send_message(
        session.chat_id,
        "Hantar gambar untuk isu ini satu demi satu. Bila selesai, balas /done.",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_telegram_bot.py::IssueFlowOrderTest::test_issue_description_transitions_to_issue_images -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_telegram_bot.py telegram_flow.py
git commit -m "feat: transition issue_description → issue_images (lampiran first)"
```

---

### Task 2: Write failing test for new transition `issue_images` → `issue_images_description` (no finalization)

**Files:**
- Modify: `tests/test_telegram_bot.py` (add to `IssueFlowOrderTest` class)

- [ ] **Step 1: Write the failing test**

Add this method to the `IssueFlowOrderTest` class in `tests/test_telegram_bot.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_telegram_bot.py::IssueFlowOrderTest::test_issue_images_done_transitions_to_images_description_without_finalizing -v`
Expected: FAIL — `session.stage` is `"more_issues"` not `"issue_images_description"`, issue is finalized into `session.issues`, and prompt says "Tambah isu lain?"

- [ ] **Step 3: Implement the transition change in `_handle_issue_images`**

In `telegram_flow.py`, find the `_handle_issue_images` function, the `/done` branch (lines 206-223). Remove finalization, change transition and prompt:

**Before (lines 206-223):**
```python
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
```

**After:**
```python
    if text == "/done":
        _ensure_persisted_session(store, session)
        session.stage = "issue_images_description"
        store.save_session(session)
        client.send_message(
            session.chat_id,
            "Masukkan keterangan lampiran untuk isu ini jika perlu. Jika tiada, balas /skip.",
        )
        return
```

Note: The `Issue` import at the top of `telegram_flow.py` (line 11) may become temporarily unused after this change. **Leave the import in place** — Task 3 re-adds `Issue(...)` construction in `_handle_issue_images_description`. Do not remove it.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_telegram_bot.py::IssueFlowOrderTest::test_issue_images_done_transitions_to_images_description_without_finalizing -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_telegram_bot.py telegram_flow.py
git commit -m "feat: transition issue_images /done → issue_images_description (no finalization)"
```

---

### Task 3: Write failing test for new transition `issue_images_description` → `more_issues` (with finalization)

**Files:**
- Modify: `tests/test_telegram_bot.py` (add to `IssueFlowOrderTest` class)

- [ ] **Step 1: Write the failing test**

Add this method to the `IssueFlowOrderTest` class in `tests/test_telegram_bot.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_telegram_bot.py::IssueFlowOrderTest -v -k "finalizes"`
Expected: FAIL — `session.stage` is `"issue_images"` not `"more_issues"`, issue is NOT finalized (still in `current_issue`), no yes/no keyboard

- [ ] **Step 3: Implement finalization and transition in `_handle_issue_images_description`**

In `telegram_flow.py`, find the `_handle_issue_images_description` function (lines 137-147). Add finalization logic, change transition and prompt:

**Before (lines 137-147):**
```python
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
```

**After:**
```python
def _handle_issue_images_description(client: Any, store: DraftStore, session: Session, text: str) -> None:
    _ensure_persisted_session(store, session)
    normalized = text.strip()
    if normalized.lower() == "/skip":
        session.current_issue.images_description = ""
    else:
        session.current_issue.images_description = normalized

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_telegram_bot.py::IssueFlowOrderTest -v -k "finalizes"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_telegram_bot.py telegram_flow.py
git commit -m "feat: finalize issue at images_description step, transition to more_issues"
```

---

### Task 4: Write test for no-photo edge case

**Files:**
- Modify: `tests/test_telegram_bot.py` (add to `IssueFlowOrderTest` class)

- [ ] **Step 1: Write the test**

Add this method to the `IssueFlowOrderTest` class in `tests/test_telegram_bot.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_telegram_bot.py::IssueFlowOrderTest::test_no_photos_still_asks_for_description -v`
Expected: PASS — this should already pass from the changes in Task 2, since the `/done` branch always transitions to `issue_images_description` regardless of photo count. This test confirms the edge case is handled.

- [ ] **Step 3: Commit**

```bash
git add tests/test_telegram_bot.py
git commit -m "test: verify no-photo edge case still asks for description"
```

---

### Task 5: Run full test suite and verify no regressions

**Files:**
- None (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL tests pass, including:
- All existing `TelegramBotReviewTest` tests (review, edit, delete flows — unaffected)
- All new `IssueFlowOrderTest` tests (new transition sequence)

- [ ] **Step 2: Check for unused imports**

Run: `python -c "import telegram_flow"` (should not error)
Then grep for `Issue` usage in `telegram_flow.py`:
Run: `grep -n "Issue(" telegram_flow.py`
Expected: `Issue(` should still appear (in the new `_handle_issue_images_description` finalization code). If it does not appear anywhere, remove the `from report_generator import Issue` import at line 11.

- [ ] **Step 3: Final commit if any cleanup was needed**

```bash
git add -A
git commit -m "chore: cleanup unused imports after flow reorder"
```
(Only if Step 2 found and removed unused imports. Otherwise skip this step.)
