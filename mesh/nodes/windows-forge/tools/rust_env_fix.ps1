# rust_env_fix.ps1 - feature corrections: reqwest 0.13 renamed rustls-tls ->
# rustls; rmcp streamable-HTTP server/client features are OFF by default and
# the meshd proposal (AI_MESH_PROPOSAL sec 2.3) requires MCP streamable-HTTP.
$ErrorActionPreference = "Continue"
$env:Path = (Join-Path $env:USERPROFILE ".cargo\bin") + ";" + $env:Path
Set-Location C:\forge\dev\meshd\crates\meshd

Write-Output "== reqwest: rustls TLS + blocking =="
& cargo add reqwest --features rustls,blocking 2>&1 | Out-String

Write-Output "== rmcp: streamable-HTTP server + client + auth =="
& cargo add rmcp --features transport-streamable-http-server,server-side-http,transport-worker,tower,transport-streamable-http-server-session,client,transport-streamable-http-client,transport-streamable-http-client-reqwest,reqwest,auth 2>&1 | Out-String

Write-Output "== fetch new tree =="
& cargo fetch 2>&1 | Select-Object -Last 3 | Out-String
Write-Output "done"
