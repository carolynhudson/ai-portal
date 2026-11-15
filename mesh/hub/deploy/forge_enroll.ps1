# forge_enroll.ps1 -- enroll RainbowBlock into the HALO mesh hub
# ASCII only, PowerShell 5.1. Bundle layout (C:\forge\etc\mesh\):
#   cert.crt  cert.key  root_ca.crt  meshctl.exe
$ErrorActionPreference = "Stop"
$dir = "C:\forge\etc\mesh"
foreach ($f in @("meshctl.exe", "cert.crt", "cert.key", "root_ca.crt")) {
    if (-not (Test-Path (Join-Path $dir $f))) {
        Write-Output ("MISSING: " + $f + " -- put the enrollment bundle in " + $dir)
        exit 1
    }
}
$meshctl = Join-Path $dir "meshctl.exe"
& $meshctl enroll --name forge `
    --cert-file (Join-Path $dir "cert.crt") `
    --key-file (Join-Path $dir "cert.key") `
    --root-ca (Join-Path $dir "root_ca.crt") `
    --roles forge `
    --hub pihole.home.lan:9443,pihole2.home.lan:9443
if ($LASTEXITCODE -ne 0) { Write-Output "enroll failed"; exit 1 }
# purge the cert/key from the bundle dir: meshctl copied them to
# %USERPROFILE%\.mesh (0600-equivalent ACLs on the node).
Remove-Item (Join-Path $dir "cert.crt") -Force
Remove-Item (Join-Path $dir "cert.key") -Force
& $meshctl nodes
Write-Output "forge enrolled; identity-forge.key is the stable grant identity"
