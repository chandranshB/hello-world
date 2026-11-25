#!/usr/bin/env python3
"""
Package Parquet files into standardized release assets organized by database.

Output in release_assets/:
  - <db>.<table_name>.parquet          (individual flat assets for direct download)
  - <db>.zip                           (zip bundle per database)
  - all-databases-parquet.zip          (complete directory tree)
  - catalog.duckdb                     (unified DuckDB catalog)
  - manifest.json                      (schema & metadata)
  - summary.md                         (run summary)
  - DATA_CATALOG.md                    (markdown catalog with download links and code snippets)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq


def human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GiB"


def package_assets(
    parquet_dir: Path,
    out_dir: Path,
    repo: str = "chandranshB/hello-world",
    tag: str = "v1.0.0",
) -> int:
    manifest_path = parquet_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"error: manifest not found at {manifest_path}", file=sys.stderr)
        return 1

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = [e for e in manifest.get("files", []) if e.get("status") == "ok"]

    out_dir.mkdir(parents=True, exist_ok=True)

    base_download_url = f"https://github.com/{repo}/releases/download/{tag}"
    latest_download_url = f"https://github.com/{repo}/releases/latest/download"

    # Group by database
    db_groups = defaultdict(list)
    for entry in entries:
        db = entry["db"]
        table = entry["table"]
        # Exclude dummy/metadata databases, loose files, and sqlmap scan results
        if db in ("_", "__", "", "default") or table.startswith("results-") or db.startswith("."):
            continue
        src = parquet_dir / db / f"{table}.parquet"
        if not src.exists():
            # try fallback path from entry
            src = Path(entry.get("output", ""))
        db_groups[db].append((entry, src))

    # 1. Copy individual tables named <db>.<table_name>.parquet
    for db, items in db_groups.items():
        for entry, src in items:
            table = entry["table"]
            flat_name = f"{db}.{table}.parquet"
            dest = out_dir / flat_name
            shutil.copy2(src, dest)
            print(f"Created table asset: {flat_name} ({human_size(dest.stat().st_size)})")

    # 2. Build per-database SQLite database: <db>.sqlite
    for db, items in db_groups.items():
        db_sqlite_path = out_dir / f"{db}.sqlite"
        if db_sqlite_path.exists():
            db_sqlite_path.unlink()
        conn = sqlite3.connect(db_sqlite_path)
        cur = conn.cursor()
        for entry, src in items:
            table = entry["table"]
            pf = pq.ParquetFile(src)
            cols = pf.schema.names
            col_defs = ", ".join(f'"{c}"' for c in cols)
            cur.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_defs})')
            placeholders = ", ".join(["?"] * len(cols))
            insert_sql = f'INSERT INTO "{table}" VALUES ({placeholders})'
            for batch in pf.iter_batches(batch_size=65536):
                pydict = batch.to_pydict()
                rows = zip(*(pydict[c] for c in cols))
                cur.executemany(insert_sql, rows)
        cur.execute("PRAGMA optimize")
        conn.commit()
        conn.close()
        print(f"Created SQLite database: {db_sqlite_path.name} ({human_size(db_sqlite_path.stat().st_size)})")

    # 3. Build unified all-databases SQLite database
    all_sqlite_path = out_dir / "all-databases.sqlite"
    if all_sqlite_path.exists():
        all_sqlite_path.unlink()
    all_conn = sqlite3.connect(all_sqlite_path)
    all_cur = all_conn.cursor()
    for db, items in db_groups.items():
        for entry, src in items:
            table = entry["table"]
            safe_table = f"{db}_{table}" if len(db_groups) > 1 else table
            pf = pq.ParquetFile(src)
            cols = pf.schema.names
            col_defs = ", ".join(f'"{c}"' for c in cols)
            all_cur.execute(f'CREATE TABLE IF NOT EXISTS "{safe_table}" ({col_defs})')
            placeholders = ", ".join(["?"] * len(cols))
            insert_sql = f'INSERT INTO "{safe_table}" VALUES ({placeholders})'
            for batch in pf.iter_batches(batch_size=65536):
                pydict = batch.to_pydict()
                rows = zip(*(pydict[c] for c in cols))
                all_cur.executemany(insert_sql, rows)
    all_cur.execute("PRAGMA optimize")
    all_conn.commit()
    all_conn.close()
    print(f"Created unified SQLite database: {all_sqlite_path.name} ({human_size(all_sqlite_path.stat().st_size)})")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet-dir", type=Path, default=Path("parquet"), help="Path to parquet/ directory")
    parser.add_argument("--out-dir", type=Path, default=Path("release_assets"), help="Output directory for staged assets")
    parser.add_argument("--repo", default="chandranshB/hello-world", help="GitHub repo OWNER/REPO")
    parser.add_argument("--tag", default="v1.0.0", help="Release tag")
    args = parser.parse_args()

    return package_assets(
        parquet_dir=args.parquet_dir.resolve(),
        out_dir=args.out_dir.resolve(),
        repo=args.repo,
        tag=args.tag,
    )


if __name__ == "__main__":
    raise SystemExit(main())
