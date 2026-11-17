# smoke_rust.ps1 - run the built release binaries (console smoke).
$svc = "C:\forge\dev\meshd\target\release\hello-svc.exe"
$md = "C:\forge\dev\meshd\target\release\meshd.exe"
Write-Output "--- hello-svc console run (expect clean SCM message, exit 0) ---"
& $svc
Write-Output ("exit: " + $LASTEXITCODE)
Write-Output "--- meshd (template main) ---"
& $md
Write-Output ("exit: " + $LASTEXITCODE)
