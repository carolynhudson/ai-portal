# start_forge_subagent.ps1 - the box's agentic sub-agent endpoint
# (llama.cpp CPU lane, Recursive Flint 40B-A2.5B Q4_K_M).
#
# Role: a general agentic sub-agent (tool-calling, reasoning, 262k context)
# served over the OpenAI-compatible API for the portal and LAN clients.
# CPU lane ONLY (-ngl 0): the 2080 Ti stays 100 pct on diffusion - same
# separation as forge-prose. Measured 2026-11-10 (this box, llama-bench 12211):
# decode 19.0 t/s at 16 threads (32 threads: 15.0 - dual-socket thrash),
# pp 166-177 t/s, load 38.7 s at 262k ctx, tool-call + reasoning verified.
#
# Security (same two gates as forge-prose):
#   1. firewall_forge_subagent.ps1: inbound 8082 from the portal IP ONLY.
#   2. --api-key-file: random per-box key at C:\forge\llama\subagent_key.txt,
#      NEVER printed here.
#
# Launcher contract: DETACHED Start-Process, per-boot logs, exits 0 fast,
# idempotent (port guard).
param(
    [string]$Root = "C:\forge",
    [string]$Model = "C:\forge\llama\router\models\flint\Flint-Q4_K_M.gguf",
    [string]$Listen = "0.0.0.0",
    [int]$Port = 8082,
    [int]$Ctx = 262144,
    [int]$Threads = 16
)
$ErrorActionPreference = "Stop"
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Write-Output ("llama-server already listening on port " + $Port + " - no start needed")
    exit 0
}
if (-not (Test-Path $Model)) { throw ("model file missing: " + $Model) }
$KeyFile = Join-Path $Root "llama\subagent_key.txt"
if (-not (Test-Path $KeyFile)) {
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 32
    $rng.GetBytes($bytes)
    $key = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    Set-Content -Path $KeyFile -Value $key -Encoding ASCII -NoNewline
    Write-Output ("generated subagent API key at " + $KeyFile + " (share with the portal owner; not printed)")
}
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logOut = Join-Path $Root ("logs\subagent-" + $stamp + ".out.log")
$logErr = Join-Path $Root ("logs\subagent-" + $stamp + ".err.log")
$exes = Join-Path $Root "llama\llama-server.exe"
$srvArgs = @("-m", $Model, "--host", $Listen, "--port", $Port,
             "-c", $Ctx, "-np", 1, "-t", $Threads, "-ngl", 0,
             "-fa", "on", "-ctk", "f16", "-ctv", "f16", "--jinja",
             "--temp", 1.0, "--top-p", 0.95, "--top-k", 20, "--min-p", 0,
             "-a", "flint", "--api-key-file", $KeyFile, "--no-webui")
Start-Process -FilePath $exes -ArgumentList $srvArgs -WorkingDirectory (Join-Path $Root "llama") `
    -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr
Write-Output ("launched llama-server (forge-subagent flint, CPU) on ${Listen}:${Port}; logs: " + $logOut + " / " + $logErr)
Write-Output ("model: " + $Model + " ctx=" + $Ctx + " slots=1 threads=" + $Threads + " ngl=0")
