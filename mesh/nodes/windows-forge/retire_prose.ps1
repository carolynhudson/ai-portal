# retire_prose.ps1 - retire the dedicated prose lane (llama-server :8081).
# LAW (owner 2026-11-12): the router (:8082) is the ONLY network-exposed
# llama.cpp surface. The prose model (Qwen3-30b-A3B-Instruct) is in the
# router library; the dedicated lane is pure RAM sprawl. Reversible:
# .\retire_prose.ps1 -Restore re-enables task + firewall rule.
param([switch]$Restore)
$ErrorActionPreference = "Stop"
$task = "ai-forge-llama-prose"
$rule = "ai-forge llama prose (portal only)"
if ($Restore) {
    Enable-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    Enable-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    Write-Output "prose lane RESTORED (task + firewall + started)"
    exit 0
}
Disable-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue | Out-Null
Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
Get-NetTCPConnection -State Listen -LocalPort 8081 -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
Disable-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
if (Get-NetTCPConnection -State Listen -LocalPort 8081 -ErrorAction SilentlyContinue) {
    Write-Output "WARNING: 8081 still listening"
} else {
    Write-Output "prose lane RETIRED: task disabled, process stopped, firewall rule disabled"
}
Write-Output "router id Qwen3-30b-A3B-Instruct replaces it (chat model=<id> loads on demand)"
