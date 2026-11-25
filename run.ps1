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
    [int]$Threads = 3,
    [double]$Delay = 0.3,
    [int]$MaxMinutes = 240,
    [string]$Database = "",
    [string]$Tables = "",
    [string[]]$Targets = @(),
    [string]$TargetsFile = "",
    [string]$Proxy = "",
    [switch]$Tor,
    [switch]$ListTables,
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
        $baseArgs = @(
            "scripts\run_sqlmap.py",
            "--sqlmap-dir", ".sqlmap",
            "--output-dir", $OutputDir,
            "--threads", "$Threads",
            "--delay", "$Delay",
            "--max-runtime", "$($MaxMinutes * 60)"
        )
        if ($Request) { $baseArgs += @("--request", $Request) }
        if ($Proxy) { $baseArgs += @("--proxy", $Proxy) }
        if ($Tor) { $baseArgs += "--tor" }
        if ($AllowUnlisted) { $baseArgs += "--allow-unlisted" }
        if ($Fresh) { $baseArgs += "--fresh" }

        if ($ListTables) {
            $sqlmapArgs = $baseArgs + @("--list-tables")
            if ($Database) { $sqlmapArgs += @("-D", $Database) }
            $sqlmapExit = Invoke-Python $sqlmapArgs
            if ($sqlmapExit -eq 0) {
                Write-Host "Table enumeration complete." -ForegroundColor Green
            }
            return
        }

        # Check if multiple targets in "db.table" format are specified
        $allTargets = @()
        if ($TargetsFile -and (Test-Path $TargetsFile)) {
            $allTargets += Get-Content $TargetsFile | Where-Object { $_ -and -not $_.Trim().StartsWith('#') }
        } elseif (-not $Targets -and -not $Tables -and -not $Database -and (Test-Path "targets.txt")) {
            $fileLines = Get-Content "targets.txt" | Where-Object { $_ -and -not $_.Trim().StartsWith('#') }
            if ($fileLines.Count -gt 0) {
                Write-Host "Found targets.txt with $($fileLines.Count) target table(s)." -ForegroundColor Yellow
                $allTargets += $fileLines
            }
        }
        if ($Targets) { $allTargets += $Targets }
        if ($Tables -and $Tables -match '[.:]') { $allTargets += $Tables -split ',' }

        if ($allTargets.Count -gt 0) {
            $grouped = [ordered]@{}
            foreach ($item in $allTargets) {
                $trimmed = $item.Trim()
                if ($trimmed -match '^([^.:]+)[.:](.+)$') {
                    $d = $matches[1]
                    $t = $matches[2]
                    if (-not $grouped.Contains($d)) { $grouped[$d] = [System.Collections.Generic.List[string]]::new() }
                    $grouped[$d].Add($t)
                } else {
                    $d = if ($Database) { $Database } else { "default" }
                    if (-not $grouped.Contains($d)) { $grouped[$d] = [System.Collections.Generic.List[string]]::new() }
                    $grouped[$d].Add($trimmed)
                }
            }

            foreach ($d in $grouped.Keys) {
                $tblStr = $grouped[$d] -join ","
                Write-Host "Dumping database [$d] table(s) [$tblStr]..." -ForegroundColor Cyan
                $groupArgs = $baseArgs + @("-D", $d, "-T", $tblStr)
                $sqlmapExit = Invoke-Python $groupArgs
                if ($sqlmapExit -ne 0) {
                    Write-Warning "sqlmap dump for $d exited with $sqlmapExit"
                }
            }
        } else {
            $sqlmapArgs = $baseArgs
            if ($Database) { $sqlmapArgs += @("-D", $Database) }
            if ($Tables) { $sqlmapArgs += @("-T", $Tables) }

            $sqlmapExit = Invoke-Python $sqlmapArgs
            if ($sqlmapExit -ne 0) {
                Write-Warning "sqlmap wrapper exited $sqlmapExit; converting whatever was dumped"
            }
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
        "--workers", "1",
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

    # ---- package release assets (Parquet + SQLite) ------------------------
    Write-Host "packaging release assets (Parquet & SQLite)" -ForegroundColor Cyan
    Invoke-Python @(
        "scripts\package_release_assets.py",
        "--parquet-dir", $ParquetDir,
        "--out-dir", "release_assets"
    ) | Out-Null

    Write-Host ""
    Write-Host "parquet:  $(Resolve-Path $ParquetDir)" -ForegroundColor Green
    Write-Host "sqlite:   $(Resolve-Path "release_assets")" -ForegroundColor Green
    Write-Host "manifest: $(Resolve-Path "$ParquetDir\manifest.json")"
    Write-Host "catalog:  $(Resolve-Path "$ParquetDir\catalog.duckdb")"
    Write-Host ""
    Write-Host "query: python -c `"import duckdb; print(duckdb.connect('$ParquetDir/catalog.duckdb').sql('SHOW ALL TABLES').fetchall())`""
    exit $verifyExit
}
finally {
    Pop-Location
}
