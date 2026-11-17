// meshd - the forge node's mesh daemon (phase 2: status + leases + service control).
// Contract: portal repo docs/NODE_CONTRACT.md (six interfaces) +
// AI_MESH_PROPOSAL.md 2.3 (mesh.lease -> granted|queued|denied; mesh.service
// user-scope instant). Personal-PC law: a real app, idle-cheap (no timers, no
// polling - the lease ledger prunes lazily on access), user-scope scheduled
// task, no admin.
//
// Read tools:
//   mesh_status   - health + services (+ drift) + router ids + caps + leases + verdict
//   node_health   - version, pid, uptime, host, RAM/VRAM/disk measured
//   node_services - services.json vs reality (TCP probe + router /v1/models state)
//   node_caps     - forge-mcp forge_caps passthrough + llama ids as cards
// Mutating tools (same bearer gate; act.node is quarantined mesh-side):
//   mesh_lease    - reserve vram/ram for ttl_s -> granted|denied (no queue in phase 2)
//   lease_release - free a lease early by id
//   mesh_service  - start|stop|restart|status registry services; DENIED for
//                   llama-router (portal brain), opencode-agent (agent harness),
//                   meshd (self) - "agents avoid hurting tasks" is node policy.
//
// Auth: bearer token, resolved hub-first: env MESH_TOKEN -> `meshctl get
// subagent-key` -> C:\forge\llama\subagent_key.txt (ONE-token law; the file
// is the fallback). Firewall gates 8092 to the portal IP; localhost always.
use std::net::SocketAddr;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::Result;
use axum::extract::State;
use axum::http::StatusCode;
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clap::{Parser, Subcommand};
use rmcp::handler::server::wrapper::Parameters;
use rmcp::handler::server::ServerHandler;
use rmcp::model::{CallToolResult, ContentBlock};
use rmcp::schemars;
use rmcp::{ErrorData as McpError, tool, tool_handler, tool_router};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

const VERSION: &str = env!("CARGO_PKG_VERSION");
// root()/leases_path(): the node's home. Windows defaults are the forge's
// C:\forge (unchanged behavior); a Linux node points MESHD_ROOT at its own
// tree (e.g. ~/forge) and the paths join natively. LEASES_PATH was a const;
// the ledger writer law (meshd is the single writer) is unchanged.
fn root() -> String {
    std::env::var("MESHD_ROOT").unwrap_or_else(|_| "C:\\forge".to_string())
}
fn leases_path() -> String {
    std::env::var("MESHD_LEASES_FILE").unwrap_or_else(|_| {
        std::path::Path::new(&root())
            .join("etc")
            .join("mesh_leases.json")
            .to_string_lossy()
            .into_owned()
    })
}
fn mcp_base() -> String {
    std::env::var("MESHD_MCP_URL").unwrap_or_else(|_| "http://127.0.0.1:8096".to_string())
}
fn router_url() -> String {
    std::env::var("MESHD_ROUTER_URL").unwrap_or_else(|_| "http://127.0.0.1:8082".to_string())
}
fn node_name() -> String {
    std::env::var("MESHD_NODE_NAME").unwrap_or_else(|_| "rainbowblock".to_string())
}
// busy/safe verdict policy (node declares; contract = declarative policy + live
// signal): the z-image bf16 bring-up trio needs ~10 GiB VRAM, router idle lanes
// want headroom; 3 GiB VRAM / 12 GiB RAM free is "someone is using this box".
const VRAM_FREE_MIN_MIB: u64 = 3072;
const RAM_FREE_MIN_MIB: u64 = 12288;
// stop/start denial list: portal brain, agent harness, self (phase 2 node policy)
const DENY_STOP: [&str; 4] = ["llama-router", "opencode-agent", "meshd", "portal"];

// ---------------- lease ledger ----------------

#[derive(Serialize, Deserialize, Clone)]
struct Lease {
    id: String,
    vram_mib: u64,
    ram_mib: u64,
    ttl_s: u64,
    job: String,
    granted_unix: u64,
    expires_unix: u64,
}

static LEDGER: Mutex<Vec<Lease>> = Mutex::new(Vec::new());

fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

fn load_ledger() {
    if let Ok(s) = std::fs::read_to_string(leases_path()) {
        if let Ok(v) = serde_json::from_str::<Value>(&s) {
            if let Some(arr) = v.get("leases").and_then(|l| l.as_array()) {
                if let Ok(list) = serde_json::from_value::<Vec<Lease>>(serde_json::json!(arr)) {
                    let now = now_unix();
                    let mut g = LEDGER.lock().unwrap();
                    *g = list.into_iter().filter(|l| l.expires_unix > now).collect();
                }
            }
        }
    }
}

fn save_ledger(g: &[Lease]) {
    let v = json!({ "schema": 1, "node": node_name(), "updated_unix": now_unix(),
                    "leases": g });
    if let Ok(s) = serde_json::to_string_pretty(&v) {
        let _ = std::fs::write(leases_path(), s);
    }
}

fn prune(g: &mut Vec<Lease>) {
    let now = now_unix();
    g.retain(|l| l.expires_unix > now);
}

fn leases_json() -> Value {
    let mut g = LEDGER.lock().unwrap();
    prune(&mut g);
    json!({ "count": g.len(),
            "committed_vram_mib": g.iter().map(|l| l.vram_mib).sum::<u64>(),
            "committed_ram_mib": g.iter().map(|l| l.ram_mib).sum::<u64>(),
            "leases": serde_json::to_value(g.as_slice()).unwrap_or(json!([])) })
}

// ---------------- token (ONE-token law, hub-first) ----------------

fn resolve_token(root: &str) -> Option<String> {
    if let Ok(t) = std::env::var("MESH_TOKEN") {
        let t = t.trim().to_string();
        if t.len() >= 16 {
            return Some(t);
        }
    }
    // meshctl: named by env, else the forge layout (root\etc\mesh\meshctl.exe)
    // on Windows / PATH lookup on unix. Backslashes in joins broke Linux.
    let meshctl = std::env::var("MESHD_MESHCTL")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| {
            #[cfg(windows)]
            {
                std::path::Path::new(root).join("etc").join("mesh").join("meshctl.exe")
            }
            #[cfg(not(windows))]
            {
                std::path::PathBuf::from("meshctl")
            }
        });
    if meshctl.exists() || meshctl.file_name() == Some(std::ffi::OsStr::new("meshctl")) {
        let mut cmd = Command::new(&meshctl);
        cmd.args(["get", "subagent-key"]);
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
        }
        if let Ok(out) = cmd.output() {
            let t = String::from_utf8_lossy(&out.stdout).trim().to_string();
            if out.status.success() && t.len() >= 16 {
                return Some(t);
            }
        }
    }
    let file = std::path::Path::new(root).join("llama").join("subagent_key.txt");
    if let Ok(s) = std::fs::read_to_string(file) {
        let t = s.trim().to_string();
        if t.len() >= 16 {
            return Some(t);
        }
    }
    None
}

async fn check_auth(State(token): State<Option<String>>, req: axum::http::Request<axum::body::Body>, next: Next) -> Response {
    match &token {
        None => {
            // no token resolvable: serve nothing sensitive
            (StatusCode::SERVICE_UNAVAILABLE, "meshd: no token resolvable").into_response()
        }
        Some(expected) => {
            let got = req
                .headers()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("")
                .trim()
                .to_string();
            if got == format!("Bearer {}", expected) {
                next.run(req).await
            } else {
                (StatusCode::UNAUTHORIZED, "meshd: bearer token required").into_response()
            }
        }
    }
}

// ---------------- probes ----------------

async fn tcp_open(port: u16) -> bool {
    let addr = format!("127.0.0.1:{}", port);
    tokio::time::timeout(
        Duration::from_millis(400),
        tokio::net::TcpStream::connect(addr),
    )
    .await
    .map(|r| r.is_ok())
    .unwrap_or(false)
}

async fn router_models() -> Value {
    let token = resolve_token(&root());
    let Some(t) = token else {
        return json!({ "ok": false, "error": "no token" });
    };
    let client = reqwest::Client::new();
    match client
        .get(format!("{}/v1/models", router_url()))
        .header("Authorization", format!("Bearer {}", t))
        .timeout(Duration::from_secs(10))
        .send()
        .await
    {
        Ok(r) => {
            let status = r.status().as_u16();
            match r.json::<Value>().await {
                Ok(v) => json!({ "ok": status == 200, "status": status, "models": v.get("data").cloned().unwrap_or(json!([])) }),
                Err(e) => json!({ "ok": false, "error": e.to_string() }),
            }
        }
        Err(e) => json!({ "ok": false, "error": e.to_string() }),
    }
}

async fn forge_caps() -> Value {
    let client = reqwest::Client::new();
    let body = json!({ "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": { "name": "forge_caps", "arguments": {} } });
    match client
        .post(format!("{}/mcp", mcp_base()))
        .header("Accept", "application/json")
        .json(&body)
        .timeout(Duration::from_secs(15))
        .send()
        .await
    {
        Ok(r) => r.json::<Value>().await.unwrap_or_else(|e| json!({ "ok": false, "error": e.to_string() })),
        Err(e) => json!({ "ok": false, "error": e.to_string() }),
    }
}

fn free_vram_mib() -> Option<u64> {
    let nvml = nvml_wrapper::Nvml::init().ok()?;
    let dev = nvml.device_by_index(0).ok()?;
    let mem = dev.memory_info().ok()?;
    Some(mem.free / 1_048_576)
}

fn free_ram_mib() -> Option<u64> {
    let mut sys = sysinfo::System::new().ok()?;
    sys.refresh_memory();
    Some((sys.total_memory() - sys.used_memory()) / 1_048_576)
}

fn health_json() -> Value {
    let ram = sysinfo::System::new().ok().map(|mut sys| {
        sys.refresh_memory();
        // sysinfo reports BYTES; report MiB (nvml branch already divides by 1_048_576)
        json!({ "total_mib": sys.total_memory() / 1_048_576, "used_mib": sys.used_memory() / 1_048_576 })
    });
    let disks = sysinfo::Disks::new_with_refreshed_list().ok();
    let disk = disks.as_ref().and_then(|list| {
        list.iter().find(|d| d.mount_point().to_string_lossy() == "C:\\").or_else(|| list.iter().next())
    });
    let vram = nvml_vram();
    // resource verdict (declarative policy + live signal, NODE_CONTRACT sec
    // "Resource signals"): busy = human/forge work is using the box's headroom.
    let fr = free_ram_mib().unwrap_or(0);
    let fv = vram.as_ref().and_then(|v| v.get("total").and_then(|t| t.as_u64()))
        .zip(vram.as_ref().and_then(|v| v.get("used").and_then(|u| u.as_u64())))
        .map(|(t, u)| t - u)
        .unwrap_or(0);
    let verdict = if fv < VRAM_FREE_MIN_MIB || fr < RAM_FREE_MIN_MIB { "busy" } else { "safe" };
    json!({
        "node": node_name(),
        "daemon": "meshd",
        "version": VERSION,
        "pid": std::process::id(),
        "uptime_s": sysinfo::System::uptime().unwrap_or(0),
        "host": sysinfo::System::host_name().unwrap_or_default(),
        "os": sysinfo::System::os_version().unwrap_or_default(),
        "ram": ram,
        "disk_c": disk.map(|d| json!({
            "total_gib": d.total_space() / 1_073_741_824,
            "free_gib": d.available_space() / 1_073_741_824,
        })),
        "vram_mib": vram,
        "llama": llama_version(),
        "verdict": { "state": verdict, "free_vram_mib": fv, "free_ram_mib": fr,
                     "policy": { "vram_free_min_mib": VRAM_FREE_MIN_MIB, "ram_free_min_mib": RAM_FREE_MIN_MIB } },
        "phase": "2 (status + leases + service control)",
    })
}

fn nvml_vram() -> Option<Value> {
    let nvml = nvml_wrapper::Nvml::init().ok()?;
    let dev = nvml.device_by_index(0).ok()?;
    let mem = dev.memory_info().ok()?;
    Some(json!({ "used": mem.used / 1_048_576, "total": mem.total / 1_048_576 }))
}

// llama_version - the node's active llama.cpp build, measured from the file its
// updater writes (update_llama.ps1 / the portal's equivalent). Cross-node
// version SYNC (owner ask 2026-11-16): the portal compares this field across
// nodes. Absent file = null - no version is claimed that was not measured.
// MESHD_LLAMA_VERSION_FILE points a non-Windows node at its own record.
fn llama_version() -> Value {
    let path = std::env::var("MESHD_LLAMA_VERSION_FILE")
        .unwrap_or_else(|_| "C:\\forge\\llama\\version.json".to_string());
    match std::fs::read_to_string(&path) {
        Ok(s) => serde_json::from_str(&s)
            .unwrap_or(json!({ "ok": false, "error": "version.json unparseable" })),
        Err(_) => Value::Null,
    }
}

fn services_registry() -> Value {
    let path = std::env::var("MESHD_SERVICES_FILE").unwrap_or_else(|_| {
        std::path::Path::new(&root())
            .join("etc")
            .join("services.json")
            .to_string_lossy()
            .into_owned()
    });
    match std::fs::read_to_string(path) {
        Ok(s) => serde_json::from_str(&s).unwrap_or(json!({ "ok": false, "error": "services.json unparseable" })),
        Err(e) => json!({ "ok": false, "error": e.to_string() }),
    }
}

async fn services_with_state() -> Value {
    let reg = services_registry();
    let mut out = Vec::new();
    if let Some(services) = reg.get("services").and_then(|s| s.as_array()) {
        for svc in services {
            let port = svc.get("port").and_then(|p| p.as_u64()).unwrap_or(0) as u16;
            let status = svc.get("status").and_then(|s| s.as_str()).unwrap_or("");
            let running = tcp_open(port).await;
            let mut row = svc.clone();
            row["observed"] = json!({
                "port_open": running,
                "state": if status == "retired" { "retired".to_string() }
                         else if running { "running".to_string() } else { "stopped".to_string() },
                "drift": status != "retired" && svc.get("status").and_then(|s| s.as_str()) == Some("enabled") && !running,
            });
            out.push(row);
        }
    }
    let models = router_models().await;
    json!({ "node": node_name(), "services": out, "llm": models,
            "registry_updated": reg.get("updated"), "laws": reg.get("laws") })
}

fn normalize_cards(raw: &Value, models: &Value, engine_state: &str) -> Vec<Value> {
    // service-card schema v0 (NODE_CONTRACT sec "Service-card schema"): the
    // advertisement unit the control plane aggregates for mesh.place.
    let mut cards = Vec::new();
    let text = raw.pointer("/result/content/0/text").and_then(|t| t.as_str()).unwrap_or("");
    if let Ok(v) = serde_json::from_str::<Value>(text) {
        if let Some(arr) = v.get("contracts").and_then(|c| c.as_array()) {
            for c in arr {
                let name = c.get("name").and_then(|n| n.as_str()).unwrap_or("?").to_string();
                // honest per-contract VRAM floors, measured on this 2080 Ti
                let (vram, backend) = match name.as_str() {
                    "chroma_key" => (1, "comfyui-cpu"),
                    "birefnet_cutout" => (4, "cuda"),
                    "svg_writer_llm" => (6, "cuda"),
                    "z_image_turbo_fast" => (8, "cuda"),
                    _ => (10, "cuda"),
                };
                cards.push(json!({
                    "node": node_name(), "kind": "image-forge", "service": name,
                    "tier": c.get("tier").cloned().unwrap_or(json!("")),
                    "fills": c.get("fills").cloned().unwrap_or(json!([])),
                    "requires": { "vram_gb_min": vram, "backend": backend },
                    "limits": { "w32": [256, 1536], "concurrency": 1, "timeout_s": [30, 1800] },
                    "idle_policy": { "free_after_s": 1200, "lease_yield": true },
                    "state": engine_state,
                    "measured": { "note": c.get("validated").cloned().unwrap_or(json!(null)) },
                    // pack is NULLABLE (owner decision 2026-11-16, bench #44): report the
                    // real pack path@version only when this node has materialized one.
                    // mesh/contracts/ is plan-only, so a card naming image-forge@v0 would
                    // be a routing lie - mesh.place consumes this field. A pack earns a
                    // version when its verifiers print measured values.
                    "pack": Value::Null
                }));
            }
        }
    }
    if let Some(arr) = models.get("models").and_then(|m| m.as_array()) {
        for m in arr {
            cards.push(json!({
                "node": node_name(), "kind": "llm-router",
                "service": m.get("id").cloned().unwrap_or(json!("?")),
                "tier": "router-preset",
                "fills": ["prompt"],
                "requires": { "ram_gb_min": 8, "backend": "llama-cuda-cpu" },
                "limits": { "context": m.get("context_length").cloned().unwrap_or(json!(0)) },
                "idle_policy": { "sleep_idle_s": 900, "models_max": 1, "memory_margin_mib": 16384 },
                "state": m.get("state").cloned().unwrap_or(json!("catalog")),
                "measured": { "note": "lanes measured 2026-11-16 on llama 9581154 (sm_75): CPU flint 18.14 t/s tg128; GPU qwen3-4b 135.3 t/s tg128; -t 16, -pg 512,128, r=2" },
                "pack": Value::Null   // nullable pack law (bench #44): no pack on disk = null
            }));
        }
    }
    cards
}

async fn caps() -> Value {
    let raw = forge_caps().await;
    let models = router_models().await;
    // engine state honest: rendering if forge queue is running, else idle
    let engine_state = match reqwest::Client::new()
        .get(format!("{}/health", mcp_base()))
        .timeout(Duration::from_secs(5)).send().await
    {
        Ok(r) => match r.json::<Value>().await {
            Ok(v) => v.pointer("/health/queue/running").and_then(|x| x.as_u64())
                .map(|n| if n > 0 { "rendering".to_string() } else { "idle".to_string() })
                .unwrap_or_else(|| "unknown".to_string()),
            Err(_) => "unknown".to_string(),
        },
        Err(_) => "unknown".to_string(),
    };
    let cards = normalize_cards(&raw, &models, &engine_state);
    json!({
        "node": node_name(),
        "cards": cards,
        "image_contracts": raw,
        "note": "cards = service-card schema v0 (NODE_CONTRACT) for control-plane aggregation; image_contracts = raw forge_caps passthrough",
    })
}

// ---------------- service control (user-scope, no admin) ----------------

fn listener_pid(port: u16) -> Option<u32> {
    // netstat -ano: " TCP  0.0.0.0:8188  0.0.0.0:0  LISTENING  12345" (en-US
    // box; local field ends with :PORT, pid last). Unix: ss -ltnp (pid column
    // shows for this user's processes; services.json controls are user-scope).
    #[cfg(windows)]
    let mut cmd = {
        let mut c = Command::new("netstat");
        c.args(["-ano"]);
        c
    };
    #[cfg(not(windows))]
    let mut cmd = {
        let mut c = Command::new("ss");
        c.args(["-ltnp"]);
        c
    };
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }
    let out = cmd.output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout).to_string();
    let needle = format!(":{}", port);
    for line in text.lines() {
        let f: Vec<&str> = line.split_whitespace().collect();
        #[cfg(windows)]
        let hit = f.len() >= 5 && f[3].eq_ignore_ascii_case("LISTENING")
            && (f[1].ends_with(&needle));
        #[cfg(not(windows))]
        let hit = f.len() >= 5 && f[0] == "LISTEN" && f[4].ends_with(&needle);
        if hit {
            #[cfg(windows)]
            let pid_opt = f[4].parse::<u32>().ok();
            #[cfg(not(windows))]
            let pid_opt = line
                .split("pid=")
                .nth(1)
                .and_then(|s| s.split(',').next())
                .and_then(|s| s.trim_end_matches(')').parse::<u32>().ok());
            if let Some(pid) = pid_opt {
                if pid > 4 {
                    return Some(pid);
                }
            }
        }
    }
    None
}

fn run_hidden(prog: &str, args: &[&str]) -> bool {
    let mut cmd = Command::new(prog);
    cmd.args(args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }
    cmd.output().map(|o| o.status.success()).unwrap_or(false)
}

fn spawn_hidden(prog: &str, args: &[&str]) -> bool {
    let mut cmd = Command::new(prog);
    cmd.args(args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000);
    }
    cmd.spawn().is_ok()
}

async fn stop_service(id: &str, port: u16) -> Value {
    let Some(pid) = listener_pid(port) else {
        return json!({ "ok": true, "id": id, "action": "stop", "state": "already-stopped" });
    };
    let pid_s = pid.to_string();
    #[cfg(windows)]
    run_hidden("taskkill", &["/PID", &pid_s, "/T", "/F"]);
    #[cfg(not(windows))]
    run_hidden("kill", &["-KILL", &pid_s]);
    // verify the port closes (10 x 500ms)
    for _ in 0..20 {
        if !tcp_open(port).await {
            return json!({ "ok": true, "id": id, "action": "stop", "state": "stopped", "pid": pid });
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    json!({ "ok": false, "id": id, "action": "stop", "state": "stuck",
            "reason": format!("port {} still open after taskkill pid {}", port, pid) })
}

async fn start_service(id: &str, port: u16, launcher: &str) -> Value {
    if launcher.is_empty() || !std::path::Path::new(launcher).exists() {
        return json!({ "ok": false, "id": id, "action": "start", "state": "refused",
                       "reason": format!("launcher missing: {}", launcher) });
    }
    // launchers are PS glue with their own port guard; machine policy Restricted
    // requires the Bypass (2026-11-07 law). Linux launchers are .sh scripts
    // (kit law: their own guards, bash explicit, never via sh).
    #[cfg(windows)]
    spawn_hidden("powershell.exe",
        &["-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", launcher]);
    #[cfg(not(windows))]
    spawn_hidden("bash", &[launcher]);
    // comfyui boots slow: poll 120 x 500ms = 60s
    for _ in 0..120 {
        if tcp_open(port).await {
            return json!({ "ok": true, "id": id, "action": "start", "state": "running", "port": port });
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    json!({ "ok": false, "id": id, "action": "start", "state": "timeout",
            "reason": format!("port {} not open 60s after launcher start", port) })
}

// ---------------- MCP tool args ----------------

#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
struct LeaseArgs {
    vram_mib: Option<u64>,
    ram_mib: Option<u64>,
    ttl_s: u64,
    job: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
struct ReleaseArgs {
    id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
struct ServiceArgs {
    id: String,
    action: String, // status | start | stop | restart
}

// ---------------- MCP server ----------------

#[derive(Clone)]
struct Meshd;

#[tool_router]
impl Meshd {
    #[tool(description = "Node aggregation: health (incl. busy/safe verdict), services vs registry (drift flags), router model ids, capability passthrough, active leases.")]
    async fn mesh_status(&self) -> Result<CallToolResult, McpError> {
        let v = json!({
            "health": health_json(),
            "services": services_with_state().await,
            "caps": caps().await,
            "leases": leases_json(),
        });
        Ok(CallToolResult::success(vec![ContentBlock::json(v)?]))
    }

    #[tool(description = "Node identity + measured resources: version, pid, uptime, host, RAM/VRAM/disk, busy/safe verdict.")]
    async fn node_health(&self) -> Result<CallToolResult, McpError> {
        Ok(CallToolResult::success(vec![ContentBlock::json(health_json())?]))
    }

    #[tool(description = "Service registry rows enriched with observed state (TCP probe) + drift flags, plus llama router ids with loaded/unloaded state.")]
    async fn node_services(&self) -> Result<CallToolResult, McpError> {
        Ok(CallToolResult::success(vec![ContentBlock::json(services_with_state().await)?]))
    }

    #[tool(description = "Capability manifest as service-card schema v0 (NODE_CONTRACT): one card per image contract + per LLM id (requires/limits/idle_policy/state/measured) + raw forge_caps passthrough. This is the unit mesh.place aggregates.")]
    async fn node_caps(&self) -> Result<CallToolResult, McpError> {
        Ok(CallToolResult::success(vec![ContentBlock::json(caps().await)?]))
    }

    #[tool(description = "Reserve node resources (vram_mib/ram_mib for ttl_s, job required). granted: committed against live free minus active leases; denied: with reason + measured headroom. No queue yet (phase 2). Leases TTL-expire; ledger at C:\\forge\\etc\\mesh_leases.json.")]
    async fn mesh_lease(&self, Parameters(a): Parameters<LeaseArgs>) -> Result<CallToolResult, McpError> {
        let vram = a.vram_mib.unwrap_or(0);
        let ram = a.ram_mib.unwrap_or(0);
        if vram == 0 && ram == 0 {
            return Ok(CallToolResult::success(vec![ContentBlock::json(
                json!({ "decision": "denied", "reason": "lease must ask for vram_mib and/or ram_mib > 0" }))?]));
        }
        let ttl = a.ttl_s.clamp(30, 86400);
        let now = now_unix();
        let mut g = LEDGER.lock().unwrap();
        prune(&mut g);
        let committed_vram: u64 = g.iter().map(|l| l.vram_mib).sum();
        let committed_ram: u64 = g.iter().map(|l| l.ram_mib).sum();
        let free_vram = free_vram_mib();
        let free_ram = free_ram_mib();
        let avail_vram = free_vram.map(|f| f.saturating_sub(committed_vram));
        let avail_ram = free_ram.map(|f| f.saturating_sub(committed_ram));
        if vram > 0 {
            match avail_vram {
                None => return Ok(CallToolResult::success(vec![ContentBlock::json(json!({
                    "decision": "denied", "reason": "nvml unavailable - cannot vouch for VRAM" }))?])),
                Some(av) if vram > av => return Ok(CallToolResult::success(vec![ContentBlock::json(json!({
                    "decision": "denied",
                    "reason": format!("vram requested {} MiB > available {} MiB (free {} - committed {})",
                                      vram, av, free_vram.unwrap_or(0), committed_vram),
                    "free_vram_mib": free_vram, "committed_vram_mib": committed_vram }))?])),
                _ => {}
            }
        }
        if ram > 0 {
            match avail_ram {
                None => return Ok(CallToolResult::success(vec![ContentBlock::json(json!({
                    "decision": "denied", "reason": "ram measurement unavailable" }))?])),
                Some(av) if ram > av => return Ok(CallToolResult::success(vec![ContentBlock::json(json!({
                    "decision": "denied",
                    "reason": format!("ram requested {} MiB > available {} MiB (free {} - committed {})",
                                      ram, av, free_ram.unwrap_or(0), committed_ram),
                    "free_ram_mib": free_ram, "committed_ram_mib": committed_ram }))?])),
                _ => {}
            }
        }
        let sub = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
        let id = format!("L{:x}{:04x}", now, sub & 0xffff);
        let lease = Lease { id: id.clone(), vram_mib: vram, ram_mib: ram, ttl_s: ttl,
                            job: a.job.clone(), granted_unix: now, expires_unix: now + ttl };
        g.push(lease.clone());
        save_ledger(&g);
        drop(g);
        Ok(CallToolResult::success(vec![ContentBlock::json(json!({
            "decision": "granted", "lease": lease,
            "free_vram_mib": free_vram, "free_ram_mib": free_ram }))?]))
    }

    #[tool(description = "Release a lease early by id. Returns released|not_found. Expired leases vanish on any access (no timers).")]
    async fn lease_release(&self, Parameters(a): Parameters<ReleaseArgs>) -> Result<CallToolResult, McpError> {
        let mut g = LEDGER.lock().unwrap();
        prune(&mut g);
        let before = g.len();
        g.retain(|l| l.id != a.id);
        let released = g.len() < before;
        save_ledger(&g);
        Ok(CallToolResult::success(vec![ContentBlock::json(json!({
            "decision": if released { "released" } else { "not_found" }, "id": a.id }))?]))
    }

    #[tool(description = "User-scope service control from the registry: action = status|start|stop|restart, id = registry service id. REFUSED for llama-router (portal brain), opencode-agent (agent harness), meshd (self) and retired rows. start uses the row launcher; stop kills the listener pid tree (taskkill, user-scope).")]
    async fn mesh_service(&self, Parameters(a): Parameters<ServiceArgs>) -> Result<CallToolResult, McpError> {
        let reg = services_registry();
        let row = reg.get("services").and_then(|s| s.as_array()).and_then(|arr| {
            arr.iter().find(|s| s.get("id").and_then(|i| i.as_str()) == Some(a.id.as_str()))
        });
        let Some(row) = row.cloned() else {
            return Ok(CallToolResult::success(vec![ContentBlock::json(
                json!({ "ok": false, "state": "refused", "reason": format!("unknown service id: {}", a.id) }))?]));
        };
        let port = row.get("port").and_then(|p| p.as_u64()).unwrap_or(0) as u16;
        let launcher = row.get("launcher").and_then(|l| l.as_str()).unwrap_or("").to_string();
        let status = row.get("status").and_then(|s| s.as_str()).unwrap_or("").to_string();
        let action = a.action.to_lowercase();
        let v = if status == "retired" {
            json!({ "ok": false, "state": "refused", "reason": format!("service {} is retired", a.id) })
        } else if DENY_STOP.contains(&a.id.as_str()) && (action == "stop" || action == "restart") {
            json!({ "ok": false, "state": "refused", "reason": format!(
                "{} is on the node deny-list (portal brain / agent harness / self) - agents avoid hurting tasks", a.id) })
        } else {
            match action.as_str() {
                "status" => json!({ "ok": true, "id": a.id, "state": if tcp_open(port).await { "running" } else { "stopped" }, "port": port }),
                "stop" => stop_service(&a.id, port).await,
                "start" => start_service(&a.id, port, &launcher).await,
                "restart" => {
                    let s = stop_service(&a.id, port).await;
                    let mut r = start_service(&a.id, port, &launcher).await;
                    r["stop"] = s;
                    r
                }
                other => json!({ "ok": false, "state": "refused", "reason": format!("unknown action: {}", other) }),
            }
        };
        Ok(CallToolResult::success(vec![ContentBlock::json(v)?]))
    }
}

#[tool_handler(name = "meshd", version = "0.3.3",
               instructions = "Forge node controller (meshd phase 2): mesh_status aggregates health+services+caps+leases; mesh_lease/lease_release reserve vram/ram with TTLs; mesh_service starts/stops registry services (deny-list: llama-router, opencode-agent, meshd). All tools bearer-gated (ONE-token, hub-first).")]
impl ServerHandler for Meshd {}

// ---------------- main ----------------

#[derive(Parser)]
#[command(name = "meshd", about = "forge node mesh daemon (phase 2: status + leases + service control)")]
struct Args {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Serve the MCP surface (streamable-HTTP /mcp) + GET /healthz
    StatusServer {
        #[arg(long, default_value = "0.0.0.0")]
        bind: String,
        #[arg(long, default_value_t = 8092)]
        port: u16,
    },
    /// One-shot status to stdout (debug; no server)
    Status,
}

type McpSvc = rmcp::transport::streamable_http_server::StreamableHttpService<
    Meshd,
    rmcp::transport::streamable_http_server::session::local::LocalSessionManager,
>;

async fn mcp_handler(
    svc: State<Arc<McpSvc>>,
    req: axum::extract::Request,
) -> Response {
    svc.handle(req).await.into_response()
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    match args.cmd {
        Cmd::Status => {
            let v = json!({
                "health": health_json(),
                "services": services_with_state().await,
                "caps": caps().await,
                "leases": leases_json(),
            });
            println!("{}", serde_json::to_string_pretty(&v)?);
            Ok(())
        }
        Cmd::StatusServer { bind, port } => {
            load_ledger();
            let token = resolve_token(&root());
            eprintln!(
                "meshd {}: serving MCP at /mcp, port {}, token {}",
                VERSION,
                port,
                if token.is_some() { "resolved" } else { "MISSING - 503 mode" }
            );
            let session_manager = Arc::new(
                rmcp::transport::streamable_http_server::session::local::LocalSessionManager::default(),
            );
            let mut config = rmcp::transport::streamable_http_server::StreamableHttpServerConfig::default();
            // EMPTY list = allow all hosts (host_is_allowed, tower.rs:1101); "*" is NOT a
            // wildcard. LAN node; the firewall gates 8092 to the portal IP.
            config.allowed_hosts.clear();
            let service = Arc::new(
                rmcp::transport::streamable_http_server::StreamableHttpService::new(
                    || Ok(Meshd),
                    session_manager,
                    config,
                ),
            );
            let api = Router::new()
                .route("/mcp", post(mcp_handler).get(mcp_handler).delete(mcp_handler).with_state(service))
                .route_layer(middleware::from_fn_with_state(token.clone(), check_auth));
            let app = Router::new().merge(api).route("/healthz", get(|| async { Json(health_json()) }));
            let addr: SocketAddr = format!("{}:{}", bind, port).parse()?;
            let listener = tokio::net::TcpListener::bind(addr).await?;
            eprintln!("meshd listening on {}", addr);
            axum::serve(listener, app).await?;
            Ok(())
        }
    }
}
