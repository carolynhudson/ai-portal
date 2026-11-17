# restart_forge_mcp.ps1 - restart the image-forge MCP server (port 8096).
# Kills the process listening on the port, then starts the launcher.
# No admin rights needed (own processes, Limited task).
param([int]$Port = 8096)
$procs = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
         Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $procs) {
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    Write-Host "killed pid $procId"
}
Start-Sleep -Seconds 1
Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList `
    "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\start_forge_mcp.ps1`""
Start-Sleep -Seconds 3
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Write-Host "forge-mcp listening pid(s): $(($listen | Select-Object -ExpandProperty OwningProcess -Unique) -join ',')"
} else {
    Write-Host "NOT LISTENING - check C:\forge\logs\forge_mcp.log"
    exit 1
}
