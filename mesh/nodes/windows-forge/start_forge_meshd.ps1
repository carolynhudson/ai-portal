# start_forge_meshd.ps1 - launch the meshd node daemon (phase 1: status server).
# meshd is a REAL APP (Rust exe) per the app_not_script law; PowerShell here is
# pure glue. Staged binary: C:\forge\bin\meshd.exe
# (build: cargo build --release in C:\forge\dev\meshd, then copy target\release).
# ASCII only; PS 5.1.
param(
    [string]$Root = "C:\forge",
    [int]$Port = 8092
)
# MESH CERT SELF-HEAL (bench #41/#42): meshd resolves its bearer token hub-first
# at startup, so heal the node leaf first. Failure-tolerant (file fallback).
try { & "$PSScriptRoot\mesh_selfheal.ps1" } catch { Write-Host "selfheal skipped: $($_.Exception.Message)" }
$exe = Join-Path $Root "bin\meshd.exe"
if (-not (Test-Path $exe)) {
    Write-Host "missing $exe - build: cargo build --release in C:\forge\dev\meshd, copy to $Root\bin"
    exit 1
}
$listen = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listen) {
    $owning = ($listen | Select-Object -First 1).OwningProcess
    Write-Host "port $Port already listening (pid $owning) - meshd is up; stop it first to restart"
    exit 0
}
$logdir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force $logdir | Out-Null
Start-Process -FilePath $exe -ArgumentList "status-server", "--port", "$Port" `
    -WindowStyle Hidden -WorkingDirectory (Split-Path $exe) `
    -RedirectStandardOutput (Join-Path $logdir "meshd.out.log") `
    -RedirectStandardError (Join-Path $logdir "meshd.err.log")
Start-Sleep -Seconds 2
$up = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($up) {
    Write-Host "meshd up on port $Port (log: $logdir\meshd.err.log)"
} else {
    Write-Host "FAILED to start - tail of meshd.err.log:"
    Get-Content (Join-Path $logdir "meshd.err.log") -Tail 5 -ErrorAction SilentlyContinue
    exit 1
}
