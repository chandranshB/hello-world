#!/usr/bin/env python3
"""
Simple CLI to list and download specific Parquet tables and databases from GitHub Releases.

Zero dependencies required (pure standard Python library).

Usage Examples:
    # 1. List all available databases and tables:
    python download.py --list

    # 2. Download a specific table from a specific database:
    python download.py --db SQLite_masterdb --table users

    # 3. Download all tables for an entire database:
    python download.py --db SQLite_masterdb

    # 4. Download everything:
    python download.py --all

    # 5. Get the direct HTTPS download URL:
    python download.py --db SQLite_masterdb --table users --url

    # 6. Query directly with DuckDB (if duckdb is installed):
    python download.py --db SQLite_masterdb --table users --sql "SELECT * FROM df LIMIT 5"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_REPO = "chandranshB/hello-world"
DEFAULT_TAG = "v1.0.0"


def fetch_manifest(repo: str, tag: str) -> dict | None:
    # First try local manifest if repo is cloned locally
    local_manifest = Path("parquet") / "manifest.json"
    if local_manifest.exists():
        try:
            return json.loads(local_manifest.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Fetch from release
    url = f"https://github.com/{repo}/releases/download/{tag}/manifest.json"
    req = urllib.request.Request(url, headers={"User-Agent": "sqlmap-parquet-downloader"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"warning: could not fetch remote manifest ({exc})", file=sys.stderr)
        return None


def get_table_url(repo: str, tag: str, db: str, table: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{db}.{table}.parquet"


def get_db_zip_url(repo: str, tag: str, db: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{db}.zip"


def get_all_zip_url(repo: str, tag: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/all-databases-parquet.zip"


def download_file(url: str, dest_path: Path) -> bool:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading: {url}")
    print(f"Destination: {dest_path}")
    req = urllib.request.Request(url, headers={"User-Agent": "sqlmap-parquet-downloader"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response, open(dest_path, "wb") as out_file:
            total_size = response.getheader("Content-Length")
            total = int(total_size) if total_size else None
            downloaded = 0
            chunk_size = 16384
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                out_file.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = (downloaded / total) * 100
                    print(f"\rProgress: {downloaded:,} / {total:,} bytes ({pct:.1f}%)", end="", flush=True)
            print("\nDownload complete.")
        return True
    except urllib.error.HTTPError as exc:
        print(f"\nHTTP Error {exc.code}: {exc.reason}", file=sys.stderr)
        if dest_path.exists():
            dest_path.unlink()
        return False
    except Exception as exc:
        print(f"\nDownload failed: {exc}", file=sys.stderr)
        if dest_path.exists():
            dest_path.unlink()
        return False


def list_catalog(manifest: dict | None, repo: str, tag: str) -> None:
    print(f"\n=== Parquet Data Catalog ({repo} @ {tag}) ===")
    if not manifest:
        print("No manifest found. Defaulting to available tables in release:")
        print(f"  - SQLite_masterdb: notes, orders, users")
        return

    files = [f for f in manifest.get("files", []) if f.get("status") == "ok"]
    dbs: dict[str, list] = {}
    for f in files:
        dbs.setdefault(f["db"], []).append(f)

    for db, tables in sorted(dbs.items()):
        print(f"\nDatabase: {db} ({len(tables)} tables)")
        print(f"  Whole DB bundle: {get_db_zip_url(repo, tag, db)}")
        print(f"  {'Table':<25} {'Rows':>8} {'Size':>10}  {'Direct URL'}")
        print("  " + "-" * 75)
        for t in tables:
            size_kb = (t.get("parquet_bytes", 0) / 1024)
            direct_url = get_table_url(repo, tag, db, t["table"])
            print(f"  {t['table']:<25} {t.get('rows', 0):>8,} {size_kb:>8.1f}KB  {direct_url}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub repository (default: {DEFAULT_REPO})")
    parser.add_argument("--tag", default=DEFAULT_TAG, help=f"Release tag (default: {DEFAULT_TAG})")
    parser.add_argument("--list", action="store_true", help="List all available databases and tables")
    parser.add_argument("--db", help="Target database name")
    parser.add_argument("--table", help="Target table name")
    parser.add_argument("--all", action="store_true", help="Download all databases and tables")
    parser.add_argument("--out-dir", type=Path, default=Path("downloads"), help="Destination directory (default: downloads/)")
    parser.add_argument("--url", action="store_true", help="Print direct download URL without downloading")
    parser.add_argument("--sql", help="Run a DuckDB SQL query against the table (requires duckdb)")
    args = parser.parse_args()

    manifest = fetch_manifest(args.repo, args.tag)

    if args.list:
        list_catalog(manifest, args.repo, args.tag)
        return 0

    if args.url:
        if args.all:
            print(get_all_zip_url(args.repo, args.tag))
        elif args.db and args.table:
            print(get_table_url(args.repo, args.tag, args.db, args.table))
        elif args.db:
            print(get_db_zip_url(args.repo, args.tag, args.db))
        else:
            print("Specify --all, or --db, or --db and --table with --url", file=sys.stderr)
            return 1
        return 0

    if args.sql:
        if not (args.db and args.table):
            print("error: --sql requires both --db and --table", file=sys.stderr)
            return 1
        try:
            import duckdb
        except ImportError:
            print("error: duckdb is not installed. Install with 'pip install duckdb'", file=sys.stderr)
            return 1
        url = get_table_url(args.repo, args.tag, args.db, args.table)
        print(f"Querying {args.db}.{args.table} directly via DuckDB:\n")
        con = duckdb.connect()
        df = con.read_parquet(url)  # noqa: F841
        res = con.sql(args.sql)
        res.show()
        return 0

    if args.all:
        zip_url = get_all_zip_url(args.repo, args.tag)
        zip_dest = args.out_dir / "all-databases-parquet.zip"
        if download_file(zip_url, zip_dest):
            print(f"Extracting to {args.out_dir}...")
            with zipfile.ZipFile(zip_dest, "r") as zf:
                zf.extractall(args.out_dir)
            zip_dest.unlink()
            print("All databases downloaded and organized successfully.")
            return 0
        return 1

    if args.db and args.table:
        table_url = get_table_url(args.repo, args.tag, args.db, args.table)
        dest = args.out_dir / args.db / f"{args.table}.parquet"
        success = download_file(table_url, dest)
        if success:
            print(f"\nTable ready at: {dest}")
            return 0
        return 1

    if args.db:
        db_zip_url = get_db_zip_url(args.repo, args.tag, args.db)
        zip_dest = args.out_dir / f"{args.db}.zip"
        if download_file(db_zip_url, zip_dest):
            print(f"Extracting {args.db} tables to {args.out_dir / args.db}...")
            with zipfile.ZipFile(zip_dest, "r") as zf:
                zf.extractall(args.out_dir)
            zip_dest.unlink()
            print(f"Database '{args.db}' tables downloaded and organized successfully.")
            return 0
        return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
