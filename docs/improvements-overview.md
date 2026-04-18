# Improvements Overview

This document keeps the main improvement areas for the Telegram Initial Report bot visible in one place.
It is intentionally broad and directional, not a delivery roadmap.

## Purpose

The app is already usable for a small internal workflow:

- Telegram bot intake
- review and editing flow
- editable reports with immutable PDF revisions
- SQLite persistence with migrations
- PDF generation
- Nextcloud link delivery
- archived report flow
- upload and report-size guardrails
- staging/production separation

The items below describe where the system can be strengthened over time as usage, sensitivity, and operational expectations increase.

## Security

### Current strengths

- secrets are loaded from environment variables
- production and staging can be separated
- generated files have retention cleanup
- author values are constrained to approved options
- intake now has configurable size/count guardrails

### Current concerns

- the bot currently assumes any user who can reach it may use it
- generated Nextcloud links are public links
- link forwarding remains possible because delivery is still via public link
- runtime logs may still expose more operational detail than necessary

### Improvement themes

- restrict access by allowed Telegram chat IDs or user IDs
- preferred direction: Telegram allowlist plus public Nextcloud link delivery, because it gives better user experience across devices without requiring an active Nextcloud login session
- keep Nextcloud group-only or user-only sharing as a stricter alternative if report sensitivity later demands file-level authenticated access
- if public links remain the primary model, strengthen them with smaller retention windows and possibly share expiry if needed
- tighten production secret handling beyond plain `.env` files when deploying to a server

## Scale

### Current model

The app is designed for a low-volume, single-instance deployment:

- one Telegram bot process
- one SQLite database
- one local runtime/draft asset store
- synchronous PDF conversion and network calls

### Practical implications

- this is fine for internal team usage with modest concurrency
- it is not designed for multiple active writer replicas
- it should not share one SQLite file across multiple live bot instances

### Improvement themes

- move PDF generation into a background job if concurrency grows
- introduce explicit generation job states such as `generating`, `generated`, `failed`
- consider Postgres if multi-instance writes or higher concurrency are needed

## Performance

### Current strengths

- SQLite operations are small and simple
- report rendering is straightforward
- image layout logic is deterministic
- long polling is easy to operate
- transient DOCX/PDF outputs are removed after successful upload

### Current concerns

- LibreOffice PDF conversion is blocking and relatively heavy
- all Telegram and Nextcloud calls are synchronous
- housekeeping runs in the main process

### Improvement themes

- offload PDF generation to a worker if response time becomes an issue
- isolate slow external operations from the main bot loop
- add simple metrics or timing logs around conversion and uploads

## Architecture

### Current strengths

The app already has useful separation of concerns:

- `telegram_bot.py` handles flow orchestration
- `telegram_ui.py` now holds Telegram text, keyboard, and callback-shape helpers
- `telegram_flow.py` now holds the conversation state machine and intake/edit transitions
- `draft_store.py` handles persistence
- `report_generator.py` handles DOCX rendering
- `nextcloud_client.py` handles storage and share creation
- `bot_state.py` defines session state
- report revisions are now treated as immutable outputs instead of terminal report states
- local source assets are explicitly tracked in SQLite

### Current concerns

- callback routing, generation flow, and housekeeping still accumulate in `telegram_bot.py`
- serialized `state_json` in SQLite is effectively a second schema
- runtime behavior still depends on careful status preservation across active and archived reports

### Improvement themes

- continue shrinking Telegram orchestration into smaller modules by concern
- version or more formally manage serialized draft state if it keeps evolving
- make job/state transitions more explicit over time

## UI / UX

### Current strengths

- mixed use of inline buttons and reply keyboard is appropriate
- author selection is constrained and easy
- review is available before generation
- report reopening improves continuity
- attachment description is now separated from image layout
- archived reports can now be browsed and restored
- generated PDFs now open directly into the revision view
- expired revisions now remain visible with clear recovery messaging
- archived reports now stay archived when reopened and browsed

### Current concerns

- some critical steps still depend on text commands such as `/done` and `/skip`
- review editing is not yet symmetrical across all issue subfields
- some Telegram client behaviors around message deletion and keyboard removal may vary

### Improvement themes

- replace `/skip` with buttons where practical
- improve issue-level editing for:
  - issue description
  - attachment description
  - image add/remove actions
- consider report duplication or “generate another from this report”
- keep the review card visible and minimize chat noise

## Data and Migrations

### Current strengths

- SQLite migrations are tracked
- startup applies pending migrations automatically
- backup-before-migrate exists for non-empty databases
- reports remain editable after generation
- revision history is stored separately from current editable report state
- local report assets now have an explicit table for cleanup
- optional inactivity-based auto-archive now exists without adding a new schema migration
- archived session saves now preserve archived status by default instead of silently flipping back to active

### Current concerns

- SQL schema is versioned, but draft/report `state_json` is still an evolving implicit schema

### Improvement themes

- keep migrations additive and explicit
- keep old draft JSON loadable through safe defaults
- keep report asset tracking and cleanup aligned with report lifecycle rules
- keep optional inactivity-based auto-archive narrow and predictable

## Operations

### Current strengths

- Docker deployment exists
- staging configuration exists
- manual backup script exists
- retention cleanup exists
- archived/deleted report assets now participate in cleanup flow
- stale active reports can now be auto-archived by housekeeping when explicitly enabled

### Current concerns

- no richer health/reporting surface yet
- no explicit alerting for repeated Telegram or Nextcloud failures
- no surfaced operator view for expired revisions or stale reports beyond the bot UX

### Improvement themes

- add operational health checks or lightweight status signals
- add richer operational visibility for revision expiry and archive volume
- document restore/recovery procedures more explicitly as production usage grows

## Guiding Principle

The app should stay simple for as long as possible.

Improvements should preserve the current strengths:

- easy to operate
- easy to understand
- easy to deploy
- easy to recover

Where possible, prefer:

- bounded guardrails over complex policy engines
- clear operational defaults over broad configurability
- small modular refactors over large rewrites
