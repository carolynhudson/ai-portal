# state_check.ps1 - one-shot survey: ports, llama dir, tool versions. Pure ASCII.
Write-Output "--- ports ---"
foreach ($p in 8188, 8081, 8096, 24096) {
    $l = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    if ($l) {
        $pids = ($l | Select-Object -ExpandProperty OwningProcess -Unique) -join ","
        Write-Output "$p LISTEN pid $pids"
    } else {
        Write-Output "$p down"
    }
}
Write-Output "--- llama dir ---"
Get-ChildItem C:\forge\llama -ErrorAction SilentlyContinue |
    Select-Object Name, Length | Format-Table -AutoSize | Out-String -Width 120
Write-Output "--- gguf files on C:\forge ---"
Get-ChildItem C:\forge -Recurse -Filter *.gguf -ErrorAction SilentlyContinue -Depth 3 |
    Select-Object FullName, @{n='GB';e={[math]::Round($_.Length/1GB, 2)}} |
    Format-Table -AutoSize | Out-String -Width 160
Write-Output "--- versions ---"
& opencode --version 2>&1 | Select-Object -First 1
& node --version 2>&1
& npm --version 2>&1
& hf --version 2>&1 | Select-Object -First 1
if (Test-Path C:\forge\llama\llama-server.exe) {
    & C:\forge\llama\llama-server.exe --version 2>&1 | Select-Object -First 2
}
Write-Output "--- global opencode config ---"
$g = Join-Path $env:USERPROFILE ".config\opencode\opencode.json"
if (Test-Path $g) { Get-Content $g -Raw } else { Write-Output "none at $g" }
