$ok = $false
for ($i = 0; $i -lt 36; $i++) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:8081/health" -TimeoutSec 5
        $ok = $true
        break
    } catch { Start-Sleep -Seconds 5 }
}
if ($ok) {
    Write-Output ("HEALTH: " + ($h | ConvertTo-Json -Compress))
} else {
    Write-Output "health not answering after 180 s - err log tail:"
    $f = Get-ChildItem C:\forge\logs\llama-*.err.log | Sort-Object LastWriteTime | Select-Object -Last 1
    Get-Content $f.FullName -Tail 15
}
