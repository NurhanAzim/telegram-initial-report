from __future__ import annotations

import hmac
import json
import logging
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from sqlite3 import Connection
from typing import Callable
from urllib.parse import urlparse

LOGGER = logging.getLogger(__name__)

PEOPLE_KINDS = ("author", "reviewer")
ERROR_RING_MAXLEN = 50
DEFAULT_PORT = 8080

READ_ROUTES = {
    ("GET", "/health"),
    ("GET", "/status"),
    ("GET", "/errors"),
    ("GET", "/space"),
}

MAX_SIDECARE_FAILURES = 5


class ControlInterface:
    """In-process management sidecar for one bot.

    Exposes a stdlib HTTP server (background thread) with read endpoints open on the
    internal network and config endpoints gated by a shared token. Holds an in-memory
    ring of recent errors and a dedicated SQLite connection for `people` mutations.
    """

    def __init__(
        self,
        db_path: Path,
        drafts_dir: Path,
        token: str,
        *,
        seed_people: list[tuple[str, str]] | None = None,
        port: int = DEFAULT_PORT,
        quota_fn: Callable[[], int | None] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._drafts_dir = Path(drafts_dir)
        self._token = token
        self._seed_people = seed_people or []
        self._port = port
        self._quota_fn = quota_fn
        self._db_lock = threading.Lock()
        self._conn = _open_connection(self._db_path)
        self._errors: deque[dict] = deque(maxlen=ERROR_RING_MAXLEN)
        self._stop_event = threading.Event()
        self._disabled_event = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._ensure_people_table()
        self._seed_if_empty()

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve_loop, name="control-sidecar", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # pragma: no cover - best effort
                LOGGER.debug("Sidecar shutdown raised", exc_info=True)

    @property
    def disabled(self) -> bool:
        return self._disabled_event.is_set()

    def _serve_loop(self) -> None:
        failures = 0
        while not self._stop_event.is_set():
            try:
                self._server = ThreadingHTTPServer(("0.0.0.0", self._port), self._make_handler())
                self._server.serve_forever()
                break
            except Exception as exc:  # noqa: BLE001 - survive any socket/OS error
                failures += 1
                LOGGER.exception("Sidecar HTTP server failed: %s", exc)
                if failures >= MAX_SIDECARE_FAILURES:
                    self._disabled_event.set()
                    LOGGER.error("Sidecar disabled after %s failures; polling continues.", failures)
                    break
                time.sleep(1)

    # ---- error capture --------------------------------------------------

    def record_error(self, exc: BaseException, traceback_text: str | None = None) -> None:
        try:
            self._errors.append(
                {
                    "ts": time.time(),
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback_text or "",
                }
            )
        except Exception:  # pragma: no cover - hook must never raise
            pass

    # ---- people CRUD -----------------------------------------------------

    def list_people(self, kind: str | None = None) -> list[dict]:
        with self._db_lock:
            if kind:
                rows = self._conn.execute(
                    "SELECT id, name, role, kind, active FROM people WHERE kind = ? ORDER BY id", (kind,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, name, role, kind, active FROM people ORDER BY id"
                ).fetchall()
            return [
                {**dict(row), "active": bool(row["active"])} for row in rows
            ]

    def add_person(self, name: str, role: str, kind: str) -> int:
        name = (name or "").strip().upper()
        if not name:
            raise ValueError("name is required")
        if kind not in PEOPLE_KINDS:
            raise ValueError("kind must be 'author' or 'reviewer'")
        with self._db_lock:
            cursor = self._conn.execute(
                "INSERT INTO people (name, role, kind, active) VALUES (?, ?, ?, 1)",
                (name, (role or "").strip().upper(), kind),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def update_person(self, person_id: int, name: str, role: str) -> None:
        name = (name or "").strip().upper()
        if not name:
            raise ValueError("name is required")
        with self._db_lock:
            self._conn.execute(
                "UPDATE people SET name = ?, role = ? WHERE id = ?",
                (name, (role or "").strip().upper(), person_id),
            )
            self._conn.commit()

    def set_active(self, person_id: int, active: bool) -> None:
        with self._db_lock:
            self._conn.execute(
                "UPDATE people SET active = ? WHERE id = ?",
                (1 if active else 0, person_id),
            )
            self._conn.commit()

    def remove_person(self, person_id: int) -> None:
        with self._db_lock:
            self._conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
            self._conn.commit()

    # ---- read models -----------------------------------------------------

    def status(self) -> dict:
        with self._db_lock:
            counts = {}
            for status in ("active", "archived", "deleted"):
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM drafts WHERE status = ?", (status,)
                ).fetchone()
                counts[status] = row["c"]
            revision_row = self._conn.execute(
                "SELECT COALESCE(SUM(current_revision), 0) AS r FROM drafts"
            ).fetchone()
        return {
            "active_reports": counts["active"],
            "archived_reports": counts["archived"],
            "deleted_reports": counts["deleted"],
            "total_revisions": revision_row["r"],
            "people": self.list_people(),
        }

    def space_usage(self) -> dict:
        database_bytes = self._db_path.stat().st_size if self._db_path.exists() else 0
        drafts_bytes = sum(
            f.stat().st_size for f in self._drafts_dir.rglob("*") if f.is_file()
        )
        remote_bytes = None
        if self._quota_fn is not None:
            try:
                remote_bytes = self._quota_fn()
            except Exception:  # pragma: no cover - quota is best-effort
                LOGGER.debug("Remote quota lookup failed", exc_info=True)
        return {
            "database_bytes": database_bytes,
            "drafts_bytes": drafts_bytes,
            "remote_bytes": remote_bytes,
        }

    def recent_errors(self) -> list[dict]:
        return list(self._errors)

    # ---- internals -------------------------------------------------------

    def _ensure_people_table(self) -> None:
        with self._db_lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS people (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self._conn.commit()

    def _seed_if_empty(self) -> None:
        if not self._seed_people:
            return
        with self._db_lock:
            count = self._conn.execute("SELECT COUNT(*) AS c FROM people").fetchone()["c"]
            if count == 0:
                for name, role in self._seed_people:
                    self._conn.execute(
                        "INSERT INTO people (name, role, kind) VALUES (?, ?, 'author')",
                        (name.strip().upper(), (role or "").strip().upper()),
                    )
                self._conn.commit()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        interface = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ControlSidecar/1.0"

            def log_message(self, *_args: object) -> None:
                return  # silence default stderr logging

            def _send(self, code: int, payload: object) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _token_valid(self) -> bool:
                header = self.headers.get("Authorization", "")
                provided = header.replace("Bearer ", "", 1).strip()
                if not provided or not interface._token:
                    return False
                return hmac.compare_digest(provided, interface._token)

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    return {}
                return json.loads(self.rfile.read(length).decode() or "{}")

            def _dispatch_read(self, path: str) -> dict:
                if path == "/health":
                    return {"status": "ok"}
                if path == "/status":
                    return interface.status()
                if path == "/errors":
                    return {"errors": interface.recent_errors()}
                if path == "/space":
                    return interface.space_usage()
                raise KeyError(path)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if (self.command, path) in READ_ROUTES:
                    try:
                        self._send(200, self._dispatch_read(path))
                    except Exception as exc:  # noqa: BLE001
                        self._send(500, {"error": str(exc)})
                    return
                if path == "/people":
                    if not self._token_valid():
                        self._send(401, {"error": "unauthorized"})
                        return
                    try:
                        kind = urlparse(self.path).query
                        kind_param = (
                            dict(
                                (k, v[0])
                                for k, v in (
                                    q.split("=") for q in kind.split("&") if "=" in q
                                )
                            ).get("kind")
                            if kind
                            else None
                        )
                        self._send(200, {"people": interface.list_people(kind_param)})
                    except Exception as exc:  # noqa: BLE001
                        self._send(400, {"error": str(exc)})
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if path == "/people":
                    if not self._token_valid():
                        self._send(401, {"error": "unauthorized"})
                        return
                    try:
                        data = self._read_json()
                        person_id = interface.add_person(
                            data.get("name", ""), data.get("role", ""), data.get("kind", "")
                        )
                        self._send(201, {"id": person_id})
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)})
                    except Exception as exc:  # noqa: BLE001
                        self._send(500, {"error": str(exc)})
                    return
                self._send(404, {"error": "not found"})

            def do_PUT(self) -> None:
                path = urlparse(self.path).path
                if path.startswith("/people/") and path.endswith("/active"):
                    if not self._token_valid():
                        self._send(401, {"error": "unauthorized"})
                        return
                    try:
                        person_id = int(path.split("/")[-2])
                        data = self._read_json()
                        active = bool(data.get("active", True))
                        interface.set_active(person_id, active)
                        self._send(200, {"id": person_id, "active": active})
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)})
                    except Exception as exc:  # noqa: BLE001
                        self._send(500, {"error": str(exc)})
                    return
                if path.startswith("/people/"):
                    if not self._token_valid():
                        self._send(401, {"error": "unauthorized"})
                        return
                    try:
                        person_id = int(path.rsplit("/", 1)[-1])
                        data = self._read_json()
                        interface.update_person(
                            person_id, data.get("name", ""), data.get("role", "")
                        )
                        self._send(200, {"id": person_id})
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)})
                    except Exception as exc:  # noqa: BLE001
                        self._send(500, {"error": str(exc)})
                    return
                self._send(404, {"error": "not found"})

            def do_DELETE(self) -> None:
                path = urlparse(self.path).path
                if path.startswith("/people/"):
                    if not self._token_valid():
                        self._send(401, {"error": "unauthorized"})
                        return
                    try:
                        person_id = int(path.rsplit("/", 1)[-1])
                        interface.remove_person(person_id)
                        self._send(200, {"id": person_id})
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)})
                    except Exception as exc:  # noqa: BLE001
                        self._send(500, {"error": str(exc)})
                    return
                self._send(404, {"error": "not found"})

        return Handler


def _open_connection(db_path: Path) -> Connection:
    import sqlite3

    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection
