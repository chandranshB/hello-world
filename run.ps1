<#
.SYNOPSIS
    One-shot local run: sqlmap dump -> CSV -> Parquet -> verify.

.DESCRIPTION
    Mirrors the GitHub Actions workflow so the exact same code path can be
    exercised on this machine. Everything is idempotent: missing Python
    dependencies are installed, a missing sqlmap checkout is cloned, and the
    Parquet tree is rebuilt from whatever CSVs are on disk.

.PARAMETER Request
    Saved request (raw HTTP, Burp XML, HAR, or a .txt holding a bare URL).
    Defaults to login-request.txt, then requests/target.http.

.PARAMETER AllowUnlisted
    Skip the allowlist check. Only for a disposable lab target you control.

.EXAMPLE
    .\run.ps1
    .\run.ps1 -Request login-request.txt -Threads 10 -MaxMinutes 240 -InferTypes
#>
[CmdletBinding()]
param(
    [string]$Request = "",
    [int]$Threads = 10,
    [int]$MaxMinutes = 240,
    [string]$OutputDir = "sqlmap-output",
    [string]$ParquetDir = "parquet",
    [switch]$AllowUnlisted,
    [switch]$Fresh,
    [switch]$InferTypes,
    [switch]$MapNull,
    [switch]$SkipSqlmap
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    function Invoke-Python {
        param([string[]]$Arguments)
        & python @Arguments
        return $LASTEXITCODE
    }

    # ---- dependencies -----------------------------------------------------
    & python -c "import pyarrow, duckdb" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "installing conversion dependencies" -ForegroundColor Cyan
        Invoke-Python @("-m", "pip", "install", "--upgrade", "pip") | Out-Null
        Invoke-Python @("-m", "pip", "install", "-r", "requirements.txt") | Out-Null
    }

    if (-not (Test-Path ".sqlmap\sqlmap.py")) {
        Write-Host "cloning sqlmap 1.9.11" -ForegroundColor Cyan
        & git clone --depth 1 --branch 1.9.11 https://github.com/sqlmapproject/sqlmap.git .sqlmap
        if ($LASTEXITCODE -ne 0) { throw "git clone of sqlmap failed" }
    }

    # ---- dump -------------------------------------------------------------
    if (-not $SkipSqlmap) {
        $sqlmapArgs = @(
            "scripts\run_sqlmap.py",
            "--sqlmap-dir", ".sqlmap",
            "--output-dir", $OutputDir,
            "--threads", "$Threads",
            "--max-runtime", "$($MaxMinutes * 60)"
        )
        if ($Request) { $sqlmapArgs += @("--request", $Request) }
        if ($AllowUnlisted) { $sqlmapArgs += "--allow-unlisted" }
        if ($Fresh) { $sqlmapArgs += "--fresh" }

        $sqlmapExit = Invoke-Python $sqlmapArgs
        if ($sqlmapExit -ne 0) {
            Write-Warning "sqlmap wrapper exited $sqlmapExit; converting whatever was dumped"
        }
    }

    if (-not (Get-ChildItem -Path $OutputDir -Recurse -Filter *.csv -ErrorAction SilentlyContinue)) {
        throw "no CSVs under $OutputDir - nothing to convert (see $OutputDir\sqlmap-console.log)"
    }

    # ---- convert ----------------------------------------------------------
    $convertArgs = @(
        "scripts\csv_to_parquet.py",
        "--dump-root", $OutputDir,
        "--out", $ParquetDir,
        "--workers", "0",
        "--compression", "zstd",
        "--checksum",
        "--catalog",
        "--run-meta", "$OutputDir\run.json",
        "--summary-md", "$ParquetDir\summary.md"
    )
    if ($InferTypes) { $convertArgs += "--infer-types" }
    if ($MapNull) { $convertArgs += @("--null-token", "NULL") }

    $convertExit = Invoke-Python $convertArgs
    if ($convertExit -ne 0) { throw "conversion failed ($convertExit)" }

    # ---- verify -----------------------------------------------------------
    $verifyExit = Invoke-Python @(
        "scripts\verify_parquet.py",
        "--manifest", "$ParquetDir\manifest.json",
        "--checksum",
        "--duckdb"
    )

    Write-Host ""
    Write-Host "parquet:  $(Resolve-Path $ParquetDir)" -ForegroundColor Green
    Write-Host "manifest: $(Resolve-Path "$ParquetDir\manifest.json")"
    Write-Host "catalog:  $(Resolve-Path "$ParquetDir\catalog.duckdb")"
    Write-Host ""
    Write-Host "query: python -c `"import duckdb; print(duckdb.connect('$ParquetDir/catalog.duckdb').sql('SHOW ALL TABLES').fetchall())`""
    exit $verifyExit
}
finally {
    Pop-Location
}
