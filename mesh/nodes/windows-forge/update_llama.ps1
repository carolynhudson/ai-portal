# update_llama.ps1 - the mini-halo llama.cpp updater for Windows (RainbowBlock).
# Mirrors the portal pattern (PORTAL_PLAN sec 4.4): fetch -> build at a ref ->
# install to a self-contained release dir -> verify -> swap (with backup) ->
# smoke. CPU-tuned host build (the box runs llama CPU-only; GPU stays on
# ComfyUI). ON DEMAND only - run it, don't schedule it.
#   powershell -ExecutionPolicy Bypass -File .\update_llama.ps1 [-Ref master] [-Swap]
# PS 5.1, pure ASCII. Rollback: restore the .b12211-style backups (see $active).
param(
    [string]$Ref = "master",
    [switch]$Swap,
    [string]$CudaArch = "75"   # RTX 2080 Ti = Turing sm_75 (mini-halo GPU tuning knob)
)
$ErrorActionPreference = "Stop"
$build = "C:\forge\llama\build"
$repo = Join-Path $build "llama.cpp"
$releases = "C:\forge\llama\releases"
$active = "C:\forge\llama"
$tools = @("llama-server.exe", "llama-cli.exe", "llama-bench.exe", "llama-perplexity.exe")

# --- MSVC discovery (VS Build Tools 17.14; cmake ships in the install) ------
$vswhere = "C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "vswhere missing - VS Build Tools not installed?" }
$vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vsPath) { throw "no VC++ tools found via vswhere" }
$cmake = Join-Path $vsPath "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
if (-not (Test-Path $cmake)) { throw ("cmake not at " + $cmake) }
Write-Output ("VS: " + $vsPath)

# --- fetch / clone at the ref -----------------------------------------------
if (-not (Test-Path (Join-Path $repo ".git"))) {
    New-Item -ItemType Directory -Force -Path $build | Out-Null
    git clone --depth 50 https://github.com/ggml-org/llama.cpp $repo
}
Set-Location $repo
git fetch --depth 50 origin $Ref
git checkout -q FETCH_HEAD
$sha = (git rev-parse --short FETCH_HEAD)
$ver = (git describe --tags --always)
Write-Output ("built-at ref: " + $sha + " (" + $ver + ")")

# --- configure + build (mini-halo tuning, ONE build with BOTH paths) --------
$stage = Join-Path $releases ("llama.cpp-" + $sha)
New-Item -ItemType Directory -Force -Path $stage | Out-Null
# CPU: GGML_NATIVE=ON compiles the Zen 4 AVX-512 path for THIS host (MSVC).
# GPU: if a CUDA toolkit is installed, add the CUDA backend with sm_75 cubins
#      ONLY (2080 Ti). Runtime picks the lane: -ngl 0 = CPU, -ngl 99 = GPU.
$cudaArgs = @("-DGGML_CUDA=OFF")
$nvcc = Get-ChildItem "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA" -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName "bin\nvcc.exe" } |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if ($nvcc) {
    Write-Output ("CUDA toolkit: " + $nvcc + " -> building CUDA backend, arch " + $CudaArch)
    # CUDA 13 MSBuild props resolve the toolkit from the VERSIONED env var
    # (CUDA_PATH_V13_5), not CUDA_PATH. A session predating the toolkit
    # install has stale process env - import machine CUDA_* vars here.
    foreach ($name in [Environment]::GetEnvironmentVariables('Machine').Keys) {
        if ($name -like 'CUDA*') { Set-Item "Env:$name" ([Environment]::GetEnvironmentVariable($name, 'Machine')) }
    }
    # CudaToolkitDir: MSBuild's CUDA targets resolve it from the registry, and
    # silent installs can leave InstallDir empty (seen 2026-11-12 with winget
    # Nvidia.CUDA). Inject it as a VS global so the build is registry-independent.
    $cudaRoot = Split-Path (Split-Path $nvcc -Parent) -Parent
    $cudaBin = $cudaRoot
    $cudaArgs = @("-DGGML_CUDA=ON", ("-DCMAKE_CUDA_ARCHITECTURES=" + $CudaArch), ("-DCMAKE_VS_GLOBALS=CudaToolkitDir=" + $cudaRoot), ("-DCMAKE_CUDA_COMPILER=" + $nvcc))
} else {
    Write-Output "CUDA toolkit not found - CPU-only build (install CUDA, re-run for GPU path)"
}
& $cmake -S $repo -B (Join-Path $build ("bld-" + $sha)) -G "Visual Studio 17 2022" -A x64 `
    "-DCMAKE_BUILD_TYPE=Release" "-DCMAKE_INSTALL_PREFIX=$stage" `
    -DGGML_NATIVE=ON -DGGML_CPU_ALL_VARIANTS=OFF `
    @cudaArgs -DGGML_VULKAN=OFF `
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_APP=OFF
if ($LASTEXITCODE -ne 0) {
    # a failed configure poisons the cache (broken-compiler checks); wipe so
    # the next run starts fresh
    Remove-Item (Join-Path $build ("bld-" + $sha)) -Recurse -Force -ErrorAction SilentlyContinue
    throw "cmake configure failed (build dir wiped; re-run for a clean configure)"
}
& $cmake --build (Join-Path $build ("bld-" + $sha)) --config Release -j 16 --target llama-server llama-cli llama-bench llama-perplexity
if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }
# stage = the built bin payload (cmake --install would demand every target's
# artifacts, including tools we deliberately do not build)
$binOut = Join-Path (Join-Path $build ("bld-" + $sha)) "bin\Release"
New-Item -ItemType Directory -Force -Path (Join-Path $stage "bin") | Out-Null
Copy-Item (Join-Path $binOut "*") (Join-Path $stage "bin") -Recurse -Force

# --- verify the staged release before touching the active dir ---------------
$binDir = Join-Path $stage "bin"
if (-not (Test-Path (Join-Path $binDir "llama-server.exe"))) {
    $binDir = $stage   # flat layout fallback
}
foreach ($t in $tools) {
    if (-not (Test-Path (Join-Path $binDir $t))) { throw ("staged release missing " + $t) }
}
# --version lands on stderr; PS 5.1 + ErrorActionPreference=Stop turns native
# stderr into a terminating NativeCommandError, so relax the preference for probes.
# ggml-cuda.dll needs the CUDA toolkit bin on PATH (CUDA LAW, see launcher) or
# the probe dies 0xC0000135 (DLL_NOT_FOUND) and reports nothing.
if ($cudaBin) { $env:PATH = (Join-Path $cudaBin "bin\x64") + ";" + (Join-Path $cudaBin "bin") + ";" + $env:PATH }
$ua = $ErrorActionPreference
$ErrorActionPreference = "Continue"
# PS 5.1 wraps native stderr into error records EVEN UNDER REDIRECTION, giving
# "llama-cli.exe : version: ..." - so the match must not be anchored at line start.
$verFile = Join-Path $env:TEMP "llama_ver_probe.txt"
& (Join-Path $binDir "llama-cli.exe") --version *> $verFile
$ErrorActionPreference = $ua
$printed = ""
if (Test-Path $verFile) {
    foreach ($ln in (Get-Content $verFile -ErrorAction SilentlyContinue)) {
        if ($ln -match 'version:\s*(.+)$') { $printed = $Matches[1].Trim(); break }
    }
    Remove-Item $verFile -Force -ErrorAction SilentlyContinue
}
Write-Output ("built version string: " + $printed)

# --- version record (CROSS-NODE VERSION SYNC, owner ask 2026-11-16) ----------
# Each node records the llama.cpp build it ACTUALLY built, next to the install.
# meshd reports this file at GET /healthz under "llama" (null when absent), so
# the portal can compare llama.cpp versions across nodes and place work with it.
# Rule: the record is written by the builder from the ref it built - never by
# hand, and a staged (undeployed) release carries its own copy in its own dir.
$verObj = [ordered]@{
    version         = $(if ($printed) { $printed } else { $ver })
    printed_version = $printed
    sha             = $sha
    ref             = $Ref
    cuda_arch       = $CudaArch
    node            = $env:COMPUTERNAME
    source          = "updater"
    built_utc       = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}
$verPath = Join-Path $stage "version.json"
$verObj | ConvertTo-Json -Compress | Set-Content -Path $verPath -Encoding ASCII
Write-Output ("version record: " + $ver + " (" + $sha + ") -> " + $verPath)

if (-not $Swap) {
    Write-Output ("STAGED ONLY at " + $stage + " - re-run with -Swap to deploy")
    exit 0
}

# --- swap: stop lanes, back up, copy, restart -------------------------------
Write-Output "SWAP: stopping lanes (staged release verified)"
foreach ($port in @(8082, 8081)) {
    Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 3
$stampTag = "pre-" + (Get-Date -Format "yyyyMMdd-HHmmss")
foreach ($t in $tools) {
    $cur = Join-Path $active $t
    if (Test-Path $cur) { Copy-Item $cur (Join-Path $active ($t + "." + $stampTag + ".bak")) -Force }
}
Copy-Item (Join-Path $binDir "*") $active -Recurse -Force
Copy-Item $verPath (Join-Path $active "version.json") -Force
Write-Output ("binaries swapped; old ones saved as *." + $stampTag + ".bak in " + $active)
$ua = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& (Join-Path $active "llama-cli.exe") --version 2>&1 | Select-Object -First 1 | Out-String
$ErrorActionPreference = $ua

# --- restart the router lane + smoke -----------------------------------------
Start-ScheduledTask -TaskName ai-forge-llama-subagent
Start-Sleep -Seconds 10
$key = (Get-Content "C:\forge\llama\subagent_key.txt" -Raw).Trim()
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8082/v1/models" -Headers @{ Authorization = ("Bearer " + $key) } -TimeoutSec 30
    Write-Output ("SMOKE: router lists " + $r.data.Count + " ids")
} catch {
    throw ("SMOKE FAILED: " + $_.Exception.Message + " - restore the .bak files and restart the task")
}
Write-Output ("DONE: llama.cpp " + $sha + " live; release staged at " + $stage)
