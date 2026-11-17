# install_mingw.ps1 - verify + extract the winlibs MinGW-w64 portable toolchain
# (fixes rustup's self-contained dlltool stub). User scope, no admin. PS 5.1.
$ErrorActionPreference = "Stop"
$zip = "C:\Users\morgi\AppData\Local\Temp\opencode\winlibs-mingw.zip"
$sha = $zip + ".sha256"
$h = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
$w = ((Get-Content $sha -Raw) -split " ")[0].ToLower()
Write-Output ("computed: " + $h)
Write-Output ("published: " + $w)
if ($h -ne $w) { throw "HASH MISMATCH - not extracting" }
Write-Output "HASH OK"
$dest = "C:\forge\dev"
if (Test-Path (Join-Path $dest "mingw64")) {
    Write-Output "mingw64 already extracted at C:\forge\dev\mingw64"
} else {
    Write-Output "extracting (255 MB zip, ~1 min)..."
    & tar.exe -xf $zip -C $dest
    if ($LASTEXITCODE -ne 0) { throw ("tar extract failed, exit " + $LASTEXITCODE) }
}
Get-ChildItem (Join-Path $dest "mingw64\bin") -Filter "dlltool.exe" | Select-Object FullName,Length | Format-Table -AutoSize | Out-String -Width 200
& (Join-Path $dest "mingw64\bin\gcc.exe") --version | Select-Object -First 1
& (Join-Path $dest "mingw64\bin\dlltool.exe") --version | Select-Object -First 1
