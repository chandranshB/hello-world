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
import sys
import zipfile
from collections import defaultdict
from pathlib import Path


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

    # 2. Build per-database zip: <db>.zip
    for db, items in db_groups.items():
        db_zip_path = out_dir / f"{db}.zip"
        with zipfile.ZipFile(db_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry, src in items:
                table = entry["table"]
                zf.write(src, arcname=f"{db}/{table}.parquet")
        print(f"Created database bundle: {db_zip_path.name} ({human_size(db_zip_path.stat().st_size)})")

    # 3. Build all-databases-parquet.zip
    all_zip_path = out_dir / "all-databases-parquet.zip"
    with zipfile.ZipFile(all_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for db, items in db_groups.items():
            for entry, src in items:
                table = entry["table"]
                zf.write(src, arcname=f"{db}/{table}.parquet")
        for extra in ["catalog.duckdb", "catalog.sql", "manifest.csv", "manifest.json", "summary.md"]:
            p = parquet_dir / extra
            if p.exists():
                zf.write(p, arcname=extra)
    print(f"Created all-in-one bundle: {all_zip_path.name} ({human_size(all_zip_path.stat().st_size)})")

    # 4. Keep internal catalog/manifest inside the zip bundle only, not as loose bloated assets
    # Loose assets are strictly clean: table parquets, per-DB zip, and all-databases zip

    # 5. Generate DATA_CATALOG.md with direct download links and examples
    catalog_lines = [
        "# Parquet Data Catalog",
        "",
        "This catalog lists all databases and tables dumped and converted to Parquet.",
        "You can download any specific table, download an entire database bundle, or directly query any table in Python/DuckDB without downloading manually.",
        "",
        "## Summary of Databases",
        "",
    ]

    for db, items in sorted(db_groups.items()):
        total_rows = sum(e["rows"] for e, _ in items)
        total_bytes = sum(e["parquet_bytes"] for e, _ in items)
        db_zip_name = f"{db}.zip"
        catalog_lines.extend([
            f"### Database: `{db}`",
            "",
            f"- **Tables**: {len(items)}",
            f"- **Total Rows**: {total_rows:,}",
            f"- **Total Parquet Size**: {human_size(total_bytes)}",
            f"- **Download All Tables for `{db}`**: [`{db_zip_name}`]({latest_download_url}/{db_zip_name})",
            "",
            "| Table | Rows | Parquet Size | Columns | Direct Download Link |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])

        for entry, _ in items:
            table = entry["table"]
            rows = entry["rows"]
            size_str = human_size(entry["parquet_bytes"])
            cols = ", ".join(f"`{c}`" for c in entry["columns"])
            asset_file = f"{db}.{table}.parquet"
            download_url = f"{latest_download_url}/{asset_file}"
            catalog_lines.append(
                f"| `{table}` | {rows:,} | {size_str} | {cols} | [Download `{asset_file}`]({download_url}) |"
            )

        catalog_lines.extend(["", "---", ""])

    catalog_lines.extend([
        "## Fast Usage Examples",
        "",
        "### 1. Download via Python CLI Helper (`download.py`)",
        "",
        "```bash",
        "# List all databases and tables",
        "python download.py --list",
        "",
        "# Download a specific table from a specific DB",
        "python download.py --db SQLite_masterdb --table users",
        "",
        "# Download an entire database",
        "python download.py --db SQLite_masterdb",
        "",
        "# Download all databases",
        "python download.py --all",
        "```",
        "",
        "### 2. Direct Query in DuckDB (Zero local download required)",
        "",
        "```python",
        "import duckdb",
        "",
        "# Query a specific table directly from GitHub Release over HTTPS:",
        f"url = '{latest_download_url}/SQLite_masterdb.users.parquet'",
        "df = duckdb.read_parquet(url)",
        "print(df.show())",
        "",
        "# Or run complex SQL directly against the remote Parquet file:",
        "duckdb.sql(f'''",
        "    SELECT username, email",
        "    FROM read_parquet('{url}')",
        "    WHERE id <= 3",
        "''').show()",
        "```",
        "",
        "### 3. Read in Pandas / Polars",
        "",
        "```python",
        "# Pandas",
        "import pandas as pd",
        f"df = pd.read_parquet('{latest_download_url}/SQLite_masterdb.users.parquet')",
        "",
        "# Polars",
        "import polars as pl",
        f"df = pl.read_parquet('{latest_download_url}/SQLite_masterdb.users.parquet')",
        "```",
        "",
        "### 4. Direct Download via curl / wget / PowerShell",
        "",
        "**PowerShell:**",
        "```powershell",
        f"Invoke-WebRequest -Uri '{latest_download_url}/SQLite_masterdb.users.parquet' -OutFile 'users.parquet'",
        "```",
        "",
        "**curl:**",
        "```bash",
        f"curl -LO '{latest_download_url}/SQLite_masterdb.users.parquet'",
        "```",
        "",
        "**wget:**",
        "```bash",
        f"wget '{latest_download_url}/SQLite_masterdb.users.parquet'",
        "```",
        "",
    ])

    catalog_path = out_dir / "DATA_CATALOG.md"
    catalog_path.write_text("\n".join(catalog_lines), encoding="utf-8")
    print(f"Generated {catalog_path.name}")

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
