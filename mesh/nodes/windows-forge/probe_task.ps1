# probe_task.ps1 - reproduce the scheduled task's command line and report
# its exit code, so the ai-forge-comfyui exit-1 quirk can be pinned down.
$cmd = 'powershell.exe -NoProfile -WindowStyle Hidden -File C:\forge\kit\start_comfyui.ps1 -Root "C:\forge"'
Write-Output ("task action: " + $cmd)
$p = Start-Process -FilePath "powershell.exe" `
    -ArgumentList '-NoProfile', '-WindowStyle', 'Hidden', '-File', 'C:\forge\kit\start_comfyui.ps1', '-Root', 'C:\forge' `
    -WorkingDirectory 'C:\Windows\System32' -WindowStyle Hidden -PassThru -Wait
Write-Output ("exitcode=" + $p.ExitCode)
Write-Output ("port 8188 listening: " + [bool](Get-NetTCPConnection -State Listen -LocalPort 8188 -ErrorAction SilentlyContinue))
