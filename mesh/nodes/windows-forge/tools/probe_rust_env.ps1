# probe_rust_env.ps1 - survey the Rust/Windows-service dev environment on this
# box: toolchain, MSVC/SDK, winget. Pure ASCII, PS 5.1.
Write-Output "--- rust toolchain ---"
foreach ($c in @("rustc", "cargo", "rustup")) {
    $p = Get-Command $c -ErrorAction SilentlyContinue
    if ($p) { Write-Output ($c + ": " + $p.Source) } else { Write-Output ($c + ": NOT FOUND") }
}
if (Test-Path "$env:USERPROFILE\.cargo\bin") {
    Write-Output "cargo bin dir exists:"
    Get-ChildItem "$env:USERPROFILE\.cargo\bin" | Select-Object -ExpandProperty Name
}
Write-Output "--- rustup targets (if installed) ---"
$ru = Get-Command rustup -ErrorAction SilentlyContinue
if ($ru) { rustup show 2>&1 | Out-String -Width 120 }
Write-Output "--- Visual Studio / Build Tools ---"
foreach ($d in @("C:\Program Files (x86)\Microsoft Visual Studio", "C:\Program Files\Microsoft Visual Studio")) {
    if (Test-Path $d) { Get-ChildItem $d | Select-Object -ExpandProperty Name }
}
Write-Output "--- Windows Kits (SDK) ---"
if (Test-Path "C:\Program Files (x86)\Windows Kits\10") {
    Get-ChildItem "C:\Program Files (x86)\Windows Kits\10" | Select-Object -ExpandProperty Name
    Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\Include" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name
}
Write-Output "--- winget ---"
winget --version 2>&1 | Select-Object -First 1
Write-Output "--- git / cl.exe on PATH ---"
$g = Get-Command git -ErrorAction SilentlyContinue
if ($g) { Write-Output ("git: " + $g.Source) } else { Write-Output "git: NOT FOUND" }
$cl = Get-Command cl.exe -ErrorAction SilentlyContinue
if ($cl) { Write-Output ("cl.exe: " + $cl.Source) } else { Write-Output "cl.exe: NOT FOUND (normal outside a VS dev shell)" }
Write-Output "--- disk ---"
$free = (Get-PSDrive C).Free / 1GB
Write-Output ("free GB: " + [math]::Round($free, 1))
