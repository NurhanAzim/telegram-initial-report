# Initial Report Automation

Fastest working path for this folder:

- Keep the existing `Template Initial Report.docx`.
- Generate the report with `python-docx`, which is already available here.
- Run a simple Telegram long-polling bot that collects text + images, converts the final report to PDF, uploads that PDF to Nextcloud, and sends the share link back.

This repo now contains:

- `report_generator.py` to render the template.
- `telegram_bot.py` to collect the report fields via Telegram.
- `draft_store.py` to persist draft/session state in SQLite.
- `nextcloud_client.py` to upload the generated PDF and create a public share URL.
- `tests/test_report_generator.py` for a local render regression test.

## Why this route

This template already uses stable placeholders like `<date>`, `<project_name>`, and `<issue_description>`.
Because those placeholders are already embedded in the DOCX, the shortest route is to replace them directly and preserve the table formatting already inside the file.

Longer-term, if you want richer loops/formatting rules inside Word, convert the template to Jinja-style tags and move to `docxtpl`.

## Setup

1. Ensure Python 3.13+ is available.
2. Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

3. Edit `.env`:

- `TELEGRAM_BOT_TOKEN`
- `NEXTCLOUD_BASE_URL`
- `NEXTCLOUD_USERNAME`
- `NEXTCLOUD_APP_PASSWORD`
- Optional: `NEXTCLOUD_UPLOAD_DIR`
- Optional: `DATA_DIR`
- Optional: `RUNTIME_DIR`
- Optional: `DATABASE_PATH`
- Optional: `DRAFTS_DIR`
- Optional: `BACKUP_DIR`
- Optional: `RETENTION_PERIOD_DAYS`
- Optional: `ARCHIVED_REPORT_RETENTION_DAYS`
- Optional: `AUTO_ARCHIVE_ACTIVE_REPORT_DAYS`
- Optional: `MAX_IMAGES_PER_ISSUE`
- Optional: `MAX_ISSUES_PER_REPORT`
- Optional: `MAX_TOTAL_IMAGES_PER_REPORT`
- Optional: `MAX_IMAGE_FILE_SIZE_MB`

For staging, copy `.env.staging.example` to `.env.staging` or edit the existing local `.env.staging`.

4. Start the bot:

```bash
python3 telegram_bot.py
```

## Telegram flow

1. Send `/start`
2. Reply with the requested report fields in the shown format:
   `date`: `DD/MM/YYYY` such as `16/04/2026`
   `project_name`: short project name
   `project_sub_name`: short phase or sub-project name
   `report_title`: short report title
   `report_purpose`: short purpose sentence
   `report_author`: choose from the keyboard buttons
3. Send one issue description
4. Optionally send attachment description text for that issue, or send `/skip`
5. Send one or more images for that issue
6. Send `/done`
7. Use the keyboard buttons `Ya` or `Tidak` to continue or open review
8. In review, use the inline buttons in the chat:
   `Jana Laporan`
   `Lihat PDF`
   `Arkib`
   `Padam Laporan`
   `Tambah Isu`
   `Edit Butiran` then pick `1-6`
   `Edit Isu` then pick issue number `1..n`
   `Padam Isu` then pick issue number `1..n`
9. After generation, the bot opens the PDF revision view directly
10. Reopen an active report later with `/reports` or the `Buka R-<id>` button
11. View archived reports later with `/archived`

If there is no issue at all, send `/done` when the bot asks for the first issue description.

## Telegram commands

The bot syncs these commands automatically on startup via `setMyCommands`, but if you want to register them manually in BotFather, use:

```text
start - Mula laporan baru
reports - Senarai laporan aktif
archived - Senarai laporan arkib
done - Selesai untuk langkah semasa
cancel - Padam laporan semasa
help - Tunjuk panduan ringkas
```

## Persistence

- Active reports are stored in SQLite at `DATABASE_PATH`
- Local report source assets are stored under `DRAFTS_DIR`
- Database backups are stored under `BACKUP_DIR`
- Generated PDF revisions older than `RETENTION_PERIOD_DAYS` are deleted automatically from Nextcloud
- Active reports with at least one generated revision can be auto-archived after `AUTO_ARCHIVE_ACTIVE_REPORT_DAYS` of inactivity; `0` disables it
- Archived or deleted report assets become eligible for local cleanup after `ARCHIVED_REPORT_RETENTION_DAYS`

## Guardrails

- `MAX_IMAGES_PER_ISSUE` limits image count on a single issue
- `MAX_ISSUES_PER_REPORT` limits issue count in one report
- `MAX_TOTAL_IMAGES_PER_REPORT` limits image count across the whole report
- `MAX_IMAGE_FILE_SIZE_MB` limits accepted image size before download

## Report Lifecycle

- A report stays editable after PDF generation
- Each PDF generation creates a new immutable revision
- `/reports` shows active editable reports
- `/archived` shows archived reports
- Reports leave the active list only when archived or deleted
- Archived reports can be restored back to active
- Old PDF revisions can expire while the editable report remains
- Expired revisions stay visible in the revision list with a clear recovery message

## Migrations

- SQLite schema migrations are tracked in the `schema_migrations` table
- Pending migrations are applied automatically on startup
- If a non-empty database needs migration, the app creates a backup in `BACKUP_DIR` first
- Draft JSON state loading is backward-compatible for missing keys via defaults in the loader

## Docker

Build and run with Docker Compose:

```bash
docker compose up -d --build
```

Run staging with the separate staging bot token and data paths:

```bash
docker compose -f docker-compose.staging.yml up -d --build
```

Persistent paths:

- `./data` for SQLite and saved draft assets
- `./runtime` for runtime scratch files
- `./data-staging` for staging SQLite and saved draft assets
- `./runtime-staging` for staging runtime scratch files

## Backup

Manual SQLite backup:

```bash
./scripts/backup_db.sh
```

Or specify explicit paths:

```bash
./scripts/backup_db.sh data/bot.db data/backups
```

## Local render test

```bash
python3 -m unittest tests/test_report_generator.py tests/test_telegram_bot.py tests/test_draft_store.py
```

## CLI render example

Create a payload JSON:

```json
{
  "date": "16/04/2026",
  "project_name": "Projek Demo",
  "project_sub_name": "Fasa 1",
  "report_title": "Server Room",
  "report_purpose": "Pemeriksaan awal",
  "report_author": "MUHAMMAD ADAM BIN JAFFRY",
  "report_author_role": "DEVOPS ENGINEER",
  "issues": [
    {
      "description": "Kabel belum dirapikan",
      "images_description": "Foto susulan tapak",
      "image_paths": ["./sample.png"]
    }
  ]
}
```

Render it:

```bash
python3 report_generator.py payload.json --output output/initial-report.docx
```
