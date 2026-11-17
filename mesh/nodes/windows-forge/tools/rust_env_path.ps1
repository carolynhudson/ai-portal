# rust_env_path.ps1 - make the Rust dev environment permanent for this user
# (user scope, no admin). Order matters: C:\forge\dev\mingw64\bin must come
# BEFORE rustup's self-contained dir, so the real dlltool/as win over the
# stubs. PS 5.1, pure ASCII.
$mingw = "C:\forge\dev\mingw64\bin"
$gnu = Join-Path $env:USERPROFILE ".rustup\toolchains\stable-x86_64-pc-windows-gnu\lib\rustlib\x86_64-pc-windows-gnu\bin\self-contained"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
# drop both managed entries, then re-add in the right order
$kept = @()
foreach ($item in ($userPath -split ";")) {
    $t = $item.Trim()
    if ($t -eq "") { continue }
    if ($t -eq $mingw) { continue }
    if ($t -eq $gnu) { continue }
    $kept += $t
}
$kept += $mingw
$kept += $gnu
$newPath = ($kept -join ";")
[Environment]::SetEnvironmentVariable("Path", $newPath, "User")
Write-Output "user PATH now ends with:"
Write-Output ("  " + $mingw)
Write-Output ("  " + $gnu)
Write-Output "user PATH updated (new shells pick it up)"
