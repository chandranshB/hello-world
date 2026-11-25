#!/usr/bin/env python3
"""
Throwaway SQLite-backed login page used to smoke-test the pipeline end to end.

It is deliberately injectable: the login handler concatenates the POSTed
user_id/pass straight into the query, which is exactly what sqlmap needs in
order to produce a real dump tree. Nothing here is meant to be deployed -- it
binds to loopback and exists so the sqlmap -> Parquet chain can be exercised
without pointing the tool at anything you do not own.

    python tests/lab_server.py --port 8080 --reset

    POST /app/login.php   user_id=<v>&pass=<v>&submit=login
"""

from __future__ import annotations

import argparse
import html
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

DEFAULT_DB = Path(__file__).resolve().parent / "lab.db"

SCHEMA = """
CREATE TABLE users (
    id       INTEGER PRIMARY KEY,
    username TEXT,
    password TEXT,
    email    TEXT,
    ssn      TEXT
);
CREATE TABLE orders (
    id      INTEGER PRIMARY KEY,
    user_id INTEGER,
    item    TEXT,
    amount  REAL,
    note    TEXT
);
CREATE TABLE notes (
    id   INTEGER PRIMARY KEY,
    body TEXT
);
"""

USERS = [
    (1, "admin", "s3cr3t-admin", "admin@lab.test", "078-05-1120"),
    (2, "alice", "wonderland", "alice@lab.test", "219-09-9999"),
    (3, "bob", "hunter2", "bob@lab.test", None),
    (4, "zoë", "pässwörd", "zoe@lab.test", "457-55-5462"),
    (5, "o'brien", "quote'd", "obrien@lab.test", "111-22-3333"),
]

ORDERS = [
    (1, 1, "widget", 19.99, "paid"),
    (2, 2, "gadget, deluxe", 149.50, "refunded"),
    (3, 3, "thing\nwith newline", 0.99, None),
    (4, 4, 'he said "hi"', 7.25, "paid"),
    (5, 5, "naïve café", 42.00, "pending"),
]

NOTES = [
    (1, "internal: rotate the lab keys"),
    (2, "NULL"),
    (3, "unicode ✓ and emoji 🚀"),
]


def seed(db_path: Path, reset: bool) -> None:
    if reset and db_path.exists():
        db_path.unlink()
    fresh = not db_path.exists()
    connection = sqlite3.connect(db_path)
    try:
        if fresh:
            connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO users VALUES (?,?,?,?,?)", USERS)
        connection.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", ORDERS)
        connection.executemany("INSERT INTO notes VALUES (?,?)", NOTES)
        connection.commit()
    finally:
        connection.close()


def make_handler(db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "lab/1.0"

        def log_message(self, fmt: str, *args) -> None:  # quieter console
            sys.stderr.write("lab %s\n" % (fmt % args))

        def _send(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self._send(
                200,
                "<html><body><h1>Lab login</h1>"
                '<form method="post" action="/app/login.php">'
                '<input name="user_id"><input name="pass" type="password">'
                '<input name="submit" type="submit" value="login"></form></body></html>',
            )

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace")
            fields = parse_qs(body)
            user_id = (fields.get("user_id") or [""])[0]
            password = (fields.get("pass") or [""])[0]

            # Intentionally concatenated: this is the injection point.
            query = (
                "SELECT id, username, email FROM users "
                f"WHERE username = '{user_id}' AND password = '{password}'"
            )

            connection = sqlite3.connect(db_path)
            try:
                rows = connection.execute(query).fetchall()
            except sqlite3.Error as exc:
                self._send(500, f"<html><body>SQL error: {html.escape(str(exc))}</body></html>")
                return
            finally:
                connection.close()

            if not rows:
                self._send(200, "<html><body>Login failed for that account.</body></html>")
                return

            cells = "".join(
                "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>"
                for row in rows
            )
            self._send(200, f"<html><body><h1>Welcome</h1><table>{cells}</table></body></html>")

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--reset", action="store_true", help="recreate the database before seeding")
    args = parser.parse_args(argv)

    seed(args.db, args.reset)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.db))
    print(f"lab listening on http://{args.host}:{args.port}/app/login.php (db {args.db})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
