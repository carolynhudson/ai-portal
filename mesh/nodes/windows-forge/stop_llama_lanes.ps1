# stop_llama_lanes.ps1 - stop flint (8082) + prose (8081) llama endpoints to
# free RAM for a big-model gauntlet. No admin needed (own processes/user tasks).
# Restore with: Start-ScheduledTask for each task name (or run the start_*.ps1).
$lanes = @(
    @{ Task = "ai-forge-llama-subagent"; Port = 8082 },
    @{ Task = "ai-forge-llama-prose";    Port = 8081 }
)
foreach ($lane in $lanes) {
    Stop-ScheduledTask -TaskName $lane.Task -ErrorAction SilentlyContinue
    $conns = Get-NetTCPConnection -LocalPort $lane.Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }
    Write-Output ("stopped " + $lane.Task + " (port " + $lane.Port + ")")
}
Start-Sleep -Seconds 3
$ram = (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB
Write-Output ("free RAM GB: " + [math]::Round($ram, 1))
