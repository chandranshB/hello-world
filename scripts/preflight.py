#!/usr/bin/env python3
"""
Resolve the target host(s) of an sqlmap request file and check them against an
allowlist.

Supported request formats (matching sqlmap's own -r / -l inputs):

  * raw HTTP request   (what sqlmap -r expects)
  * Burp XML export    (what sqlmap -l expects)
  * HAR log            (what sqlmap -l expects)

Exit codes:
  0  every target is allowlisted
  1  the request file could not be parsed
  2  at least one target is not allowlisted
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


class TargetParseError(Exception):
    pass


def _split_host_header(value: str) -> tuple[str, int | None]:
    """Split 'host:port' / '[v6]:port' / 'host' into (host, port)."""
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        return host, int(port) if port.isdigit() else None
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host, int(port)
    return value, None


def _target(host: str, scheme: str | None, port: int | None, source: str) -> dict[str, Any]:
    return {
        "host": host.strip(),
        "scheme": scheme,
        "port": port,
        "source": source,
    }


def parse_raw_request(text: str, default_scheme: str = "http") -> list[dict[str, Any]]:
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or not lines[0].strip():
        raise TargetParseError("empty request file")

    request_line = re.match(r"^\s*([A-Za-z]+)\s+(\S+)\s+HTTP/", lines[0])
    if not request_line:
        raise TargetParseError(
            "first line is not an HTTP request line (expected 'METHOD /path HTTP/1.1')"
        )

    target = request_line.group(2)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()

    if target.lower().startswith(("http://", "https://")):
        parts = urlsplit(target)
        scheme = parts.scheme.lower()
        return [
            _target(
                parts.hostname or "",
                scheme,
                parts.port or DEFAULT_PORTS.get(scheme, 80),
                "request-line",
            )
        ]

    raw_host = headers.get("host")
    if not raw_host:
        raise TargetParseError("no absolute URL and no Host header")

    host, port = _split_host_header(raw_host)
    if not host:
        raise TargetParseError("empty Host header")
    return [_target(host, default_scheme, port or DEFAULT_PORTS[default_scheme], "host-header")]


def parse_burp_xml(text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise TargetParseError(f"invalid Burp XML: {exc}") from exc

    targets: list[dict[str, Any]] = []
    for item in root.iter("item"):
        protocol = (item.findtext("protocol") or "http").strip().lower()
        host = (item.findtext("host") or "").strip()
        url_text = (item.findtext("url") or "").strip()
        port_text = (item.findtext("port") or "").strip()

        if not host and url_text:
            parts = urlsplit(url_text)
            host = parts.hostname or ""
            protocol = parts.scheme or protocol

        if not host:
            continue

        port = int(port_text) if port_text.isdigit() else (urlsplit(url_text).port if url_text else None)
        targets.append(_target(host, protocol, port or DEFAULT_PORTS.get(protocol, 80), "burp-item"))

    if not targets:
        raise TargetParseError("Burp XML contains no <item> targets")
    return targets


def parse_har(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TargetParseError(f"invalid HAR JSON: {exc}") from exc

    entries = (data.get("log") or {}).get("entries") or []
    targets: list[dict[str, Any]] = []
    for entry in entries:
        url = ((entry.get("request") or {}).get("url") or "").strip()
        if not url:
            continue
        parts = urlsplit(url)
        if not parts.hostname:
            continue
        scheme = (parts.scheme or "http").lower()
        targets.append(
            _target(parts.hostname, scheme, parts.port or DEFAULT_PORTS.get(scheme, 80), "har-entry")
        )

    if not targets:
        raise TargetParseError("HAR log contains no request URLs")
    return targets


def load_request_targets(path: Path, default_scheme: str = "http") -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
    head = text[:4096].lstrip()

    if head.startswith(("<?xml", "<items", "<!DOCTYPE items")):
        return parse_burp_xml(text)
    if head.startswith("{") and '"log"' in text[:65536]:
        return parse_har(text)
    return parse_raw_request(text, default_scheme=default_scheme)


def read_allowlist(path: Path) -> list[str]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.append(line.lower())
    return entries


def _entry_parts(entry: str) -> tuple[str, int | None]:
    if entry.startswith("["):
        match = re.match(r"^\[([^]]+)](?::(\d+))?$", entry)
        if match:
            return match.group(1), int(match.group(2)) if match.group(2) else None
        return entry, None
    if entry.count(":") == 1:
        host, _, port = entry.partition(":")
        if port.isdigit():
            return host, int(port)
    return entry, None


def target_allowed(host: str, port: int | None, allowlist: list[str]) -> str | None:
    """Return the allowlist entry that matches, else None."""
    host = host.strip().lower().rstrip(".")

    for entry in allowlist:
        entry_host, entry_port = _entry_parts(entry)
        if entry_port is not None and port is not None and int(entry_port) != int(port):
            continue

        if "/" in entry_host:
            try:
                if ipaddress.ip_address(host) in ipaddress.ip_network(entry_host, strict=False):
                    return entry
            except ValueError:
                continue
            continue

        if entry_host.startswith("*."):
            suffix = entry_host[1:]  # ".example.com"
            if host.endswith(suffix) and len(host) > len(suffix):
                return entry
            continue

        try:
            if ipaddress.ip_address(entry_host) == ipaddress.ip_address(host):
                return entry
        except ValueError:
            pass

        if host == entry_host:
            return entry

    return None


def check_targets(
    targets: list[dict[str, Any]], allowlist: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    allowed, denied = [], []
    for item in targets:
        match = target_allowed(item["host"], item["port"], allowlist)
        item = dict(item, allowlist_entry=match)
        (allowed if match else denied).append(item)
    return allowed, denied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--request", required=True, type=Path, help="raw HTTP request, Burp XML or HAR log")
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=Path("targets/allowlist.txt"),
        help="file with one allowed host/CIDR per line (default: targets/allowlist.txt)",
    )
    parser.add_argument(
        "--allow-unlisted",
        action="store_true",
        help="skip the allowlist check (only for disposable lab targets you control)",
    )
    parser.add_argument("--default-scheme", default="http", choices=["http", "https"])
    parser.add_argument("--json", action="store_true", help="emit machine-readable output on stdout")
    args = parser.parse_args(argv)

    if not args.request.exists():
        print(f"error: request file not found: {args.request}", file=sys.stderr)
        return 1

    try:
        targets = load_request_targets(args.request, default_scheme=args.default_scheme)
    except TargetParseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    allowlist = read_allowlist(args.allowlist)
    allowed, denied = check_targets(targets, allowlist)

    if args.allow_unlisted:
        allowed, denied = targets, []

    result = {
        "request": str(args.request),
        "allowlist": str(args.allowlist),
        "allowlist_entries": len(allowlist),
        "allowed": allowed,
        "denied": denied,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for item in allowed:
            print(
                f"allowed  {item['host']}:{item['port']}  ({item['source']}, via {item['allowlist_entry']})",
                file=sys.stderr,
            )
        for item in denied:
            print(
                f"BLOCKED  {item['host']}:{item['port']}  ({item['source']}) not in {args.allowlist}",
                file=sys.stderr,
            )
        if not allowlist and not args.allow_unlisted:
            print(
                f"error: allowlist {args.allowlist} is missing or empty; add the host you are authorized to test",
                file=sys.stderr,
            )

    if args.allow_unlisted:
        print("warning: allowlist check skipped (--allow-unlisted)", file=sys.stderr)
        return 0

    return 0 if allowed and not denied else 2


if __name__ == "__main__":
    raise SystemExit(main())
