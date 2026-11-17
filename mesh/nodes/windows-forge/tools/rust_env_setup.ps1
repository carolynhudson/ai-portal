# rust_env_setup.ps1 - build the meshd Rust development workspace and
# pre-fetch the crate stack (per AI_MESH_PROPOSAL.md: user-scope daemon,
# MCP streamable-HTTP, windows-service support). No admin; no service code
# beyond a buildable hello skeleton for toolchain verification.
$ErrorActionPreference = "Continue"
$env:Path = (Join-Path $env:USERPROFILE ".cargo\bin") + ";" + $env:Path
$root = "C:\forge\dev\meshd"

if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root | Out-Null }
Set-Location $root

Write-Output "== workspace members =="
& cargo new crates\meshd --bin 2>&1 | Out-String
& cargo new crates\hello-svc --bin 2>&1 | Out-String

# root workspace manifest (members + shared profile)
"[workspace]" | Set-Content -Path Cargo.toml -Encoding ASCII
"members = [`"crates/meshd`", `"crates/hello-svc`"]" | Add-Content -Path Cargo.toml -Encoding ASCII
"resolver = `"2`"" | Add-Content -Path Cargo.toml -Encoding ASCII
"" | Add-Content -Path Cargo.toml -Encoding ASCII
"[profile.release]" | Add-Content -Path Cargo.toml -Encoding ASCII
"lto = `"thin`"" | Add-Content -Path Cargo.toml -Encoding ASCII
"strip = true" | Add-Content -Path Cargo.toml -Encoding ASCII

Push-Location crates\meshd
Write-Output "== meshd deps (MCP/HTTP/system stack) =="
& cargo add tokio --features full 2>&1 | Out-String
& cargo add axum 2>&1 | Out-String
& cargo add serde --features derive 2>&1 | Out-String
& cargo add serde_json 2>&1 | Out-String
& cargo add thiserror 2>&1 | Out-String
& cargo add anyhow 2>&1 | Out-String
& cargo add clap --features derive 2>&1 | Out-String
& cargo add tracing 2>&1 | Out-String
& cargo add tracing-subscriber --features env-filter 2>&1 | Out-String
& cargo add sysinfo 2>&1 | Out-String
& cargo add nvml-wrapper 2>&1 | Out-String
& cargo add chrono 2>&1 | Out-String
& cargo add tower-http --features trace 2>&1 | Out-String
& cargo add reqwest --no-default-features --features json,rustls-tls 2>&1 | Out-String
& cargo add rmcp 2>&1 | Out-String
Pop-Location

Push-Location crates\hello-svc
Write-Output "== hello-svc deps (SCM service verification skeleton) =="
& cargo add windows-service 2>&1 | Out-String
& cargo add windows --features Win32_Foundation,Win32_System_Services,Win32_System_Threading,Win32_Security,Win32_System_ProcessStatus,Win32_System_Diagnostics_ToolHelp,Win32_NetworkManagement_IpHelper,Win32_Networking_WinSock 2>&1 | Out-String
& cargo add tracing 2>&1 | Out-String
& cargo add anyhow 2>&1 | Out-String
Pop-Location

Write-Output "== fetch all crate sources (offline-ready cache) =="
& cargo fetch 2>&1 | Select-Object -Last 3 | Out-String

Write-Output "== toolchain summary =="
& rustc --version
& cargo --version
& rustup show 2>&1 | Out-String -Width 120
