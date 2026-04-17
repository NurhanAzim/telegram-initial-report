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
- Optional: `RETENTION_PERIOD_DAYS`

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
4. Send one or more images for that issue
5. Send `/done`
6. Use the keyboard buttons `Ya` or `Tidak` to continue or open review
7. In review, use the inline buttons in the chat:
   `Jana Laporan`
   `Tambah Isu`
   `Edit Butiran` then pick `1-6`
   `Edit Isu` then pick issue number `1..n`
   `Padam Isu` then pick issue number `1..n`
8. Reopen an unfinished draft later with `/drafts` or `/edit <id>`

If there is no issue at all, send `/done` when the bot asks for the first issue description.

## Telegram commands

The bot syncs these commands automatically on startup via `setMyCommands`, but if you want to register them manually in BotFather, use:

```text
start - Mula laporan baru
drafts - Senarai draf belum siap
edit - Buka draf ikut ID
done - Selesai untuk langkah semasa
cancel - Batal sesi semasa
help - Tunjuk panduan ringkas
```

## Persistence

- Drafts are stored in SQLite at `DATABASE_PATH`
- Saved draft assets are stored under `DRAFTS_DIR`
- Generated Nextcloud PDFs older than `RETENTION_PERIOD_DAYS` are deleted automatically

## Docker

Build and run with Docker Compose:

```bash
docker compose up -d --build
```

Persistent paths:

- `./data` for SQLite and saved draft assets
- `./runtime` for runtime scratch files

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
      "image_paths": ["./sample.png"]
    }
  ]
}
```

Render it:

```bash
python3 report_generator.py payload.json --output output/initial-report.docx
```
