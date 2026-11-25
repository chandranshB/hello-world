#!/usr/bin/env python3
"""
Self-test for the sqlmap -> Parquet pipeline.

Builds a synthetic sqlmap dump tree with the awkward cases real dumps contain
(quoted commas, embedded newlines, quotes, unicode, sqlmap's literal NULL,
header-only tables, zero-byte files, duplicate column names, CP1252 bytes),
then exercises the converter, the verifier, the DuckDB catalog and the
allowlist logic end to end.

Run: python tests/selftest.py
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import preflight  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def write_csv(path: Path, rows: list[list[str]], header: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def build_fixtures(root: Path) -> None:
    shop = root / "app.test" / "dump" / "shop"

    write_csv(
        shop / "users.csv",
        [
            ["1", "Alice, A.", 'he said "hi"'],
            ["2", "Bob\nNewline", ""],
            ["3", "Zoë", "NULL"],
            ["4", "", "  spaced  "],
            ["5", "O'Brien; DROP TABLE", "x,y\nz"],
        ],
        ["id", "name", "note"],
    )

    orders_rows = [[str(i), f"{(i % 1000) / 100:.2f}", "true" if i % 2 else "false"] for i in range(200_000)]
    write_csv(shop / "orders.csv", orders_rows, ["id", "amount", "paid"])

    write_csv(shop / "dup.csv", [["1", "2", "3", "4"]], ["id", "id", "", "name"])

    cp_path = shop / "legacy.csv"
    cp_path.write_bytes("name,city\ncafé,Zürich\n".encode("cp1252"))

    write_csv(shop / "empty_table.csv", [], ["a", "b", "c"])
    (shop / "zero_bytes.csv").write_bytes(b"")

    write_csv(
        shop / "course_coordinator.csv",
        [
            ["1", "101", "2024-12-10 10:00:00", "F01"],
            ["2", "102", "2025-05-20 14:30:00", "F02"],
            ["3", "103", "2026-02-15 09:00:00", "F03"],
            ["4", "104", "2026-07-22 18:00:00", "F04"],
        ],
        ["sem", "ccode", "date1", "fcode"],
    )

    # a table that lives directly under dump/ (no db directory)
    write_csv(root / "app.test" / "dump" / "loose.csv", [["7"]], ["value"])


def run(args: list[str], expect: int = 0) -> subprocess.CompletedProcess:
    completed = subprocess.run([sys.executable, *args], capture_output=True, text=True)
    if completed.returncode != expect:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
    check(f"exit {expect}: {' '.join(Path(a).name if a.startswith(str(ROOT)) else a for a in args[:3])}",
          completed.returncode == expect, f"got {completed.returncode}")
    return completed


def test_conversion(tmp: Path) -> None:
    print("conversion")
    dump_root = tmp / "fixtures"
    out = tmp / "parquet"
    summary = out / "summary.md"
    build_fixtures(dump_root)

    run(
        [
            str(SCRIPTS / "csv_to_parquet.py"),
            "--dump-root", str(dump_root),
            "--out", str(out),
            "--workers", "4",
            "--infer-types",
            "--checksum",
            "--catalog",
            "--summary-md", str(summary),
        ]
    )

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    check("manifest tables", manifest["totals"]["files"] == 7, str(manifest["totals"]))
    check("manifest skipped zero-byte", any(e["status"] == "skipped" for e in manifest["files"]))
    check("summary markdown written", summary.exists() and "users" in summary.read_text(encoding="utf-8"))

    coordinator = pq.read_table(out / "shop" / "course_coordinator.parquet")
    check("course_coordinator filtered to >2025", coordinator.num_rows == 2, str(coordinator.num_rows))

    users = pq.read_table(out / "shop" / "users.parquet")
    check("users rows", users.num_rows == 5, str(users.num_rows))
    check("users columns", users.column_names == ["id", "name", "note"], str(users.column_names))
    values = users.to_pydict()
    check("quoted comma preserved", values["name"][0] == "Alice, A.", repr(values["name"][0]))
    check("embedded newline preserved", values["name"][1] == "Bob\nNewline", repr(values["name"][1]))
    check("quotes preserved", values["note"][0] == 'he said "hi"', repr(values["note"][0]))
    check("unicode preserved", values["name"][2] == "Zoë", repr(values["name"][2]))
    check("sqlmap NULL kept as text", values["note"][2] == "NULL", repr(values["note"][2]))
    check("empty string kept", values["note"][1] == "", repr(values["note"][1]))
    check("whitespace kept", values["note"][3] == "  spaced  ", repr(values["note"][3]))
    check("int inferred", users.schema.field("id").type == pa.int64(), str(users.schema.field("id").type))

    orders = pq.read_table(out / "shop" / "orders.parquet")
    check("orders rows", orders.num_rows == 200_000, str(orders.num_rows))
    check("float inferred", orders.schema.field("amount").type == pa.float64(), str(orders.schema.field("amount").type))
    check("bool inferred", orders.schema.field("paid").type == pa.bool_(), str(orders.schema.field("paid").type))
    check("float round-trip", orders.to_pydict()["amount"][123] == 1.23, str(orders.to_pydict()["amount"][123]))

    dup = pq.read_table(out / "shop" / "dup.parquet")
    check("duplicate header deduped", dup.column_names == ["id", "id_2", "column_3", "name"], str(dup.column_names))

    legacy = pq.read_table(out / "shop" / "legacy.parquet").to_pydict()
    check("cp1252 decoded", legacy["name"] == ["café"], str(legacy["name"]))

    empty = pq.read_table(out / "shop" / "empty_table.parquet")
    check("header-only table", empty.num_rows == 0 and empty.num_columns == 3, str(empty.schema))

    loose = pq.read_table(out / "_" / "loose.parquet")
    check("dump-root table placed", loose.num_rows == 1, str(loose.num_rows))

    run([str(SCRIPTS / "verify_parquet.py"), "--manifest", str(out / "manifest.json"), "--checksum", "--duckdb"])

    # sqlmap NULL token -> real NULL
    out2 = tmp / "parquet-null"
    run(
        [
            str(SCRIPTS / "csv_to_parquet.py"),
            "--dump-root", str(dump_root),
            "--out", str(out2),
            "--workers", "1",
            "--null-token", "NULL",
        ]
    )
    notes = pq.read_table(out2 / "shop" / "users.parquet").to_pydict()["note"]
    check("NULL token mapped to None", notes[2] is None, repr(notes[2]))
    check("empty string survives without inference", notes[1] == "", repr(notes[1]))

    if (out / "catalog.duckdb").exists():
        import duckdb

        connection = duckdb.connect(str(out / "catalog.duckdb"), read_only=True)
        count = connection.execute('SELECT count(*) FROM "shop"."orders"').fetchone()[0]
        check("duckdb view row count", count == 200_000, str(count))
        inventory = connection.execute("SELECT count(*) FROM _inventory").fetchone()[0]
        check("duckdb inventory", inventory == 7, str(inventory))
        connection.close()


def test_preflight(tmp: Path) -> None:
    print("preflight")
    allow = tmp / "allow.txt"
    allow.write_text("# comment\n127.0.0.1:8080\n*.staging.example.com\n10.0.0.0/8\n", encoding="utf-8")
    entries = preflight.read_allowlist(allow)
    check("allowlist parsed", entries == ["127.0.0.1:8080", "*.staging.example.com", "10.0.0.0/8"], str(entries))

    check("exact host+port", preflight.target_allowed("127.0.0.1", 8080, entries) == "127.0.0.1:8080")
    check("wrong port rejected", preflight.target_allowed("127.0.0.1", 80, entries) is None)
    check("wildcard subdomain", preflight.target_allowed("api.staging.example.com", 443, entries))
    check("wildcard does not match apex", preflight.target_allowed("staging.example.com", 443, entries) is None)
    check("cidr match", preflight.target_allowed("10.4.5.6", 80, entries) == "10.0.0.0/8")
    check("cidr outside rejected", preflight.target_allowed("11.4.5.6", 80, entries) is None)

    raw = preflight.parse_raw_request(
        "POST /app/login.php HTTP/1.1\r\nHost: 127.0.0.1:8080\r\n\r\nuser_id=&pass="
    )
    check("raw request target", raw[0]["host"] == "127.0.0.1" and raw[0]["port"] == 8080, str(raw))

    burp = preflight.parse_burp_xml(
        "<items><item><url>http://10.1.2.3:8080/x.php</url><host ip=\"10.1.2.3\">10.1.2.3</host>"
        "<port>8080</port><protocol>http</protocol></item></items>"
    )
    check("burp xml target", burp[0]["host"] == "10.1.2.3" and burp[0]["port"] == 8080, str(burp))

    request_path = tmp / "request.http"
    request_path.write_text("GET /x HTTP/1.1\nHost: 192.0.2.10\n\n", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / "preflight.py"), "--request", str(request_path), "--allowlist", str(allow)],
        capture_output=True,
        text=True,
    )
    check("preflight denies unlisted host", completed.returncode == 2, f"exit {completed.returncode}")

    completed = subprocess.run(
        [
            sys.executable, str(SCRIPTS / "preflight.py"),
            "--request", str(ROOT / "requests" / "example.http"),
            "--allowlist", str(allow),
        ],
        capture_output=True,
        text=True,
    )
    check("preflight allows listed host", completed.returncode == 0, f"exit {completed.returncode}")


def test_ragged_and_encoding(tmp: Path) -> None:
    print("robustness")
    root = tmp / "ragged" / "h" / "dump" / "db"
    root.mkdir(parents=True)
    (root / "ragged.csv").write_text("a,b,c\n1,2\n1,2,3,4\n", encoding="utf-8")
    out = tmp / "ragged-out"
    completed = run(
        [str(SCRIPTS / "csv_to_parquet.py"), "--dump-root", str(root.parent.parent.parent), "--out", str(out), "--workers", "1"]
    )
    table = pq.read_table(out / "db" / "ragged.parquet")
    check("ragged rows kept", table.num_rows == 2, str(table.num_rows))
    check("short row padded", table.to_pydict()["c"][0] is None, str(table.to_pydict()))
    entry = json.loads((out / "manifest.json").read_text())["files"][0]
    check("malformed rows counted", entry["malformed_rows"] == 2, str(entry["malformed_rows"]))

    broken = tmp / "broken"
    broken.mkdir()
    completed = subprocess.run(
        [sys.executable, str(SCRIPTS / "csv_to_parquet.py"), "--dump-root", str(broken), "--out", str(tmp / "nowhere")],
        capture_output=True,
        text=True,
    )
    check("empty dump root is an error", completed.returncode == 2, f"exit {completed.returncode}")


def main() -> int:
    print(f"python {sys.version.split()[0]}")
    compileall = subprocess.run(
        [sys.executable, "-m", "py_compile", *map(str, SCRIPTS.glob("*.py"))],
        capture_output=True,
        text=True,
    )
    check("scripts compile", compileall.returncode == 0, compileall.stderr)

    with tempfile.TemporaryDirectory(prefix="sqlmap-parquet-selftest-") as temp:
        tmp = Path(temp)
        test_conversion(tmp)
        test_preflight(tmp)
        test_ragged_and_encoding(tmp)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
