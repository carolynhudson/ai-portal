# start_llama_server.ps1 - the box's CPU prose endpoint (llama.cpp, Qwen3-30B-A3B)
#
# Role: LLM prose lane on the 7950X3D CPU (PE prompt rewriting, SVG authoring)
# so the 2080 Ti stays 100 pct on diffusion with the UNet WARM. Measured 2026-11-08:
# decode 21.6 t/s on CPU (vs 1.47 t/s on the GPU) - see REPORT addendum d.
#
# Security (2026-11-08): llama-server serves an OpenAI-compatible API. Two gates:
#   1. firewall_llama.ps1: inbound 8081 from the portal IP ONLY (same style as 8188).
#   2. --api-key-file: random per-box key generated on first start, stored at
#      C:\forge\llama\api_key.txt. The key is NEVER printed by this script.
#
# Launcher contract (same as start_comfyui.ps1): DETACHED Start-Process,
# stdout/stderr to per-boot logs, exits 0 fast, idempotent (port guard).
param(
    [string]$Root = "C:\forge",
    [string]$Model = "C:\forge\llama\router\models\Qwen3-30b-A3B-Instruct\Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf",
    [string]$Listen = "0.0.0.0",
    [int]$Port = 8081,
    [int]$Ctx = 16384,
    [int]$Parallel = 2,
    [int]$Threads = 16
)
$ErrorActionPreference = "Stop"
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Write-Output ("llama-server already listening on port " + $Port + " - no start needed")
    exit 0
}
if (-not (Test-Path $Model)) { throw ("model file missing: " + $Model) }
$KeyFile = Join-Path $Root "llama\api_key.txt"
if (-not (Test-Path $KeyFile)) {
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 32
    $rng.GetBytes($bytes)
    $key = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    Set-Content -Path $KeyFile -Value $key -Encoding ASCII -NoNewline
    Write-Output ("generated prose API key at " + $KeyFile + " (share with the portal owner; not printed)")
}
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logOut = Join-Path $Root ("logs\llama-" + $stamp + ".out.log")
$logErr = Join-Path $Root ("logs\llama-" + $stamp + ".err.log")
$exes = Join-Path $Root "llama\llama-server.exe"
$srvArgs = @("-m", $Model, "--host", $Listen, "--port", $Port,
             "-c", $Ctx, "-np", $Parallel, "-t", $Threads,
             "-lm", "mlock", "-a", "forge-prose",
             "--api-key-file", $KeyFile, "--no-webui")
Start-Process -FilePath $exes -ArgumentList $srvArgs -WorkingDirectory (Join-Path $Root "llama") `
    -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr
Write-Output ("launched llama-server (forge-prose, CPU) on ${Listen}:${Port}; logs: " + $logOut + " / " + $logErr)
Write-Output ("model: " + $Model + " ctx=" + $Ctx + " slots=" + $Parallel + " threads=" + $Threads)
