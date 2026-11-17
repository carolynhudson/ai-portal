# bench_llama.ps1 - re-measure the lane map on the CURRENT llama.cpp build
# (BUILD.md law: after any llama.cpp update, RE-MEASURE - t/s, pp, load).
# Runs llama-bench on the library files and prints the rows; the two standard
# lanes are Qwen3-4b on GPU (11 GiB home lane) and flint on the CPU lane.
# CUDA LAW: ggml-cuda.dll needs the toolkit bin\x64 on PATH or the process dies
# 0xc0000135 / falls back to CPU silently - added here, like the launchers.
# ASCII only, PS 5.1.  Long runs: start it in the background.
param(
    [string]$Models = 'Qwen3-4b:99,flint:0',   # name:ngl pairs; ngl 99 = GPU lane, 0 = CPU lane
    [int]$Threads = 16,                        # -t 16 law (physical cores on this host)
    [string]$Pg = '512,128',                   # llama-bench -pg prompt,prediction
    [int]$Rep = 2,
    [string]$ModelDir = 'C:\forge\llama\router\models',
    [string]$Bin = 'C:\forge\llama'
)
$ErrorActionPreference = 'Continue'
$bench = Join-Path $Bin 'llama-bench.exe'
if (-not (Test-Path $bench)) { throw ('no llama-bench.exe in ' + $Bin) }

$cub = Get-ChildItem 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA' -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Where-Object { Test-Path (Join-Path $_.FullName 'bin\x64') } | Select-Object -First 1
if ($cub) { $env:PATH = (Join-Path $cub.FullName 'bin\x64') + ';' + (Join-Path $cub.FullName 'bin') + ';' + $env:PATH }

foreach ($spec in (($Models -join ',') -split ',')) {
    $parts = $spec.Split(':')
    $name = $parts[0].Trim()
    $ngl = 0
    if ($parts.Count -gt 1) { $ngl = [int]$parts[1].Trim() }
    $gguf = Get-ChildItem (Join-Path $ModelDir $name) -Filter *.gguf -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $gguf) { 'SKIP ' + $name + ' - no gguf under ' + (Join-Path $ModelDir $name); continue }
    '=== ' + $name + '  -ngl ' + $ngl + '  -t ' + $Threads + '  -pg ' + $Pg + '  (' + [math]::Round($gguf.Length / 1GB, 2) + ' GB)'
    # llama-bench writes its table to stdout; -v 0 keeps it to the summary row
    & $bench -m $gguf.FullName -ngl $ngl -t $Threads -pg $Pg -r $Rep 2>&1 |
        Select-String -Pattern 'llama-bench|ggml_cuda|load_backend|CPU backend|main.*load|^\|' |
        Select-Object -First 12
}
