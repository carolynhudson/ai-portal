# install_buildtools.ps1 - elevated VS 2022 Build Tools install from the cached
# bootstrapper (winget's own UAC got cancelled twice, 1602). Self-elevates via
# RunAs: the UAC prompt will sit until answered. Writes a result file.
# PS 5.1, pure ASCII.
$ErrorActionPreference = "Stop"
$boot = "C:\forge\dev\vs_BuildTools.exe"
$cached = "C:\Users\morgi\AppData\Local\Temp\WinGet\Microsoft.VisualStudio.2022.BuildTools.17.14.45\vs_BuildTools.exe"
if (-not (Test-Path $boot)) { Copy-Item $cached $boot }
$marker = "C:\forge\dev\buildtools_install_result.txt"
if (Test-Path $marker) { Remove-Item $marker }
Write-Output "requesting elevation for the Build Tools installer (click Yes on the UAC prompt)..."
$p = Start-Process -FilePath $boot -Verb RunAs -PassThru `
    -ArgumentList "--quiet","--wait","--norestart","--add","Microsoft.VisualStudio.Workload.VCTools;includeRecommended"
$p.WaitForExit()
$code = $p.ExitCode
"exit code: $code  $(Get-Date)" | Set-Content -Path $marker -Encoding ASCII
Write-Output ("installer exit code: " + $code + "  (0 = ok, 3010 = ok-reboot, 1602 = cancelled)")
