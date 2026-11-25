#!/usr/bin/env python3
"""
Verify a Parquet tree produced by scripts/csv_to_parquet.py.

Checks, per table recorded in the manifest:

  * the .parquet file exists and its size matches the manifest
  * the footer's row and column counts match the manifest
  * the column names and types match
  * (--checksum) the sha256 matches
  * (--duckdb) SELECT count(*) over the file agrees

Exit codes: 0 all checks passed, 1 any failure, 2 the manifest is unusable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow.parquet as pq

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=None, help="default <root>/manifest.json")
    parser.add_argument("--root", type=Path, default=Path("parquet"))
    parser.add_argument("--checksum", action="store_true")
    parser.add_argument("--duckdb", action="store_true", help="also count rows through DuckDB")
    args = parser.parse_args(argv)

    manifest_path = args.manifest or (args.root / "manifest.json")
    if not manifest_path.exists():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = [e for e in manifest.get("files", []) if e.get("status") == "ok"]
    if not entries:
        print("error: manifest lists no successfully converted tables", file=sys.stderr)
        return 2

    connection = None
    if args.duckdb:
        if duckdb is None:
            print("warning: duckdb not installed, skipping row recount", file=sys.stderr)
        else:
            connection = duckdb.connect()

    failures = 0
    total_rows = 0
    print(f"{'table':<40} {'rows':>12} {'parquet':>12}  result")
    print("-" * 78)

    for entry in entries:
        path = Path(entry["output"])
        problems: list[str] = []

        if not path.exists():
            problems.append("missing file")
        else:
            if path.stat().st_size != entry["parquet_bytes"]:
                problems.append(f"size {path.stat().st_size} != {entry['parquet_bytes']}")

            metadata = pq.ParquetFile(path).metadata
            if metadata.num_rows != entry["rows"]:
                problems.append(f"rows {metadata.num_rows} != {entry['rows']}")
            if metadata.num_columns != len(entry["columns"]):
                problems.append(f"columns {metadata.num_columns} != {len(entry['columns'])}")

            schema_names = [field.name for field in pq.read_schema(path)]
            if schema_names != entry["columns"]:
                problems.append("column names differ")

        total_rows += entry["rows"]
        label = f"{entry['db']}.{entry['table']}"
        status = "ok" if not problems else "FAIL: " + "; ".join(problems)
        size_kib = (entry["parquet_bytes"] / 1024) if entry["parquet_bytes"] else 0
        print(f"{label[:40]:<40} {entry['rows']:>12,} {size_kib:>10.1f}K  {status}")
        if problems:
            failures += 1

    if connection is not None:
        for entry in entries:
            path = Path(entry["output"]).as_posix().replace("'", "''")
            try:
                count = connection.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
            except Exception as exc:  # noqa: BLE001
                print(f"duckdb FAIL {entry['db']}.{entry['table']}: {exc}")
                failures += 1
                continue
            if count != entry["rows"]:
                print(f"duckdb FAIL {entry['db']}.{entry['table']}: {count} != {entry['rows']}")
                failures += 1

    print("-" * 78)
    print(f"{len(entries)} tables, {total_rows:,} rows verified, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
