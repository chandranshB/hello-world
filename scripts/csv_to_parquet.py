#!/usr/bin/env python3
"""
Convert an sqlmap CSV dump tree into Parquet.

sqlmap writes dumps as RFC-4180 CSV under:

    <output-dir>/<host>/dump/<db>/<table>.csv

with a header row, comma delimiter, double-quote escaping, embedded newlines
quoted, and database NULLs rendered as the literal token ``NULL``. That is a
fine interchange format and a poor analytics format, so this script rewrites
every table as a zstd-compressed Parquet file, verifies each one by reading the
footer back, and records everything in a JSON manifest.

Features that matter for real dumps:

  * streaming conversion with a fixed batch size (constant memory, any file size)
  * encoding detection (UTF-8/UTF-8-BOM/UTF-16/CP1252/Latin-1) so GBK or
    Latin-1 data does not abort the run
  * duplicate / empty / non-identifier column names sanitised
  * ragged rows padded or truncated and counted, never silently dropped
  * parallel workers (``--workers``), one file per worker
  * optional strict type inference (int64/float64/bool) with round-trip guards
  * atomic writes (temp file + rename), so a killed run never leaves a
    half-written .parquet that looks valid
  * optional DuckDB catalog with one view per table
  * GitHub-flavoured Markdown summary for $GITHUB_STEP_SUMMARY
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover
    print("error: pyarrow is required (pip install pyarrow)", file=sys.stderr)
    raise SystemExit(3)

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

DEFAULT_BATCH = 65536
SQLMAP_NULL = "NULL"
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
INT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
FLOAT_RE = re.compile(r"^-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def sanitize_name(name: str, fallback: str) -> str:
    """Filesystem-safe, case-insensitive-unique friendly identifier."""
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", "_", name)
    name = name.strip("._ ") or fallback
    if name.lower() in WINDOWS_RESERVED:
        name = f"{name}_"
    return name[:120]


def detect_encoding(path: Path) -> str:
    with open(path, "rb") as handle:
        raw = handle.read(1 << 20)
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    probe = raw[:-4] if len(raw) > 4 else raw  # do not split a multi-byte char
    try:
        probe.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        probe.decode("cp1252")
        return "cp1252"
    except UnicodeDecodeError:
        return "latin-1"


def open_csv_text(path: Path, encoding: str) -> io.TextIOWrapper:
    return open(path, "r", encoding=encoding, errors="replace", newline="")


def normalize_header(raw_header: list[str]) -> list[str]:
    """Deduplicate case-insensitively and replace empty names."""
    seen: dict[str, int] = {}
    names: list[str] = []
    for index, name in enumerate(raw_header, start=1):
        name = (name or "").replace("\ufeff", "").strip() or f"column_{index}"
        key = name.lower()
        if key in seen:
            seen[key] += 1
            name = f"{name}_{seen[key]}"
            key = name.lower()
        else:
            seen[key] = 1
        names.append(name)
    return names


def discover_csv_files(root: Path, include: list[str], exclude: list[str]) -> list[Path]:
    if not root.is_dir():
        raise SystemExit(f"error: dump root not found: {root}")
    include_re = [re.compile(p) for p in include]
    exclude_re = [re.compile(p) for p in exclude]
    found = []
    for path in sorted(root.rglob("*.csv")):
        rel = path.relative_to(root).as_posix()
        if include_re and not any(r.search(rel) for r in include_re):
            continue
        if any(r.search(rel) for r in exclude_re):
            continue
        found.append(path)
    return found


def split_db_table(rel: Path) -> tuple[str, str, str]:
    """Derive (host, db, table) from <host>/dump/<db>/<table>.csv, with fallbacks."""
    parts = list(rel.parts)
    host = ""
    db = "_"
    if "dump" in parts[:-1]:
        marker = len(parts) - 1 - parts[::-1].index("dump")
        host = parts[marker - 1] if marker >= 1 else ""
        remainder = parts[marker + 1 :]
        if len(remainder) >= 2:
            db = remainder[-2]
        elif len(remainder) == 1:
            db = "_"
    elif len(parts) >= 2:
        db = parts[-2]
    return host, sanitize_name(db, "_"), sanitize_name(Path(parts[-1]).stem, "table")


# --------------------------------------------------------------------------- #
# type inference
# --------------------------------------------------------------------------- #

@dataclass
class ColumnProfile:
    all_int: bool = True
    all_float: bool = True
    all_bool: bool = True
    has_float_marker: bool = False
    seen: bool = False

    def offer(self, value: str) -> None:
        if value is None or value == "":
            return
        self.seen = True
        if not (INT_RE.match(value) and INT64_MIN <= int(value) <= INT64_MAX):
            self.all_int = False
        if "." in value or "e" in value or "E" in value:
            self.has_float_marker = True
        if not (FLOAT_RE.match(value) or INT_RE.match(value)):
            self.all_float = False
        if value.lower() not in ("true", "false"):
            self.all_bool = False

    def resolve(self) -> pa.DataType:
        if self.seen and self.all_bool:
            return pa.bool_()
        if self.seen and self.all_int:
            return pa.int64()
        if self.seen and self.all_float and self.has_float_marker:
            return pa.float64()
        return pa.string()


def is_row_included(table: str, header: list[str], row: list[str]) -> bool:
    """Filter rows by domain rules. For course_coordinator, keep only entries where date has year > 2025."""
    if "course_coordinator" in table.lower():
        date_idx = -1
        for idx, col in enumerate(header):
            if col.lower() in ("date1", "date"):
                date_idx = idx
                break
        if date_idx != -1 and date_idx < len(row):
            val = (row[date_idx] or "").strip()
            m = re.match(r"^(\d{4})", val)
            if m:
                try:
                    return int(m.group(1)) > 2025
                except ValueError:
                    return False
            return False
    return True


def profile_file(
    path: Path, encoding: str, delimiter: str, table_name: str = ""
) -> tuple[list[str], list[ColumnProfile], int]:
    profiles: list[ColumnProfile] = []
    header: list[str] = []
    rows = 0
    with open_csv_text(path, encoding) as handle:
        reader = csv.reader(handle, delimiter=delimiter, quotechar='"', doublequote=True)
        raw_header = next(reader, None)
        if raw_header is None:
            return [], [], 0
        header = normalize_header(raw_header)
        profiles = [ColumnProfile() for _ in header]
        for row in reader:
            if not row:
                continue
            if not is_row_included(table_name, header, row):
                continue
            rows += 1
            for index, value in enumerate(row[: len(header)]):
                profiles[index].offer(value)
    return header, profiles, rows


# --------------------------------------------------------------------------- #
# conversion
# --------------------------------------------------------------------------- #

@dataclass
class Task:
    source: Path
    target: Path
    host: str
    db: str
    table: str
    delimiter: str
    compression: str | None
    batch_size: int
    infer_types: bool
    null_token: str | None
    checksum: bool


@dataclass
class Result:
    source: str = ""
    db: str = ""
    table: str = ""
    output: str = ""
    rows: int = 0
    columns: list[str] = field(default_factory=list)
    column_types: list[str] = field(default_factory=list)
    csv_bytes: int = 0
    parquet_bytes: int = 0
    encoding: str = ""
    lossy_encoding: bool = False
    malformed_rows: int = 0
    sha256: str | None = None
    elapsed_s: float = 0.0
    status: str = "ok"
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "db": self.db,
            "table": self.table,
            "output": self.output,
            "rows": self.rows,
            "columns": self.columns,
            "column_types": self.column_types,
            "csv_bytes": self.csv_bytes,
            "parquet_bytes": self.parquet_bytes,
            "encoding": self.encoding,
            "lossy_encoding": self.lossy_encoding,
            "malformed_rows": self.malformed_rows,
            "sha256": self.sha256,
            "elapsed_s": self.elapsed_s,
            "status": self.status,
            "error": self.error,
        }


def _value_converter(dtype: pa.DataType, null_token: str | None):
    if dtype == pa.string():
        if null_token is None:
            return lambda v: v
        return lambda v: None if v == null_token else v
    if dtype == pa.int64():
        return lambda v: None if v in (None, "", null_token) else int(v)
    if dtype == pa.float64():
        return lambda v: None if v in (None, "", null_token) else float(v)
    if dtype == pa.bool_():
        return lambda v: None if v in (None, "", null_token) else v.lower() == "true"
    raise AssertionError(f"unsupported type {dtype}")


def convert(task: Task) -> Result:
    result = Result(source=str(task.source), db=task.db, table=task.table)
    started = time.perf_counter()
    result.csv_bytes = task.source.stat().st_size if task.source.exists() else 0

    temp_path: Path | None = None
    try:
        encoding = detect_encoding(task.source)
        result.encoding = encoding
        result.lossy_encoding = encoding in ("cp1252", "latin-1")

        if task.infer_types:
            header, profiles, _ = profile_file(task.source, encoding, task.delimiter, task.table)
            types = [profile.resolve() for profile in profiles]
        else:
            with open_csv_text(task.source, encoding) as handle:
                raw_header = next(csv.reader(handle, delimiter=task.delimiter, quotechar='"'), None)
            header = normalize_header(raw_header) if raw_header else []
            types = [pa.string()] * len(header)

        if not header:
            result.status = "skipped"
            result.error = "empty file (no header row)"
            result.elapsed_s = round(time.perf_counter() - started, 3)
            return result

        result.columns = header
        result.column_types = [str(t) for t in types]
        schema = pa.schema([pa.field(name, dtype) for name, dtype in zip(header, types)])
        converters = [_value_converter(t, task.null_token) for t in types]

        task.target.parent.mkdir(parents=True, exist_ok=True)
        result.output = str(task.target)
        with tempfile.NamedTemporaryFile(
            dir=task.target.parent, prefix=f".{task.target.stem}.", suffix=".tmp", delete=False
        ) as handle:
            temp_path = Path(handle.name)

        writer = pq.ParquetWriter(
            temp_path,
            schema,
            compression=task.compression,
            use_dictionary=True,
            write_statistics=True,
        )
        rows = 0
        malformed = 0
        try:
            with open_csv_text(task.source, encoding) as handle:
                reader = csv.reader(handle, delimiter=task.delimiter, quotechar='"', doublequote=True)
                next(reader, None)  # header already consumed
                batch: list[list[Any]] = [[] for _ in header]
                pending = 0

                for row in reader:
                    if not row:
                        continue

                    if not is_row_included(task.table, header, row):
                        continue

                    if len(row) > len(header):
                        malformed += 1
                        row = row[: len(header)]
                    elif len(row) < len(header):
                        malformed += 1
                        row = row + [None] * (len(header) - len(row))

                    for index, convert_value in enumerate(converters):
                        batch[index].append(convert_value(row[index]))

                    pending += 1
                    rows += 1
                    if pending >= task.batch_size:
                        table = pa.Table.from_arrays(
                            [pa.array(column, type=dtype) for column, dtype in zip(batch, types)],
                            schema=schema,
                        )
                        writer.write_table(table)
                        batch = [[] for _ in header]
                        pending = 0

                if pending:
                    table = pa.Table.from_arrays(
                        [pa.array(column, type=dtype) for column, dtype in zip(batch, types)],
                        schema=schema,
                    )
                    writer.write_table(table)
                elif rows == 0:
                    # header-only table: still emit a valid file with the right schema
                    writer.write_table(schema.empty_table())
        finally:
            writer.close()

        # verify the footer before publishing the file
        written = pq.ParquetFile(temp_path)
        try:
            if written.metadata.num_rows != rows:
                raise RuntimeError(f"row count mismatch: csv={rows} parquet={written.metadata.num_rows}")
            if written.metadata.num_columns != len(header):
                raise RuntimeError(
                    f"column count mismatch: header={len(header)} parquet={written.metadata.num_columns}"
                )
        finally:
            written.close()

        os.replace(temp_path, task.target)
        temp_path = None

        result.rows = rows
        result.malformed_rows = malformed
        result.parquet_bytes = task.target.stat().st_size
        if task.checksum:
            result.sha256 = sha256_file(task.target)
    except Exception as exc:  # noqa: BLE001 - one bad table must not kill the run
        result.status = "error"
        result.error = f"{type(exc).__name__}: {exc}"
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        result.elapsed_s = round(time.perf_counter() - started, 3)

    return result


def _worker(task: Task) -> dict[str, Any]:
    return convert(task).as_dict()


# --------------------------------------------------------------------------- #
# manifest / catalog / summary
# --------------------------------------------------------------------------- #

def build_duckdb(entries: list[dict[str, Any]], catalog: Path, sql_path: Path | None) -> None:
    try:
        import duckdb
    except ImportError:
        print("warning: duckdb not installed, skipping catalog", file=sys.stderr)
        return

    def ident(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    statements: list[str] = []
    for entry in entries:
        if entry["status"] != "ok":
            continue
        schema_name = ident(entry["db"])
        statements.append(f"CREATE SCHEMA IF NOT EXISTS {schema_name}")
        statements.append(
            f"CREATE OR REPLACE VIEW {schema_name}.{ident(entry['table'])} AS "
            f"SELECT * FROM read_parquet({literal(Path(entry['output']).as_posix())})"
        )

    catalog.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(catalog))
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "CREATE OR REPLACE TABLE _inventory ("
            "db VARCHAR, \"table\" VARCHAR, rows BIGINT, columns BIGINT, parquet VARCHAR)"
        )
        inventory = [
            (e["db"], e["table"], e["rows"], len(e["columns"]), Path(e["output"]).as_posix())
            for e in entries
            if e["status"] == "ok"
        ]
        if inventory:
            connection.executemany("INSERT INTO _inventory VALUES (?, ?, ?, ?, ?)", inventory)
    finally:
        connection.close()

    if sql_path is not None:
        sql_path.write_text(";\n".join(statements) + ";\n", encoding="utf-8")


def write_summary_md(
    entries: list[dict[str, Any]],
    totals: dict[str, Any],
    path: Path,
    run_meta: dict[str, Any] | None = None,
) -> None:
    lines = ["## sqlmap -> Parquet", ""]
    if run_meta:
        status = "ok" if run_meta.get("ok") else f"failed (exit {run_meta.get('returncode')})"
        lines.append(f"- run: {status}, {run_meta.get('duration_seconds', '?')}s")
        for target in run_meta.get("targets", []):
            lines.append(f"- target: `{target.get('host')}:{target.get('port')}`")
        lines.append(f"- sqlmap: `{run_meta.get('sqlmap_version') or 'unknown'}`")
        lines.append("")

    lines += [
        "| table | rows | CSV | Parquet | ratio |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for entry in sorted(entries, key=lambda e: (e["db"], e["table"])):
        if entry["status"] != "ok":
            continue
        ratio = entry["csv_bytes"] / entry["parquet_bytes"] if entry["parquet_bytes"] else 0
        lines.append(
            f"| `{entry['db']}.{entry['table']}` | {entry['rows']:,} | "
            f"{_human(entry['csv_bytes'])} | {_human(entry['parquet_bytes'])} | {ratio:.1f}x |"
        )

    lines += [
        "",
        f"**{totals['files']} tables, {totals['rows']:,} rows, "
        f"{_human(totals['parquet_bytes'])} parquet**",
    ]

    failures = [e for e in entries if e["status"] == "error"]
    skipped = [e for e in entries if e["status"] == "skipped"]
    if skipped:
        lines.append("")
        lines.append(f"skipped {len(skipped)} empty file(s)")
    if failures:
        lines += ["", "### failures", ""]
        for entry in failures:
            lines.append(f"- `{entry['source']}`: {entry['error']}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _human(size: int | float) -> str:
    size = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dump-root", required=True, type=Path, help="sqlmap output directory")
    parser.add_argument("--out", required=True, type=Path, help="parquet output directory")
    parser.add_argument("--workers", type=int, default=0, help="0 = one per CPU (default)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument(
        "--compression", default="zstd", choices=["zstd", "snappy", "gzip", "lz4", "none"]
    )
    parser.add_argument("--delimiter", default=",", help="CSV delimiter (sqlmap default ',')")
    parser.add_argument("--null-token", default=None, help=f"map this literal to NULL (sqlmap uses {SQLMAP_NULL})")
    parser.add_argument("--infer-types", action="store_true", help="promote int64/float64/bool columns")
    parser.add_argument("--checksum", action="store_true", help="store sha256 of each parquet in the manifest")
    parser.add_argument("--include", action="append", default=[], help="regex on relative path to include")
    parser.add_argument("--exclude", action="append", default=[], help="regex on relative path to exclude")
    parser.add_argument("--manifest", type=Path, default=None, help="manifest path (default <out>/manifest.json)")
    parser.add_argument("--run-meta", type=Path, default=None, help="run.json from scripts/run_sqlmap.py")
    parser.add_argument("--catalog", action="store_true", help="build a DuckDB catalog with one view per table")
    parser.add_argument("--summary-md", type=Path, default=None, help="write a Markdown summary here")
    parser.add_argument("--no-fail-on-error", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    dump_root = args.dump_root.resolve()
    out_root = args.out.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    sources = discover_csv_files(dump_root, args.include, args.exclude)
    if not sources:
        print(f"error: no .csv files under {dump_root}", file=sys.stderr)
        return 2

    compression = None if args.compression == "none" else args.compression
    workers = args.workers or (os.cpu_count() or 1)

    # plan targets first so collisions are resolved deterministically
    tasks: list[Task] = []
    used: set[Path] = set()
    for source in sources:
        rel = source.relative_to(dump_root)
        host, db, table = split_db_table(rel)
        target = out_root / db / f"{table}.parquet"
        if target in used:
            target = out_root / db / f"{table}__{host or 'dupe'}.parquet"
        suffix = 1
        while target in used:
            target = out_root / db / f"{table}__{suffix}.parquet"
            suffix += 1
        used.add(target)
        tasks.append(
            Task(
                source=source,
                target=target,
                host=host,
                db=db,
                table=table,
                delimiter=args.delimiter,
                compression=compression,
                batch_size=max(1, args.batch_size),
                infer_types=args.infer_types,
                null_token=args.null_token,
                checksum=args.checksum,
            )
        )

    if not args.quiet:
        print(f"converting {len(tasks)} table(s) with {workers} worker(s)", file=sys.stderr)

    results: list[dict[str, Any]] = []
    started = time.perf_counter()

    if workers == 1 or len(tasks) == 1:
        for index, task in enumerate(tasks, start=1):
            entry = _worker(task)
            results.append(entry)
            if not args.quiet:
                _report(index, len(tasks), entry)
    else:
        import multiprocessing

        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            futures = {pool.submit(_worker, task): task for task in tasks}
            for index, future in enumerate(as_completed(futures), start=1):
                entry = future.result()
                results.append(entry)
                if not args.quiet:
                    _report(index, len(tasks), entry)

    elapsed = round(time.perf_counter() - started, 2)
    ok = [e for e in results if e["status"] == "ok"]
    totals = {
        "files": len(ok),
        "rows": sum(e["rows"] for e in ok),
        "csv_bytes": sum(e["csv_bytes"] for e in ok),
        "parquet_bytes": sum(e["parquet_bytes"] for e in ok),
    }
    totals["ratio"] = round(totals["csv_bytes"] / totals["parquet_bytes"], 2) if totals["parquet_bytes"] else None

    run_meta = None
    if args.run_meta and args.run_meta.exists():
        try:
            run_meta = json.loads(args.run_meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            run_meta = None

    manifest_path = args.manifest or (out_root / "manifest.json")
    
    # Merge with previous manifest entries if present so no tables are lost across partial runs
    merged_entries: dict[tuple[str, str], dict[str, Any]] = {}
    if manifest_path.exists():
        try:
            prev_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for entry in prev_manifest.get("files", []):
                out_p = Path(entry.get("output", ""))
                # Retain previous entry if the parquet file still exists on disk
                if out_p.exists() or (out_root / entry["db"] / f"{entry['table']}.parquet").exists():
                    merged_entries[(entry["db"], entry["table"])] = entry
        except Exception:
            pass

    for entry in results:
        merged_entries[(entry["db"], entry["table"])] = entry

    all_files = sorted(merged_entries.values(), key=lambda e: (e["db"], e["table"]))
    ok_files = [e for e in all_files if e.get("status") == "ok"]

    totals = {
        "files": len(ok_files),
        "rows": sum(e.get("rows", 0) for e in ok_files),
        "csv_bytes": sum(e.get("csv_bytes", 0) for e in ok_files),
        "parquet_bytes": sum(e.get("parquet_bytes", 0) for e in ok_files),
    }
    totals["ratio"] = round(totals["csv_bytes"] / totals["parquet_bytes"], 2) if totals["parquet_bytes"] else None

    manifest = {
        "generated_at": utc_now(),
        "dump_root": str(dump_root),
        "parquet_root": str(out_root),
        "elapsed_seconds": elapsed,
        "options": {
            "compression": args.compression,
            "infer_types": args.infer_types,
            "null_token": args.null_token,
            "delimiter": args.delimiter,
            "batch_size": args.batch_size,
            "workers": workers,
        },
        "run": run_meta,
        "totals": totals,
        "files": all_files,
        "errors": [e for e in all_files if e.get("status") == "error"],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    manifest_csv = manifest_path.with_suffix(".csv")
    with open(manifest_csv, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["db", "table", "rows", "columns", "csv_bytes", "parquet_bytes", "status", "output", "error"])
        for entry in manifest["files"]:
            writer.writerow(
                [
                    entry["db"],
                    entry["table"],
                    entry["rows"],
                    len(entry["columns"]),
                    entry["csv_bytes"],
                    entry["parquet_bytes"],
                    entry["status"],
                    entry["output"],
                    entry["error"] or "",
                ]
            )

    if args.catalog:
        catalog_path = out_root / "catalog.duckdb"
        build_duckdb(manifest["files"], catalog_path, out_root / "catalog.sql")
        manifest["catalog"] = str(catalog_path)

    if args.summary_md:
        write_summary_md(manifest["files"], totals, args.summary_md, run_meta)

    if not args.quiet:
        print(
            f"done: {totals['files']} tables, {totals['rows']:,} rows, "
            f"{_human(totals['csv_bytes'])} CSV -> {_human(totals['parquet_bytes'])} Parquet "
            f"in {elapsed}s",
            file=sys.stderr,
        )

    failures = [e for e in results if e["status"] == "error"]
    if failures:
        for entry in failures:
            print(f"FAILED {entry['source']}: {entry['error']}", file=sys.stderr)
        if not args.no_fail_on_error:
            return 1
    return 0


def _report(index: int, total: int, entry: dict[str, Any]) -> None:
    if entry["status"] == "ok":
        print(
            f"[{index}/{total}] {entry['db']}.{entry['table']}  {entry['rows']:,} rows  "
            f"{_human(entry['csv_bytes'])} -> {_human(entry['parquet_bytes'])}  {entry['elapsed_s']}s",
            file=sys.stderr,
        )
    elif entry["status"] == "skipped":
        print(f"[{index}/{total}] skipped {entry['source']}", file=sys.stderr)
    else:
        print(f"[{index}/{total}] FAILED {entry['source']}: {entry['error']}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
