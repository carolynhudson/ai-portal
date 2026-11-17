# probe_stats.ps1 - bounded wait for the forge API to answer (bring-up helper)
param(
    [int]$Port = 8188,
    [int]$Tries = 24,
    [int]$SleepSec = 5
)
for ($i = 0; $i -lt $Tries; $i = $i + 1) {
    try {
        $r = Invoke-RestMethod ("http://127.0.0.1:" + $Port + "/system_stats") -TimeoutSec 5
        Write-Host "API_UP"
        $r | ConvertTo-Json -Depth 6 | Write-Host
        exit 0
    } catch {
        Start-Sleep -Seconds $SleepSec
    }
}
Write-Host "API_DOWN after $Tries tries"
exit 1
