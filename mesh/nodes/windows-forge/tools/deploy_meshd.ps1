# deploy_meshd.ps1 - stage a new meshd.exe and cycle the service (Windows node).
# Order matters and is the whole point: STOP the listener, THEN back up and
# replace the exe (Windows refuses to overwrite a running image), THEN start the
# task, THEN prove the new version answers. meshd is stateless (the lease ledger
# is a file), so a cycle costs one request's latency and never touches the
# router, the agent harness, or ComfyUI. ASCII only, PS 5.1.
param(
    [string]$Exe = 'C:\forge\src\ai-portal\mesh\meshd\target\release\meshd.exe',
    [string]$Bin = 'C:\forge\bin\meshd.exe',
    [string]$Task = 'ai-forge-meshd',
    [int]$Port = 8092,
    [switch]$NoRestart
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $Exe)) { throw ('source exe missing: ' + $Exe) }
$src = Get-Item $Exe
'source: ' + $src.FullName + '  ' + [math]::Round($src.Length / 1MB, 1) + ' MB  ' + $src.LastWriteTime

# 1. stop the listener (task first, then the process on the port)
Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
$conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    'stopped pid ' + $conn.OwningProcess + ' on port ' + $Port
    Start-Sleep -Seconds 2
}

# 2. back up, then replace (the image is free now)
if (Test-Path $Bin) {
    $bak = $Bin + '.pre-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.bak'
    Copy-Item -LiteralPath $Bin -Destination $bak -Force
    'backup: ' + $bak
}
Copy-Item -LiteralPath $Exe -Destination $Bin -Force
'staged: ' + $Bin

# 3. start + prove
if ($NoRestart) { 'restart skipped (-NoRestart)'; exit 0 }
Start-ScheduledTask -TaskName $Task
Start-Sleep -Seconds 6
try {
    $h = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $Port + '/healthz') -UseBasicParsing -TimeoutSec 20
    'healthz: v' + $h.version + '  phase=' + $h.phase + '  verdict=' + $h.verdict.state
    if ($h.llama) { 'llama: ' + ($h.llama | ConvertTo-Json -Compress) } else { 'llama: null (no version.json)' }
    if ($h.version) { 'DEPLOY OK: meshd v' + $h.version + ' answering on ' + $Port }
} catch {
    throw ('healthz dead after deploy: ' + $_.Exception.Message + ' - restore the .bak and restart task ' + $Task)
}
