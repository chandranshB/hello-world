# sqlmap dump to Parquet

This repo runs sqlmap from a saved HTTP request, dumps all accessible non-system
databases, converts every dumped CSV table to Parquet, verifies the Parquet
files, and uploads the results as GitHub Actions artifacts.

## Where to upload `login-request.txt`

The workflow looks for the request file in this order:

1. `SQLMAP_REQUEST_B64` repository secret (base64 of the raw request) - best if
   the request contains cookies/tokens you do not want committed.
2. The `request_path` workflow input (manual runs).
3. `login-request.txt` in the repository root.
4. `requests/login-request.txt`
5. `requests/target.http`

**For practical day:** replace the repo-root [`login-request.txt`](login-request.txt)
with the exact request file your instructor gives you, commit it, and push. The
scheduled cron job and manual workflow runs will pick it up automatically.

You can also upload it without committing it:

1. Go to **Settings -> Secrets and variables -> Actions**.
2. Create a secret named `SQLMAP_REQUEST_B64`.
3. Paste the base64-encoded contents of the request file.

PowerShell:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\login-request.txt")) | Set-Clipboard
```

Linux/macOS:

```bash
base64 -w0 login-request.txt
```

## Important: request file vs URL

`login-request.txt` is a saved HTTP request file. sqlmap expects it with `-r`,
not `-u`:

```bash
# Correct for a saved request file
python .sqlmap/sqlmap.py -r login-request.txt --dump-all --exclude-sysdbs --batch -o --threads=10

# -u takes a URL, not a file path
# sqlmap -u login-request.txt   # WRONG for a request file
```

`scripts/run_sqlmap.py` detects the file format automatically and uses the
correct sqlmap flag (`-r` for raw HTTP, `-l` for Burp XML/HAR, `-u` only when
the file contains a bare URL).

## Allowlist

Before sqlmap runs, [`scripts/preflight.py`](scripts/preflight.py) resolves the
target host from the request and checks it against
[`targets/allowlist.txt`](targets/allowlist.txt). If the host is not listed, the
run is blocked.

Add your authorized lab/practical host to `targets/allowlist.txt`, or set the
`ALLOWED_HOSTS` repository secret (newline-separated entries). For a disposable
local/lab target you control, manual workflow runs can set `allow_unlisted`.

## GitHub Actions

Workflow: [`.github/workflows/sqlmap-parquet.yml`](.github/workflows/sqlmap-parquet.yml)

- Runs nightly by cron and can be started manually with **Run workflow**.
- Installs Python dependencies, clones a pinned sqlmap release, runs preflight,
  runs sqlmap, converts dumped CSVs to Parquet, verifies Parquet, and uploads
  artifacts.
- Uses the maximum practical GitHub-hosted compute settings exposed by the
  workflow: `threads=10`, all CPU cores for Parquet conversion, and a long
  runtime limit.

Artifacts:

- `parquet-<run-id>` - Parquet files, `manifest.json`, `manifest.csv`,
  `summary.md`, and optional DuckDB catalog.
- `sqlmap-logs-<run-id>` - `run.json`, `sqlmap-console.log`, and sqlmap logs.

## Local run

PowerShell wrapper:

```powershell
.\run.ps1 -Request login-request.txt -Threads 10 -MaxMinutes 240
```

Manual commands:

```powershell
python -m pip install -r requirements.txt
git clone --depth 1 --branch 1.9.11 https://github.com/sqlmapproject/sqlmap.git .sqlmap

python scripts/run_sqlmap.py --request login-request.txt --allowlist targets/allowlist.txt `
  --output-dir sqlmap-output --threads 10 --max-runtime 14400

python scripts/csv_to_parquet.py --dump-root sqlmap-output --out parquet `
  --workers 0 --compression zstd --checksum --catalog --run-meta sqlmap-output/run.json

python scripts/verify_parquet.py --manifest parquet/manifest.json --checksum --duckdb
```

## Output layout

```text
sqlmap-output/
  run.json
  sqlmap-console.log
  <host>/dump/<db>/<table>.csv

parquet/
  manifest.json
  manifest.csv
  summary.md
  catalog.duckdb        # optional
  catalog.sql           # optional
  <db>/<table>.parquet
```

## Safety

Only run sqlmap against systems you are explicitly authorized to test. Keep the
allowlist narrow. Do not commit live session cookies or secrets; use
`SQLMAP_REQUEST_B64` when the request contains sensitive values.
