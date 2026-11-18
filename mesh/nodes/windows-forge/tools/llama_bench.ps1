# llama_bench.ps1 - ON-DEMAND timing lane for the mesh capacity cards (owner 2026-11-17).
#
# meshd cards carry computed cold/warm first-token estimates from GGUF headers;
# this script upgrades them to MEASURED by timing the real router and writes
# $env:MESHD_BENCH_FILE (default C:\forge\llama\bench.json), which meshd reads
# read-side at caps() time.
#
# LAWS (same as the Linux kit's llama_bench.sh):
#  - ON-DEMAND ONLY: every model request loads the model (router cold start).
#    Never a scheduled task; never alongside an image-forge render (resource law).
#  - READ-side honesty: timings from the wire (timings=1), disk speed measured
#    with a real sequential read. A model that fails keeps its previous row.
#  - RESTORE law (owner, 2026-11-17): a max_instances=1 router does NOT
#    self-restore what a measurement evicted. The bench refuses while a model
#    is loaded, and re-loads the pre-bench models at the end.
#  - The image: section is merged from the existing file, never dropped.
#  - RAM floor: refuse under 24 GiB free (start_comfy law - do not OOM the box).
#
# Run:  powershell -ExecutionPolicy Bypass -File llama_bench.ps1
param(
    [string]$RouterUrl = $(if ($env:MESHD_ROUTER_URL) { $env:MESHD_ROUTER_URL } else { "http://127.0.0.1:8082" }),
    [string]$OutFile   = $(if ($env:MESHD_BENCH_FILE) { $env:MESHD_BENCH_FILE } else { "$env:USERPROFILE\forge\llama\bench.json" })
)
$ErrorActionPreference = "Stop"
$Root = $(if ($env:MESHD_ROOT) { $env:MESHD_ROOT } else { "$env:USERPROFILE\forge" })
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$freeGb = (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB
if ($freeGb -lt 24) { Write-Error ("refusing: {0:N1} GiB free < 24 GiB floor (resource law)" -f $freeGb); exit 1 }

$Tok = if ($env:MESH_TOKEN) { $env:MESH_TOKEN } elseif ($env:MESHD_TOKEN) { $env:MESHD_TOKEN } else { "" }
if (-not $Tok) {
    $meshctl = Join-Path $Root "etc\mesh\meshctl.exe"
    if (Test-Path $meshctl) { $Tok = (& $meshctl get subagent-key | Out-String).Trim() }
}
if (-not $Tok) {
    $TokFile = Join-Path $Root "llama\subagent_key.txt"
    if (Test-Path $TokFile) { $Tok = (Get-Content $TokFile -Raw).Trim() }
}
if (-not $Tok) { Write-Error "no ONE-token (MESH_TOKEN / meshctl / llama\subagent_key.txt)"; exit 1 }
$Hdr = @{ Authorization = "Bearer $Tok" }

$Prev = @{}
if (Test-Path $OutFile) { try { $Prev = Get-Content $OutFile -Raw | ConvertFrom-Json } catch { $Prev = @{} } }

# --- disk read speed: 256 MiB sequential from the biggest registered model
$models = (Invoke-RestMethod -Uri "$RouterUrl/v1/models" -Headers $Hdr -TimeoutSec 15).data
$paths = @()
foreach ($m in $models) {
    $args = $m.status.args
    for ($i = 0; $i -lt $args.Count; $i++) {
        if ($args[$i] -eq "--model" -and ($i + 1) -lt $args.Count) { $paths += $args[$i + 1]; break }
    }
}
if (-not $paths) { Write-Error "no models registered at $RouterUrl"; exit 1 }

# BUSY-ROUTER LAW (owner incident 2026-11-17): measuring loads models, and a
# max_instances=1 router SWAPS the owner's running model out. The bench
# refuses while ANY model is loaded; run on an idle router or set
# $env:MESHD_BENCH_ALLOW_SWAP = "1" on purpose.
$loadedIds = @($models | Where-Object { $_.status.value -eq "loaded" } | ForEach-Object { $_.id })
if ($loadedIds.Count -gt 0 -and $env:MESHD_BENCH_ALLOW_SWAP -ne "1") {
    Write-Error ("router BUSY (loaded: {0}) - a measurement loads a model and a max_instances=1 router EVICTS yours. Run when idle, or MESHD_BENCH_ALLOW_SWAP=1" -f (($loadedIds | Select-Object -First 3) -join ", "))
    exit 1
}
$biggest = $paths | ForEach-Object { Get-Item $_ -ErrorAction SilentlyContinue } |
    Sort-Object Length -Descending | Select-Object -First 1
$diskMbS = 300
if ($biggest -and $biggest.Length -gt 300MB) {
    $fs = [IO.File]::Open($biggest.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $buf = New-Object byte[] (1MB)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $read = 0
    while ($read -lt 256MB) {
        $n = $fs.Read($buf, 0, $buf.Length)
        if ($n -le 0) { break }
        $read += $n
    }
    $sw.Stop(); $fs.Close()
    if ($sw.Elapsed.TotalSeconds -gt 0.05) { $diskMbS = [int]($read / 1MB / $sw.Elapsed.TotalSeconds) }
}

$bench = @{}
if ($Prev.router) { foreach ($p in $Prev.router.PSObject.Properties) { $bench[$p.Name] = $p.Value } }

function Get-Loaded([string]$Id) {
    (Invoke-RestMethod -Uri "$RouterUrl/v1/models" -Headers $Hdr -TimeoutSec 15).data |
        Where-Object { $_.id -eq $Id } | ForEach-Object { $_.status.value }
}
function Ensure-Loaded([string]$Id, [double]$BudgetSec = 900) {
    if ((Get-Loaded $Id) -eq "loaded") { return 0.0 }
    $t0 = Get-Date
    Invoke-RestMethod -Uri "$RouterUrl/models/load" -Method Post -Headers $Hdr `
        -ContentType "application/json" -Body (@{ model = $Id } | ConvertTo-Json) -TimeoutSec 60 | Out-Null
    while (((Get-Date) - $t0).TotalSeconds -lt $BudgetSec) {
        if ((Get-Loaded $Id) -eq "loaded") { return ((Get-Date) - $t0).TotalSeconds }
        Start-Sleep -Seconds 2
    }
    throw "load did not reach 'loaded' in ${BudgetSec}s"
}
function Unload-Model([string]$Id) {
    try {
        Invoke-RestMethod -Uri "$RouterUrl/models/unload" -Method Post -Headers $Hdr `
            -ContentType "application/json" -Body (@{ model = $Id } | ConvertTo-Json) -TimeoutSec 120 | Out-Null
    } catch { }
}

$originals = @($models | Where-Object { $_.status.value -eq "loaded" } | ForEach-Object { $_.id })
if ($originals.Count -gt 0) {
    Write-Host ("router busy (loaded: {0}) - the router does NOT self-restore: the bench re-loads these at the end" -f ($originals -join ", "))
}
foreach ($m in $models) {
    $mid = $m.id
    if (-not $mid) { continue }
    $row = if ($bench.ContainsKey($mid)) { $bench[$mid] } else { $null }
    if ($row -isnot [System.Management.Automation.PSCustomObject]) { $row = [pscustomobject]@{} }
    $body = @{
        model = $mid
        messages = @(@{ role = "user"; content = "Count from 1 to 120, one number per line." })
        max_tokens = 64; temperature = 0; timings = $true
    } | ConvertTo-Json -Depth 5
    try {
        # honest load timed on the router's own lane, then one warm request
        # for prompt/decode tps with the model resident
        $loadS = Ensure-Loaded $mid
        $warm = Invoke-RestMethod -Uri "$RouterUrl/v1/chat/completions" -Method Post -Headers $Hdr `
            -ContentType "application/json" -Body $body -TimeoutSec 600
        $pt2 = ($warm.timings.prompt_ms -as [double]) / 1000.0
        $et2 = ($warm.timings.eval_ms -as [double]) / 1000.0
        $row | Add-Member -Force NoteProperty load_s ([Math]::Round($loadS, 2))
        if ($pt2 -gt 0.05 -and $warm.usage.prompt_tokens) {
            $row | Add-Member -Force NoteProperty prompt_tps ([Math]::Round($warm.usage.prompt_tokens / $pt2, 1)) }
        if ($et2 -gt 0.05 -and $warm.usage.completion_tokens) {
            $row | Add-Member -Force NoteProperty decode_tps ([Math]::Round($warm.usage.completion_tokens / $et2, 1)) }
        $row | Add-Member -Force NoteProperty at ([int][DateTimeOffset]::Now.ToUnixTimeSeconds())
        $bench[$mid] = $row
        Write-Host ("  {0,-44} load {1}s prompt {2} t/s decode {3} t/s" -f `
            $mid.Substring(0, [Math]::Min(44, $mid.Length)), $row.load_s, $row.prompt_tps, $row.decode_tps)
    } catch {
        Write-Host ("  {0,-44} FAILED ({1}) - keeping the previous row" -f `
            $mid.Substring(0, [Math]::Min(44, $mid.Length)), $_.Exception.GetType().Name)
    }
    Unload-Model $mid
}

# RESTORE PHASE (owner law 2026-11-17): a max_instances=1 router does NOT
# self-restore the evicted model. Leave the router as you found it.
foreach ($mid in $originals) {
    try {
        Ensure-Loaded $mid 1800 | Out-Null
        Write-Host "restored: $mid"
    } catch {
        Write-Host ("RESTORE FAILED for {0} ({1}) - load it from the router WebUI before use" -f $mid, $_.Exception.GetType().Name)
    }
}

$out = [ordered]@{
    disk_read_mb_s = $diskMbS
    router = $bench
    image = $(if ($Prev.image) { $Prev.image } else { @{} })
    note = "measured by llama_bench.ps1 (on-demand; never a scheduled task)"
}
$tmp = "$OutFile.tmp"
New-Item -ItemType Directory -Force -Split (Split-Path $OutFile) | Out-Null
$out | ConvertTo-Json -Depth 6 | Set-Content -Path $tmp -Encoding UTF8
Move-Item -Force $tmp $OutFile
Write-Host "wrote $OutFile"
