# Request files

`scripts/run_sqlmap.py` picks the sqlmap input flag from the file contents:

| File contents | sqlmap flag | Notes |
| --- | --- | --- |
| Raw HTTP request (`POST /path HTTP/1.1` + headers + blank line + body) | `-r` | Use `login-request.txt` in repo root or `requests/example.http` as template |
| Burp XML export (`<?xml ...><items>`) | `-l` | Burp "Save items" export |
| HAR log (`{"log": {"entries": [...]}}`) | `-l` | Browser devtools export |
| Bare URL on one line | `-u` | The wrapper passes the URL, not the filename |

## Where to upload `login-request.txt` (practical day)

Pick one:

1. **Repo root (simplest):** replace [`../login-request.txt`](../login-request.txt)
   with the file your instructor gives you, commit, and push. The scheduled
   workflow looks for `login-request.txt` automatically.
2. **Repository secret (recommended if it has cookies/tokens):** create an
   Actions secret named `SQLMAP_REQUEST_B64` containing the base64-encoded
   request file. The workflow materializes it at runtime as `requests/live.http`
   and never commits it.

   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\to\login-request.txt")) | Set-Clipboard
   ```

   ```bash
   base64 -w0 login-request.txt
   ```

3. **Manual workflow run:** start the `sqlmap-dump-to-parquet` workflow from the
   Actions tab and set `request_path` to a committed request file path.

The target host must also be listed in [`../targets/allowlist.txt`](../targets/allowlist.txt)
or supplied through the `ALLOWED_HOSTS` secret. The workflow and local runner
refuse to start sqlmap otherwise.

## Local run

```powershell
python -m pip install -r requirements.txt
git clone --depth 1 --branch 1.9.11 https://github.com/sqlmapproject/sqlmap.git .sqlmap

# dry run: resolve target, print exact sqlmap command
python scripts/run_sqlmap.py --request login-request.txt --allowlist targets/allowlist.txt --dry-run

# real run
python scripts/run_sqlmap.py --request login-request.txt --allowlist targets/allowlist.txt `
  --output-dir sqlmap-output --threads 10 --max-runtime 14400

# convert
python scripts/csv_to_parquet.py --dump-root sqlmap-output --out parquet `
  --workers 0 --compression zstd --checksum --catalog --run-meta sqlmap-output/run.json

# verify
python scripts/verify_parquet.py --manifest parquet/manifest.json --checksum --duckdb
```

Query the result:

```powershell
python -c "import duckdb; con=duckdb.connect('parquet/catalog.duckdb'); print(con.sql('SHOW ALL TABLES').fetchall())"
```
