# start_forge_coder.ps1 - the box's coder-lane endpoint (llama.cpp CPU,
# Qwen3.8-Flash-Next Coder GSQ-RCO IQ1_M, 58.4 GB, two shards).
#
# SCOPE LAW (owner, 2026-11-10): expert-pruned coder model - use for AGENTIC
# and CODING work ONLY; general-purpose sub-agent work goes to flint (8082).
#
# Memory law: --lazy-mode on keeps the 28.8 GB n-gram embedding table on disk
# (demand rows); resident weights ~29.6 GB. CPU lane only (-ngl 0): the 2080
# Ti stays on diffusion. Coexists with flint/prose (file-backed, reclaimable).
#
# SHELF LAW (2026-11-11): the model fileset STAYS at C:\forge\share, NOT in the
# router library C:\forge\llama\router\models - the lane is STOP (7.1 t/s, no
# MTP head); listing it in the router would let any client load the crawl.
#
# Security (same pattern as prose/subagent): firewall portal-IP-only (8083)
# + --api-key-file at C:\forge\llama\coder_key.txt (never printed).
# Launcher contract: DETACHED, per-boot logs, exits 0 fast, port guard.
param(
    [string]$Root = "C:\forge",
    [string]$Model = "C:\forge\share\Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002.gguf",
    [string]$Listen = "0.0.0.0",
    [int]$Port = 8083,
    [int]$Ctx = 262144,
    [int]$Threads = 16
)
$ErrorActionPreference = "Stop"
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Write-Output ("llama-server already listening on port " + $Port + " - no start needed")
    exit 0
}
if (-not (Test-Path $Model)) { throw ("model file missing: " + $Model) }
$KeyFile = Join-Path $Root "llama\coder_key.txt"
if (-not (Test-Path $KeyFile)) {
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 32
    $rng.GetBytes($bytes)
    $key = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    Set-Content -Path $KeyFile -Value $key -Encoding ASCII -NoNewline
    Write-Output ("generated coder API key at " + $KeyFile + " (share with the portal owner; not printed)")
}
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logOut = Join-Path $Root ("logs\coder-" + $stamp + ".out.log")
$logErr = Join-Path $Root ("logs\coder-" + $stamp + ".err.log")
$exes = Join-Path $Root "llama\llama-server.exe"
$srvArgs = @("-m", $Model, "--host", $Listen, "--port", $Port,
             "-c", $Ctx, "-np", 1, "-t", $Threads, "-ngl", 0,
             "-fa", "on", "--jinja", "--lazy-mode", "on",
             "--temp", 1.0, "--top-p", 0.95, "--top-k", 20, "--min-p", 0,
             "-a", "coder", "--api-key-file", $KeyFile, "--no-webui")
Start-Process -FilePath $exes -ArgumentList $srvArgs -WorkingDirectory (Join-Path $Root "llama") `
    -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr
Write-Output ("launched llama-server (forge-coder IQ1_M, CPU, lazy-mode) on ${Listen}:${Port}; logs: " + $logOut + " / " + $logErr)
Write-Output ("model: " + $Model + " ctx=" + $Ctx + " slots=1 threads=" + $Threads + " ngl=0")
