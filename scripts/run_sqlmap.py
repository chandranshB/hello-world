#!/usr/bin/env python3
"""
Run sqlmap against a saved request and write its CSV dump tree, then emit run
metadata for the converter.

The wrapper does four things sqlmap does not do for you in CI:

  1. resolves the target from the request file and checks it against an allowlist
     before a single packet leaves the box;
  2. picks the right sqlmap input flag (-r for a raw request, -l for Burp/HAR,
     -u when the file holds nothing but a URL) -- note that `-u login-request.txt`
     is not valid sqlmap: -u takes a URL, not a filename;
  3. pins --dump-format=CSV so the conversion step has a stable input;
  4. enforces a wall-clock deadline and always leaves run.json behind, even when
     sqlmap fails or is killed.

Exit codes: 0 sqlmap finished cleanly, 1 sqlmap failed, 2 blocked by preflight,
124 deadline exceeded, 3 sqlmap not found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import preflight  # noqa: E402  (local module)

MAX_THREADS = 10

# Where the request file is looked for when --request is omitted. The first
# non-empty match wins; "login-request.txt" is first because that is the name
# handed out for the course exercise.
REQUEST_CANDIDATES = (
    "login-request.txt",
    "requests/login-request.txt",
    "requests/target.http",
    "request.txt",
    "requests/request.txt",
)

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
REQUEST_LINE_RE = re.compile(r"^[A-Za-z]+\s+\S+\s+HTTP/\d")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def discover_request(explicit: Path | None) -> Path:
    """Resolve the request file, falling back to the well-known names."""
    if explicit is not None:
        return explicit
    for candidate in REQUEST_CANDIDATES:
        path = Path(candidate)
        try:
            if path.is_file() and path.stat().st_size:
                return path
        except OSError:
            continue
    for pattern in ("requests/*.http", "requests/*.txt"):
        for path in sorted(Path(".").glob(pattern)):
            if path.is_file() and path.stat().st_size:
                return path
    raise FileNotFoundError(
        "no request file found; looked for " + ", ".join(REQUEST_CANDIDATES)
    )


def detect_input(request: Path) -> tuple[str, str]:
    """Return the sqlmap input flag and its argument for this request file.

    sqlmap's target flags are not interchangeable:

      -r FILE   raw HTTP request captured from a proxy or browser devtools
      -l FILE   Burp/WebScarab XML export or a HAR log
      -u URL    a URL -- passing a filename here is the mistake to avoid
    """
    text = request.read_text(encoding="utf-8", errors="replace")[:65536]
    head = text.lstrip("\ufeff \t\r\n")
    if head.startswith(("<?xml", "<items", "<!DOCTYPE items")):
        return "-l", str(request)
    if head.startswith("{") and '"log"' in text:
        return "-l", str(request)

    first = next((line.strip() for line in head.splitlines() if line.strip()), "")
    if URL_RE.match(first) and not REQUEST_LINE_RE.match(first):
        # a bare URL in a .txt file: hand sqlmap the URL, not the path
        return "-u", first
    return "-r", str(request)


def sqlmap_version(sqlmap_py: Path) -> str | None:
    try:
        out = subprocess.run(
            [sys.executable, str(sqlmap_py), "--version", "--non-interactive"],
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout
        # sqlmap prints e.g. "1.9.11#stable"
        match = re.search(r"\b(\d+\.\d+(?:\.\d+)*(?:#\w+)?)", out)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None


def tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument("--sqlmap-dir", type=Path, default=Path(os.environ.get("SQLMAP_DIR", ".sqlmap")))
    parser.add_argument(
        "--request",
        type=Path,
        default=None,
        help="saved request (raw HTTP, Burp XML, HAR, or a bare URL). "
        "Default: login-request.txt, then requests/target.http",
    )
    parser.add_argument("--allowlist", type=Path, default=Path("targets/allowlist.txt"))
    parser.add_argument("--allow-unlisted", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("sqlmap-output"))
    parser.add_argument(
        "--staging-dir",
        type=Path,
        default=Path(".sqlmap-run"),
        help="space-free working directory sqlmap writes into; results are moved to --output-dir",
    )
    parser.add_argument("--log-file", type=Path, default=None)

    parser.add_argument("--threads", type=int, default=3, help=f"1..{MAX_THREADS} (default 3)")
    parser.add_argument("--level", type=int, default=1, choices=range(1, 6))
    parser.add_argument("--risk", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--technique", default=None, help="e.g. BEUSTQ")
    parser.add_argument("--dbms", default=None)
    parser.add_argument("-D", "--db", "--database", dest="database", default=None, help="DBMS database to enumerate/dump")
    parser.add_argument("-T", "--tbl", "--table", "--tables", dest="tables", default=None, help="DBMS table(s) to dump (comma-separated)")
    parser.add_argument("--targets-file", type=Path, default=None, help="file listing target tables (e.g. targets/biweekly.txt)")
    parser.add_argument("--list-tables", action="store_true", help="enumerate tables only without dumping data")
    parser.add_argument("--tamper", default=None, help="comma-separated tamper scripts")
    parser.add_argument("--proxy", default=None, help="HTTP/SOCKS proxy (e.g. http://127.0.0.1:8080 or socks5://127.0.0.1:9050)")
    parser.add_argument("--tor", action="store_true", help="route all traffic through Tor anonymity network")
    parser.add_argument("--delay", type=float, default=0.3, help="seconds between requests (default 0.3s for safe balanced execution)")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout seconds")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--dump-all", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--exclude-sysdbs", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--optimize", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--fresh", action="store_true", help="flush the sqlmap session before running")
    parser.add_argument("--max-runtime", type=int, default=0, help="hard deadline in seconds (0 = none)")
    parser.add_argument("--extra", action="append", default=[], help="extra sqlmap argument (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="print the command and exit")

    args = parser.parse_args(argv)

    # A cp1252 console cannot encode sqlmap's unicode table output; degrade to
    # '?' instead of dying halfway through a dump.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

    try:
        request = discover_request(args.request).resolve()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not request.exists():
        print(f"error: request file not found: {request}", file=sys.stderr)
        return 1

    sqlmap_py = (args.sqlmap_dir / "sqlmap.py").resolve()
    if not sqlmap_py.exists():
        print(
            f"error: sqlmap.py not found under {args.sqlmap_dir} (set --sqlmap-dir or SQLMAP_DIR)",
            file=sys.stderr,
        )
        return 3

    # ---- preflight: never scan a host that is not explicitly allowed -------
    try:
        targets = preflight.load_request_targets(request)
    except preflight.TargetParseError as exc:
        print(f"error: cannot resolve target: {exc}", file=sys.stderr)
        return 2

    allowlist = preflight.read_allowlist(args.allowlist)
    allowed, denied = preflight.check_targets(targets, allowlist)
    for item in allowed:
        print(f"target  {item['host']}:{item['port']}  allowlisted by {item['allowlist_entry']}", file=sys.stderr)
    for item in denied:
        print(f"BLOCKED {item['host']}:{item['port']}  not in {args.allowlist}", file=sys.stderr)

    if denied or not allowed:
        if not args.allow_unlisted:
            if not allowlist:
                print(
                    f"error: allowlist {args.allowlist} is missing or empty; add your authorized host first",
                    file=sys.stderr,
                )
            print("error: target not authorized, refusing to start sqlmap", file=sys.stderr)
            return 2
        print("warning: allowlist check skipped (--allow-unlisted)", file=sys.stderr)
        allowed = targets

    input_flag, input_arg = detect_input(request)
    if input_flag == "-u":
        print(
            f"note: request file contains a bare URL; invoking sqlmap as "
            f"-u {input_arg} (-u takes a URL, not a filename)",
            file=sys.stderr,
        )
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = (args.log_file or output_dir / "sqlmap-console.log").resolve()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # sqlmap re-splits its own argv on whitespace, so any path containing a
    # space ("W:\Code\Projects\hello, world\login-request.txt") is truncated at
    # the space. Stage the request and the output tree behind relative,
    # space-free names and move the results back afterwards.
    staging = args.staging_dir.resolve()
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    staged_out = staging / "out"
    if output_dir.exists() and any(output_dir.iterdir()):
        try:
            shutil.copytree(output_dir, staged_out, dirs_exist_ok=True)
        except OSError:
            pass

    if input_flag in ("-r", "-l"):
        staged_request = staging / f"request{request.suffix or '.txt'}"
        shutil.copyfile(request, staged_request)
        input_arg = os.path.relpath(staged_request, Path.cwd())
    staged_out_arg = os.path.relpath(staged_out, Path.cwd())

    threads = max(1, min(args.threads, MAX_THREADS))

    # Check for targets file if no explicit db or tables passed
    multi_targets = []
    target_candidates = [args.targets_file] if args.targets_file else [Path("targets.txt"), Path("targets/targets.txt")]
    for candidate in target_candidates:
        if candidate and candidate.exists() and not args.tables and not args.database and not args.list_tables:
            lines = [line.strip() for line in candidate.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]
            if lines:
                print(f"Loaded {len(lines)} target(s) from {candidate}", file=sys.stderr)
                multi_targets = lines
                break

    base_cmd: list[str] = [
        sys.executable,
        str(sqlmap_py),
        input_flag,
        input_arg,
        "--batch",
        "--non-interactive",
    ]
    if args.optimize:
        base_cmd.append("-o")
    if args.exclude_sysdbs:
        base_cmd.append("--exclude-sysdbs")

    common_flags: list[str] = [
        f"--threads={threads}",
        f"--level={args.level}",
        f"--risk={args.risk}",
        f"--timeout={args.timeout}",
        f"--retries={args.retries}",
        "--dump-format=CSV",
        f"--output-dir={staged_out_arg}",
        "--disable-coloring",
    ]
    if args.fresh:
        common_flags.append("--flush-session")
    if args.technique:
        common_flags.append(f"--technique={args.technique}")
    if args.dbms:
        common_flags.append(f"--dbms={args.dbms}")
    if args.tamper:
        common_flags.append(f"--tamper={args.tamper}")
    if args.delay is not None:
        common_flags.append(f"--delay={args.delay}")
    if args.proxy:
        common_flags.append(f"--proxy={args.proxy}")
    if args.tor:
        common_flags.extend(["--tor", "--check-tor"])
    common_flags.extend(args.extra)

    commands: list[tuple[str, list[str]]] = []
    if multi_targets:
        targets_by_db: dict[str, list[str]] = {}
        for item in multi_targets:
            if "." in item:
                d, _, t = item.partition(".")
                targets_by_db.setdefault(d.strip(), []).append(t.strip())
            elif ":" in item:
                d, _, t = item.partition(":")
                targets_by_db.setdefault(d.strip(), []).append(t.strip())
            else:
                targets_by_db.setdefault("default", []).append(item.strip())
        for d, tbls in targets_by_db.items():
            cmd = list(base_cmd)
            if d != "default":
                cmd.extend(["-D", d])
            cmd.extend(["-T", ",".join(tbls), "--dump"])
            cmd.extend(common_flags)
            commands.append((f"{d}: {','.join(tbls)}", cmd))
    else:
        cmd = list(base_cmd)
        if args.list_tables:
            if args.database:
                cmd.extend(["-D", args.database])
            cmd.append("--tables")
        elif args.tables:
            if args.database:
                cmd.extend(["-D", args.database])
            cmd.extend(["-T", args.tables, "--dump"])
        elif args.database and not args.dump_all:
            cmd.extend(["-D", args.database, "--dump"])
        else:
            if args.dump_all:
                cmd.append("--dump-all")
        cmd.extend(common_flags)
        commands.append(("default", cmd))

    for label, cmd in commands:
        print(f"command [{label}]: " + " ".join(cmd), file=sys.stderr)

    if args.dry_run:
        return 0

    # ---- run with a hard deadline and live log streaming -------------------
    started = time.time()
    started_iso = utc_now()
    timed_out = False
    returncode: int | None = 0
    error: str | None = None
    all_cmds: list[list[str]] = []

    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        with open(log_file, "w", encoding="utf-8", errors="replace") as log_handle:
            for label, cmd in commands:
                all_cmds.append(cmd)
                log_handle.write(f"\n=== Running sqlmap for [{label}] ===\n")
                log_handle.flush()
                process = subprocess.Popen(
                    cmd,
                    cwd=str(Path.cwd()),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )

                def pump() -> None:
                    assert process.stdout is not None
                    for line in process.stdout:
                        log_handle.write(line)
                        log_handle.flush()
                        try:
                            sys.stdout.write(line)
                            sys.stdout.flush()
                        except UnicodeEncodeError:
                            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
                            sys.stdout.write(line.encode(encoding, "replace").decode(encoding, "replace"))
                            sys.stdout.flush()

                reader = threading.Thread(target=pump, daemon=True)
                reader.start()

                try:
                    rc = process.wait(timeout=args.max_runtime or None)
                    if rc != 0:
                        returncode = rc
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.kill()
                    try:
                        returncode = process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        returncode = -1
                    error = f"deadline of {args.max_runtime}s exceeded; sqlmap killed"
                    break
                reader.join(timeout=30)
    except FileNotFoundError as exc:
        error = str(exc)
        print(f"error: {exc}", file=sys.stderr)

    # ---- move the dump tree out of staging --------------------------------
    relocated = 0
    if staged_out.exists():
        try:
            shutil.copytree(staged_out, output_dir, dirs_exist_ok=True)
            relocated = sum(1 for _ in staged_out.rglob("*"))
        except OSError as exc:
            error = f"could not move {staged_out} to {output_dir}: {exc}"
            print(f"error: {error}", file=sys.stderr)
    shutil.rmtree(staging, ignore_errors=True)

    finished = time.time()
    meta = {
        "started_at": started_iso,
        "finished_at": utc_now(),
        "duration_seconds": round(finished - started, 2),
        "command": cmd,
        "cwd": str(Path.cwd()),
        "staging_dir": str(staging),
        "relocated_entries": relocated,
        "sqlmap_version": sqlmap_version(sqlmap_py),
        "request_file": str(request),
        "input_flag": input_flag,
        "input_arg": input_arg,
        "targets": targets,
        "output_dir": str(output_dir),
        "log_file": str(log_file),
        "threads": threads,
        "database": args.database,
        "tables": args.tables,
        "dump_all": args.dump_all,
        "exclude_sysdbs": args.exclude_sysdbs,
        "level": args.level,
        "risk": args.risk,
        "returncode": returncode,
        "timed_out": timed_out,
        "error": error,
        "ok": (returncode == 0 and not timed_out),
        "log_tail": tail(log_file),
    }
    (output_dir / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    if timed_out:
        return 124
    return 0 if returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
