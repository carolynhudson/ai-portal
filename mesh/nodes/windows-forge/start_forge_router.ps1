# start_forge_router.ps1 - the box's router-mode LLM endpoint (llama.cpp
# router server, port 8082): flint (general agent) + nemotron (ThinkingCap
# Nemotron-3.5-Lightning-30B-A3B, agentic/coding, MTP spec-decode) behind ONE
# OpenAI-compatible endpoint, ONE model loaded at a time (--models-max 1,
# CLI-only: preset models-max is ignored, portal KB PORTAL_PLAN sec 4.10.2).
#
# Measured 2026-11-10 (this box, build 12211): nemotron raw 15.3 t/s, MTP
# 21.1 t/s, load 4.6 s at 262k ctx (Mamba-2 hybrid); flint 19.0 t/s raw,
# load 38.7 s. Sampling: temp 1.0 top-p 0.95 (both cards), top-k 20 (flint
# card; mild for nemotron), min-p 0 (no-op). Clients override per request.
#
# Security: firewall 8082 portal-IP-only + --api-key-file (subagent_key.txt,
# NEVER printed). Launcher contract: DETACHED, per-boot logs, port guard.
#
# IDLE LAW (owner 2026-11-12, personal-PC rule): the router starts COLD (no
# load-on-startup in presets.ini) and self-manages: --sleep-idle-seconds 900
# (15 min idle -> sleep, releases resident weights; wakes on next request)
# and --models-memory-margin 16384 (keep ~16 GiB RAM free; LRU-unload models
# when a load would eat into it). The router is the ONLY network-exposed
# llama.cpp surface; the prose lane 8081 is retired (router id
# Qwen3-30b-A3B-Instruct replaces it).
param(
    [string]$Root = "C:\forge",
    [string]$Listen = "0.0.0.0",
    [int]$Port = 8082,
    [int]$Ctx = 262144,
    [int]$Threads = 16
)
$ErrorActionPreference = "Stop"
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Write-Output ("llama router already listening on port " + $Port + " - no start needed")
    exit 0
}
$ModelsDir = Join-Path $Root "llama\router\models"
$Presets = Join-Path $Root "llama\router\presets.ini"
if (-not (Test-Path $Presets)) { throw ("presets file missing: " + $Presets) }
if (-not (Test-Path (Join-Path $ModelsDir "flint\Flint-Q4_K_M.gguf"))) { throw ("flint hardlink missing under " + $ModelsDir) }
if (-not (Test-Path (Join-Path $ModelsDir "nemotron\ThinkingCap-NVIDIA-Nemotron-3.5-Lightning-30B-A3B-Q4_K_M.gguf"))) { throw ("nemotron hardlink missing under " + $ModelsDir) }
$KeyFile = Join-Path $Root "llama\subagent_key.txt"
if (-not (Test-Path $KeyFile)) {
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 32
    $rng.GetBytes($bytes)
    $key = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    Set-Content -Path $KeyFile -Value $key -Encoding ASCII -NoNewline
    Write-Output ("generated API key at " + $KeyFile + " (share with the portal owner; not printed)")
}
# MESH CERT SELF-HEAL (bench #41/#42): before the hub-first token fetch, heal
# the node leaf if close to expiry (portal mints nightly + stages the bundle
# on halo). Failure-tolerant: the file fallback below covers a failed heal.
try { & "$PSScriptRoot\mesh_selfheal.ps1" } catch { Write-Host "selfheal skipped: $($_.Exception.Message)" }

# ONE-TOKEN LAW, hub-first (mesh enrollment 2026-11-15, bench #24/#25): if the
# mesh hub grants subagent-key to this node, meshctl fetches it (age decrypt is
# client-side) and the key FILE is retired: the token is materialized per-boot
# under %USERPROFILE%\.mesh (user-private ACL) for --api-key-file. Grant not
# shared yet (403) / meshctl absent -> file fallback, token never printed.
$TokenFile = $KeyFile
$meshctl = Join-Path $Root "etc\mesh\meshctl.exe"
if (Test-Path $meshctl) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $hubKey = (& $meshctl get subagent-key 2>$null | Out-String).Trim()
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($rc -eq 0 -and $hubKey -and $hubKey.Length -ge 16) {
        $TokenFile = Join-Path $env:USERPROFILE ".mesh\router_api_key"
        Set-Content -Path $TokenFile -Value $hubKey -Encoding ASCII -NoNewline
        Write-Output "token: hub (subagent-key from mesh hub)"
    } else {
        Write-Output "token: file fallback (hub grant pending)"
    }
}
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$logOut = Join-Path $Root ("logs\router-" + $stamp + ".out.log")
$logErr = Join-Path $Root ("logs\router-" + $stamp + ".err.log")
# Fence the hub-cache model discovery (no CLI flag; portal KB 4.9.1). Router
# follows HF_HUB_CACHE for cache scanning; point it at an EMPTY forge dir so
# public model ids are ONLY the dirs under models-dir (the whole library;
# hub cache on this box is now empty - fence kept as insurance).
$CacheFence = Join-Path $Root "llama\hf-cache"
if (-not (Test-Path $CacheFence)) { New-Item -ItemType Directory -Path $CacheFence | Out-Null }
$env:HF_HUB_CACHE = $CacheFence
# CUDA LAW (2026-11-12): ggml-cuda.dll imports cublas64_13/cublasLt64_13 from
# the toolkit bin\x64; the installer adds NOTHING to PATH. A process that
# lacks these paths fails -1073741515 (exe load) or silently falls back to
# CPU (backend LoadLibrary fail). Prepend toolkit bin\x64 + bin.
$cub = Get-ChildItem "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA" -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
if ($cub) {
    $env:PATH = (Join-Path $cub.FullName "bin\x64") + ";" + (Join-Path $cub.FullName "bin") + ";" + $env:PATH
}
$exes = Join-Path $Root "llama\llama-server.exe"
$srvArgs = @("--models-dir", $ModelsDir, "--models-preset", $Presets,
             "--models-max", 1, "--host", $Listen, "--port", $Port,
             "-t", $Threads, "-ngl", 0, "-fa", "on", "--jinja",
             "--temp", 1.0, "--top-p", 0.95, "--top-k", 20, "--min-p", 0,
             "-c", $Ctx, "-np", 1,
             "--sleep-idle-seconds", 900, "--models-memory-margin", 16384,
             "--api-key-file", $TokenFile, "--no-webui")
Start-Process -FilePath $exes -ArgumentList $srvArgs -WorkingDirectory (Join-Path $Root "llama") `
    -WindowStyle Hidden -RedirectStandardOutput $logOut -RedirectStandardError $logErr
Write-Output ("launched llama router (flint + nemotron, models-max 1, CPU) on ${Listen}:${Port}; logs: " + $logOut + " / " + $logErr)
Write-Output ("models-dir: " + $ModelsDir + " presets: " + $Presets)
