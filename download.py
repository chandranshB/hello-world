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
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_REPO = "chandranshB/hello-world"
DEFAULT_TAG = "v1.0.0"


def fetch_release_assets(repo: str, tag: str) -> list[dict]:
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "sqlmap-parquet-downloader",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("assets", [])
    except Exception as exc:
        print(f"warning: could not fetch release assets via GitHub API ({exc})", file=sys.stderr)
        return []


def parse_catalog_from_assets(assets: list[dict], repo: str, tag: str) -> dict[str, dict]:
    """
    Returns a dict of {db_name: {"tables": {table_name: {"size": int, "url": str}}, "zip_url": str}}
    """
    catalog: dict[str, dict] = {}

    for asset in assets:
        name = asset.get("name", "")
        size = asset.get("size", 0)
        url = asset.get("browser_download_url", "")

        # Match <db>.<table_name>.parquet
        if name.endswith(".parquet") and "." in name:
            parts = name.split(".")
            if len(parts) >= 3:
                db = parts[0]
                table = parts[1]
                if db not in catalog:
                    catalog[db] = {"tables": {}, "zip_url": ""}
                catalog[db]["tables"][table] = {"size": size, "url": url}

        # Match <db>.zip
        elif name.endswith(".zip") and name not in ("all-databases-parquet.zip", "all-databases-sqlite.zip"):
            if name.endswith(".sqlite.zip"):
                db = name[:-11]
                if db not in catalog:
                    catalog[db] = {"tables": {}, "zip_url": ""}
                catalog[db]["sqlite_zip_url"] = url
            else:
                db = name[:-4]
                if db not in catalog:
                    catalog[db] = {"tables": {}, "zip_url": ""}
                catalog[db]["zip_url"] = url

        # Match <db>.sqlite
        elif name.endswith(".sqlite") and name != "all-databases.sqlite":
            db = name[:-7]
            if db not in catalog:
                catalog[db] = {"tables": {}, "zip_url": ""}
            catalog[db]["sqlite_url"] = url

    return catalog


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
    parser.add_argument("--sqlite", action="store_true", help="Download as SQLite database (.sqlite / .sqlite.zip) instead of Parquet")
    parser.add_argument("--out-dir", type=Path, default=Path("downloads"), help="Destination directory (default: downloads/)")
    parser.add_argument("--url", action="store_true", help="Print direct download URL without downloading")
    parser.add_argument("--sql", help="Run a DuckDB SQL query against the table (requires duckdb)")
    args = parser.parse_args()

    assets = fetch_release_assets(args.repo, args.tag)
    catalog = parse_catalog_from_assets(assets, args.repo, args.tag)

    all_zip_url = next(
        (a["browser_download_url"] for a in assets if a.get("name") == "all-databases-parquet.zip"),
        f"https://github.com/{args.repo}/releases/download/{args.tag}/all-databases-parquet.zip",
    )

    if args.list:
        print(f"\n=== Parquet Data Catalog ({args.repo} @ {args.tag}) ===")
        if not catalog:
            print("No database tables found in release assets.")
            return 0
        for db, info in sorted(catalog.items()):
            tables = info["tables"]
            print(f"\nDatabase: {db} ({len(tables)} tables)")
            if info["zip_url"]:
                print(f"  Whole DB bundle: {info['zip_url']}")
            print(f"  {'Table':<25} {'Size':>10}  {'Direct URL'}")
            print("  " + "-" * 75)
            for t_name, t_info in sorted(tables.items()):
                size_kb = (t_info["size"] / 1024)
                print(f"  {t_name:<25} {size_kb:>8.1f}KB  {t_info['url']}")
        print(f"\nAll-in-one archive: {all_zip_url}\n")
        return 0

    if args.url:
        if args.all:
            print(all_zip_url)
        elif args.db and args.table:
            url = catalog.get(args.db, {}).get("tables", {}).get(args.table, {}).get("url")
            if not url:
                url = f"https://github.com/{args.repo}/releases/download/{args.tag}/{args.db}.{args.table}.parquet"
            print(url)
        elif args.db:
            url = catalog.get(args.db, {}).get("zip_url")
            if not url:
                url = f"https://github.com/{args.repo}/releases/download/{args.tag}/{args.db}.zip"
            print(url)
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
        table_url = catalog.get(args.db, {}).get("tables", {}).get(args.table, {}).get("url")
        if not table_url:
            table_url = f"https://github.com/{args.repo}/releases/download/{args.tag}/{args.db}.{args.table}.parquet"
        print(f"Querying {args.db}.{args.table} directly via DuckDB:\n")
        con = duckdb.connect()
        df = con.read_parquet(table_url)  # noqa: F841
        res = con.sql(args.sql)
        res.show()
        return 0

    if args.sqlite:
        if args.db:
            sqlite_url = catalog.get(args.db, {}).get("sqlite_url")
            if not sqlite_url:
                sqlite_url = f"https://github.com/{args.repo}/releases/download/{args.tag}/{args.db}.sqlite"
            dest = args.out_dir / f"{args.db}.sqlite"
            success = download_file(sqlite_url, dest)
            if success:
                print(f"\nSQLite database ready at: {dest}")
                return 0
            return 1
        else:
            all_sqlite_url = f"https://github.com/{args.repo}/releases/download/{args.tag}/all-databases.sqlite"
            dest = args.out_dir / "all-databases.sqlite"
            success = download_file(all_sqlite_url, dest)
            if success:
                print(f"\nUnified SQLite database ready at: {dest}")
                return 0
            return 1

    if args.all:
        zip_dest = args.out_dir / "all-databases-parquet.zip"
        if download_file(all_zip_url, zip_dest):
            print(f"Extracting to {args.out_dir}...")
            with zipfile.ZipFile(zip_dest, "r") as zf:
                zf.extractall(args.out_dir)
            zip_dest.unlink()
            print("All databases downloaded and organized successfully.")
            return 0
        return 1

    if args.db and args.table:
        table_url = catalog.get(args.db, {}).get("tables", {}).get(args.table, {}).get("url")
        if not table_url:
            table_url = f"https://github.com/{args.repo}/releases/download/{args.tag}/{args.db}.{args.table}.parquet"
        dest = args.out_dir / args.db / f"{args.table}.parquet"
        success = download_file(table_url, dest)
        if success:
            print(f"\nTable ready at: {dest}")
            return 0
        return 1

    if args.db:
        db_zip_url = catalog.get(args.db, {}).get("zip_url")
        if not db_zip_url:
            db_zip_url = f"https://github.com/{args.repo}/releases/download/{args.tag}/{args.db}.zip"
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
