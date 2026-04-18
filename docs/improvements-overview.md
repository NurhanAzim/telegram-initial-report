# Improvements Overview

This document keeps the main improvement areas for the Telegram Initial Report bot visible in one place.
It is intentionally broad and directional, not a delivery roadmap.

## Purpose

The app is already usable for a small internal workflow:

- Telegram bot intake
- review and editing flow
- SQLite draft persistence
- PDF generation
- Nextcloud link delivery
- staging/production separation

The items below describe where the system can be strengthened over time as usage, sensitivity, and operational expectations increase.

## Security

### Current strengths

- secrets are loaded from environment variables
- production and staging can be separated
- generated files have retention cleanup
- author values are constrained to approved options

### Current concerns

- the bot currently assumes any user who can reach it may use it
- generated Nextcloud links are public links
- uploads are accepted without explicit file-size or count limits
- local draft assets can accumulate on disk
- runtime logs may still expose more operational detail than necessary

### Improvement themes

- restrict access by allowed Telegram chat IDs or user IDs
- add file upload size limits
- add image count limits per issue
- add issue count limits per report
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
- `draft_store.py` handles persistence
- `report_generator.py` handles DOCX rendering
- `nextcloud_client.py` handles storage and share creation
- `bot_state.py` defines session state

### Current concerns

- `telegram_bot.py` is becoming the main accumulation point for behavior
- serialized `state_json` in SQLite is effectively a second schema
- review flow, callback flow, and conversation flow all live in one module

### Improvement themes

- split Telegram logic into smaller modules by concern
- version or more formally manage serialized draft state if it keeps evolving
- make job/state transitions more explicit over time

## UI / UX

### Current strengths

- mixed use of inline buttons and reply keyboard is appropriate
- author selection is constrained and easy
- review is available before generation
- draft reopening improves continuity
- attachment description is now separated from image layout

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
- consider draft duplication or “generate another from this draft”
- keep the review card visible and minimize chat noise

## Data and Migrations

### Current strengths

- SQLite migrations are tracked
- startup applies pending migrations automatically
- backup-before-migrate exists for non-empty databases

### Current concerns

- SQL schema is versioned, but draft `state_json` is still an evolving implicit schema
- local draft assets are not yet cleaned up with the same discipline as generated Nextcloud outputs

### Improvement themes

- keep migrations additive and explicit
- keep old draft JSON loadable through safe defaults
- add local draft asset retention or cleanup rules

## Operations

### Current strengths

- Docker deployment exists
- staging configuration exists
- manual backup script exists
- retention cleanup exists

### Current concerns

- no richer health/reporting surface yet
- no explicit alerting for repeated Telegram or Nextcloud failures
- no formal cleanup policy for local draft folders

### Improvement themes

- add operational health checks or lightweight status signals
- add local disk cleanup policies
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
