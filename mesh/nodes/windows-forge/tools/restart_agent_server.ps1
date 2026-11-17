$c = Get-NetTCPConnection -State Listen -LocalPort 24096 -ErrorAction SilentlyContinue
if ($c) {
    $pid24 = ($c | Select-Object -First 1).OwningProcess
    Write-Output ("agent server pid " + $pid24 + " - stopping (task restart follows)")
    Stop-Process -Id $pid24 -Force
    Start-Sleep -Seconds 2
}
Start-ScheduledTask -TaskName "ai-forge-opencode-agent"
Start-Sleep -Seconds 6
$ok = $false
for ($i = 0; $i -lt 10; $i++) {
    if (Get-NetTCPConnection -State Listen -LocalPort 24096 -ErrorAction SilentlyContinue) { $ok = $true; break }
    Start-Sleep -Seconds 3
}
if ($ok) { Write-Output "agent server LISTENING on 24096 (task-managed)" } else { Write-Output "NOT listening after 30 s - check task last result:"; (Get-ScheduledTaskInfo -TaskName "ai-forge-opencode-agent").LastTaskResult }
