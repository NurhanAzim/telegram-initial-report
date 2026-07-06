# Design: Reorder Issue Flow (tambah isu → lampiran → butiran isu)

**Date:** 2026-07-07
**Status:** Approved
**Codebase:** `/home/nohan/Code/FHSB/initial-doc-automation` (Python Telegram bot)

## Goal

Reorder the per-issue entry flow from:

1. `issue_description` — tambah isu (enter issue description)
2. `issue_images_description` — butiran isu (enter attachment description, or `/skip`)
3. `issue_images` — lampiran (upload photos, then `/done`)

to:

1. `issue_description` — tambah isu (unchanged)
2. `issue_images` — lampiran (moved up)
3. `issue_images_description` — butiran isu (moved down)

## Approach

**Swap transitions only (minimal change).** Keep the existing stage names and handler functions. Change which stage each handler transitions to, and move the issue-finalization logic from the image handler to the description handler (since the description step is now last).

Rejected alternatives:
- *Rename stages to match new order* — larger blast radius, higher risk, no functional benefit.
- *Config-driven ordered steps* — over-engineering for a fixed 3-step flow.

## Changes

### 1. Transition order swap (all in `telegram_flow.py`)

| Handler | Current transition | New transition |
|---|---|---|
| `_handle_issue_description` (line ~127) | → `issue_images_description` | → `issue_images` |
| `_handle_issue_images` (line ~216, on `/done`) | → `more_issues` | → `issue_images_description` |
| `_handle_issue_images_description` (line ~145) | → `issue_images` | → `more_issues` |

### 2. Move finalization logic

Currently `_handle_issue_images` finalizes the issue on `/done`: appends `current_issue` to `session.issues`, resets `current_issue` to empty `PendingIssue`, then transitions to `more_issues`.

In the new flow, finalization moves to `_handle_issue_images_description` (the last step). The image handler transitions to the description step without finalizing.

### 3. Prompt updates

| After step | Current prompt | New prompt |
|---|---|---|
| `issue_description` | "Masukkan keterangan lampiran untuk isu ini jika perlu. Jika tiada, balas /skip." | "Hantar gambar untuk isu ini satu demi satu. Bila selesai, balas /done." |
| `issue_images` (`/done`) | "Tambah isu lain?" + yes/no keyboard | "Masukkan keterangan lampiran untuk isu ini jika perlu. Jika tiada, balas /skip." |
| `issue_images_description` | "Hantar gambar untuk isu ini satu demi satu. Bila selesai, balas /done." | "Tambah isu lain?" + yes/no keyboard |

### 4. Dispatcher (`telegram_bot.py`)

No changes. The dispatcher checks `session.stage` and routes to the correct handler. Stage names stay the same, so routing works unchanged.

### 5. Edge case: no photos uploaded

If the user sends `/done` at the image step with zero photos, the bot still transitions to `issue_images_description` and asks for the attachment description. The `/skip` option remains available there. This keeps the flow consistent and allows text-only notes.

### 6. Tests

Add tests in `tests/test_telegram_bot.py` for:
- New transition sequence: `issue_description` → `issue_images` → `issue_images_description` → `more_issues`
- No-photo edge case: `/done` with zero photos still asks for description
- Finalization happens at the description step, not the image step

## What Does NOT Change

- `PendingIssue` / `Issue` data models (`bot_state.py`, `report_generator.py`)
- `draft_store.py` serialization
- Review screen edit menu (independent edits, order-agnostic)
- `_handle_more_issues` loop-back logic (Ya → `issue_description`, Tidak → `_enter_report_action_flow`)
- `/done` shortcut in `issue_description` to skip all issues
- Image count limits and validation (`max_images_per_issue`, `max_total_images_per_report`, `max_image_file_size_bytes`)
- `_enter_report_action_flow` / `_enter_report_conclusion_flow` / `_enter_review` exit flow

## Risk Assessment

- **Low risk:** Only transition targets and prompt strings change. No data model, storage, or routing changes.
- **Finalization relocation** is the most delicate part — must ensure `current_issue` is appended to `session.issues` and reset exactly once, at the new final step.
- **Existing tests** for review/edit/delete flows remain valid since they operate on finalized `session.issues`, not the entry flow.
