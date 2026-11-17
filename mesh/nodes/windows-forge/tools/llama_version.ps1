# llama_version.ps1 - record / show this node's ACTIVE llama.cpp build
# (CROSS-NODE VERSION SYNC, owner ask 2026-11-16). meshd reports the file this
# writes (C:\forge\llama\version.json) at GET /healthz under "llama", so the
# portal compares llama.cpp versions across nodes. update_llama.ps1 writes the
# same shape at build time (source "updater"); this tool covers the cases it
# did not: a build made by hand, a staged release deployed without the
# updater, a fresh node.
#
# Measurement law: report only what is measured, and never let the BUILD repo
# stand in for the ACTIVE install - after a staged build the repo sits at the
# incoming ref, not at what is running. So: match the active exe (length +
# mtime) against the staged release dirs to learn its sha, and say where the
# answer came from in "source".
# ASCII only, PS 5.1.  -Show prints without writing.
param(
    [string]$ActiveDir = 'C:\forge\llama',
    [string]$Releases = 'C:\forge\llama\releases',
    [string]$Repo = 'C:\forge\llama\build\llama.cpp',
    [string]$OutFile = '',
    [string]$CudaArch = '75',
    [switch]$Show
)
$ErrorActionPreference = 'Continue'
if (-not $OutFile) { $OutFile = Join-Path $ActiveDir 'version.json' }

$exe = Join-Path $ActiveDir 'llama-server.exe'
$fi = Get-Item $exe -ErrorAction SilentlyContinue
if (-not $fi) { Write-Output 'NO llama-server.exe in ActiveDir - nothing measured'; exit 1 }
$mtimeUtc = $fi.LastWriteTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

# 1. find the staged release whose binary IS the active one (size + mtime)
$sha = ''
$source = ''
foreach ($d in (Get-ChildItem $Releases -Directory -ErrorAction SilentlyContinue)) {
    foreach ($cand in @((Join-Path $d.FullName 'bin\llama-server.exe'), (Join-Path $d.FullName 'llama-server.exe'))) {
        $ci = Get-Item $cand -ErrorAction SilentlyContinue
        if ($ci -and $ci.Length -eq $fi.Length -and $ci.LastWriteTime -eq $fi.LastWriteTime) {
            if ($d.Name -match 'llama\.cpp-([0-9a-f]{7,40})$') { $sha = $Matches[1]; $source = 'release-match' }
            break
        }
    }
    if ($sha) { break }
}
if (-not $sha) { $sha = $mtimeUtc; $source = 'exe-mtime' }

# 2. version string: describe the sha only when that object exists HERE
$ver = ''
if ($source -eq 'release-match' -and (Test-Path (Join-Path $Repo '.git'))) {
    Push-Location $Repo
    $ver = (& git describe --tags --always $sha 2>$null | Out-String).Trim()
    Pop-Location
    if ($LASTEXITCODE -ne 0) { $ver = '' }
}
if (-not $ver) { $ver = 'sha-' + $sha }

# 3. the exe's own version string (what a human compares across machines:
# "version: 1.0.0-dev (build 50, commit 9581154)"). ggml-cuda.dll imports the
# cublas DLLs from the CUDA toolkit dir, which is on PATH only inside the
# launchers (CUDA LAW), so add it here too - else the probe dies 0xC0000135.
$printed = ''
$cub = Get-ChildItem 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA' -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Where-Object { Test-Path (Join-Path $_.FullName 'bin\x64') } | Select-Object -First 1
if ($cub) { $env:PATH = (Join-Path $cub.FullName 'bin\x64') + ';' + (Join-Path $cub.FullName 'bin') + ';' + $env:PATH }
$ua = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
# PS 5.1 wraps a native stderr line into an error record EVEN UNDER REDIRECTION
# ("llama-server.exe : version: 1.0.0-dev (build 50, commit 9581154)"), so the
# match must not be anchored at line start - the "exe : " prefix is part of the
# line. The "At <script> char:N" noise lands on the following lines, not here.
$vfile = Join-Path $env:TEMP 'llama_version_probe.txt'
& $exe --version *> $vfile
$ErrorActionPreference = $ua
$printed = ''
if (Test-Path $vfile) {
    foreach ($ln in (Get-Content $vfile -ErrorAction SilentlyContinue)) {
        if ($ln -match 'version:\s*(.+)$') { $printed = $Matches[1].Trim(); break }
    }
    Remove-Item $vfile -Force -ErrorAction SilentlyContinue
}

$rec = [ordered]@{
    version        = $(if ($printed) { $printed } else { $ver })
    printed_version = $printed
    sha            = $sha
    cuda_arch      = $CudaArch
    node           = $env:COMPUTERNAME
    source         = $source
    exe_version    = $fi.VersionInfo.FileVersion
    exe_mtime      = $mtimeUtc
    built_utc      = $mtimeUtc
    measured_utc   = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
}
$json = $rec | ConvertTo-Json -Compress
if ($Show) { Write-Output $json; exit 0 }
$dir = Split-Path $OutFile -Parent
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
$json | Set-Content -Path $OutFile -Encoding ASCII
Write-Output ('wrote ' + $OutFile + ': ' + $json)
