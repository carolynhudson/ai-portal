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

// ---------------- model capacity + timing (owner 2026-11-17) ----------------
// A router card should carry what memory management needs: weights size, KV
// geometry IN THE PORTAL'S PRICER SHAPE (modelindex.kv_bytes_at consumes
// measured.gguf verbatim), a RAM floor, and cold/warm first-token
// expectations with a generous timeout buffer. All of it is READ-side at
// caps() time: stat + the metadata head (<=16 MiB, parsed once and cached by
// size/mtime) per model - the KV section is at the file head, no model load,
// no boot burden - the startup-cost law. Measured numbers come from bench.json, written by the
// kit's ON-DEMAND bench script (installs/on-demand runs load models - that
// is router work: never at daemon start, one model at a time by mesh law).
// Buffer law: timeout = estimate x 2.5 + floor (warm 15 s, cold 60 s).

fn bench_file() -> String {
    std::env::var("MESHD_BENCH_FILE").unwrap_or_else(|_| {
        std::path::Path::new(&root()).join("llama").join("bench.json").to_string_lossy().into_owned()
    })
}
fn seen_file() -> String {
    std::env::var("MESHD_SEEN_FILE").unwrap_or_else(|_| {
        std::path::Path::new(&root()).join("llama").join("models_seen.json").to_string_lossy().into_owned()
    })
}

fn read_json_file(path: &str) -> Value {
    std::fs::read_to_string(path).ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(json!({}))
}

#[derive(Clone, Debug, PartialEq)]
enum Kv { S(String), N(f64), Arr(Vec<Kv>) }

fn rd_u64(b: &[u8], p: &mut usize, be: bool) -> Option<u64> {
    let a: [u8; 8] = b.get(*p..*p + 8)?.try_into().ok()?;
    *p += 8;
    Some(if be { u64::from_be_bytes(a) } else { u64::from_le_bytes(a) })
}

fn rd_u32(b: &[u8], p: &mut usize, be: bool) -> Option<u32> {
    let a: [u8; 4] = b.get(*p..*p + 4)?.try_into().ok()?;
    *p += 4;
    Some(if be { u32::from_be_bytes(a) } else { u32::from_le_bytes(a) })
}

fn rd_str(b: &[u8], p: &mut usize, be: bool) -> Option<String> {
    let len = rd_u64(b, p, be)? as usize;
    if len > 1 << 20 || *p + len > b.len() {
        return None;
    }
    let s = String::from_utf8_lossy(b.get(*p..*p + len)?).into_owned();
    *p += len;
    Some(s)
}

fn rd_val(b: &[u8], p: &mut usize, t: u8, be: bool) -> Option<Kv> {
    // THE type enum, mirrored from app/core/modelmeta.py _SCALAR_FMT (proven
    // on every GGUF on this box): STRING=8, ARRAY=9, scalars 0..7 + 10..12.
    // (llama.cpp gguf-py's higher numbers are a decoy: the portal's table is
    // the one with wire evidence here.) Endianness is the file's, decided in
    // gguf_kv from the byte-swapped version field (HF transformers writers).
    let v = match t {
        0 => { let x = *b.get(*p)? as f64; *p += 1; Kv::N(x) }
        1 => { let x = *b.get(*p)? as i8 as f64; *p += 1; Kv::N(x) }
        2 | 3 => {
            let a: [u8; 2] = b.get(*p..*p + 2)?.try_into().ok()?;
            *p += 2;
            if be { Kv::N(if t == 2 { u16::from_be_bytes(a) as f64 } else { i16::from_be_bytes(a) as f64 }) }
            else { Kv::N(if t == 2 { u16::from_le_bytes(a) as f64 } else { i16::from_le_bytes(a) as f64 }) }
        }
        4 | 5 => {
            let a: [u8; 4] = b.get(*p..*p + 4)?.try_into().ok()?;
            *p += 4;
            if be { Kv::N(if t == 4 { u32::from_be_bytes(a) as f64 } else { i32::from_be_bytes(a) as f64 }) }
            else { Kv::N(if t == 4 { u32::from_le_bytes(a) as f64 } else { i32::from_le_bytes(a) as f64 }) }
        }
        6 => {
            let a: [u8; 4] = b.get(*p..*p + 4)?.try_into().ok()?;
            *p += 4;
            Kv::N(if be { f32::from_be_bytes(a) as f64 } else { f32::from_le_bytes(a) as f64 })
        }
        7 => { let x = *b.get(*p)? != 0; *p += 1; Kv::N(if x { 1.0 } else { 0.0 }) }
        10 | 11 => {
            let a: [u8; 8] = b.get(*p..*p + 8)?.try_into().ok()?;
            *p += 8;
            if be { Kv::N(if t == 10 { u64::from_be_bytes(a) as f64 } else { i64::from_be_bytes(a) as f64 }) }
            else { Kv::N(if t == 10 { u64::from_le_bytes(a) as f64 } else { i64::from_le_bytes(a) as f64 }) }
        }
        12 => {
            let a: [u8; 8] = b.get(*p..*p + 8)?.try_into().ok()?;
            *p += 8;
            Kv::N(if be { f64::from_be_bytes(a) } else { f64::from_le_bytes(a) })
        }
        8 => Kv::S(rd_str(b, p, be)?),
        9 => {
            let et = rd_u32(b, p, be)? as u8;
            let n = rd_u64(b, p, be)?;
            if n > 4096 {
                // tokenizer-scale array: walk past the ELEMENTS (geometry
                // lives elsewhere). Must not re-dispatch to rd_skip(9): that
                // re-reads the et/n header we just consumed and desyncs.
                // The LENGTH survives: tokenizer.ggml.tokens length IS the
                // vocab size (the portal's modelmeta derives it the same way).
                for _ in 0..n.min(1 << 22) {
                    rd_skip(b, p, et, be)?;
                }
                return Some(Kv::N(n as f64));
            }
            let mut v = Vec::new();
            for _ in 0..n {
                v.push(rd_val(b, p, et, be)?);
            }
            Kv::Arr(v)
        }
        _ => return None, // f16 (13) and unknown: refuse, caller breaks cleanly
    };
    Some(v)
}

fn rd_skip(b: &[u8], p: &mut usize, t: u8, be: bool) -> Option<()> {
    // walk past one value without storing it (huge arrays: tokenizer lists)
    match t {
        0 | 1 | 7 => { *p += 1; }
        2 | 3 => { *p += 2; }
        4 | 5 | 6 => { *p += 4; }
        10 | 11 | 12 => { *p += 8; }
        8 => { rd_str(b, p, be)?; }
        9 => {
            let et = rd_u32(b, p, be)? as u8;
            let n = rd_u64(b, p, be)?;
            for _ in 0..n.min(1 << 22) {
                rd_skip(b, p, et, be)?;
            }
        }
        _ => return None,
    }
    Some(())
}

fn gguf_kv(b: &[u8]) -> Option<std::collections::BTreeMap<String, Kv>> {
    // Endianness: the GGUF version field is byte-swapped in BE files (the
    // portal's sniff_gguf rule: (version & 0xFFFF) == 0 means big-endian).
    // HF transformers writers produce these; reading them little-endian
    // desyncs every pair.
    let ver_le = u32::from_le_bytes(b.get(4..8)?.try_into().ok()?);
    let be = (ver_le & 0xFFFF) == 0;
    // llama.cpp writes UNPADDED KV sections; gguf-py producers (HF
    // transformers writers) 8-byte-align every key. A plain walk over an
    // aligned file consumes the pad bytes as a type tag, reads one junk
    // pair, and "succeeds" with a 1-key map - so a plain-then-aligned
    // fallback must judge quality, not just Some-ness. Pick the walk that
    // reads architecture as a string, then more keys.
    let plain = gguf_kv_walk(b, false, be);
    let aligned = gguf_kv_walk(b, true, be);
    // The architecture-string check comes FIRST: a plain walk over an
    // aligned file reads pad bytes as a type and dies after one pair; an
    // aligned walk over an unpadded file reads type bytes as key-length and
    // collects a few nameless pairs. Architecture-as-string is the signal
    // only the correctly-aligned walk produces at full depth; key count
    // breaks ties (both walks see the first pair of an aligned file).
    let arch_s = |m: &Option<std::collections::BTreeMap<String, Kv>>|
        m.as_ref().map_or(0u8, |v| matches!(v.get("general.architecture"), Some(Kv::S(_))) as u8);
    let cnt = |m: &Option<std::collections::BTreeMap<String, Kv>>| m.as_ref().map_or(0, |v| v.len());
    let pick_aligned = arch_s(&aligned) > arch_s(&plain)
        || (arch_s(&aligned) == arch_s(&plain) && cnt(&aligned) > cnt(&plain));
    match (plain, aligned) {
        (Some(p), Some(a)) => Some(if pick_aligned { a } else { p }),
        (p, a) => p.or(a),
    }
}

fn gguf_kv_walk(b: &[u8], aligned: bool, be: bool) -> Option<std::collections::BTreeMap<String, Kv>> {
    if b.len() < 24 || &b[0..4] != b"GGUF" {
        return None;
    }
    let ver = u32::from_le_bytes(b[4..8].try_into().ok()?);
    let ver = if be { u32::from_be_bytes(b[4..8].try_into().ok()?) } else { ver };
    if ver < 2 {
        return None;
    }
    let mut p = 8usize; // GGUF head = magic(4) + version(4) + n_tensors(8) + n_kv(8)
    rd_u64(b, &mut p, be)?; // n_tensors
    let n_kv = rd_u64(b, &mut p, be)?;
    if n_kv > 100_000 {
        return None;
    }
    let mut m = std::collections::BTreeMap::new();
    for _ in 0..n_kv {
        if aligned {
            let pad = (8 - (p % 8)) % 8;
            p += pad;
        }
        let k = match rd_str(b, &mut p, be) {
            Some(k) => k,
            None => break, // buffer exhausted: keep what we have
        };
        let t = match rd_u32(b, &mut p, be) {
            Some(t) => t as u8,
            None => break,
        };
        match rd_val(b, &mut p, t, be) {
            Some(v) => {
                m.insert(k, v);
            }
            None => break,
        }
    }
    Some(m)
}

fn arr_nums(v: &Kv) -> Vec<f64> {
    match v {
        Kv::Arr(a) => a.iter().filter_map(|x| matches!(x, Kv::N(_)).then(|| match x { Kv::N(n) => *n, _ => 0.0 })).collect(),
        _ => vec![],
    }
}

/// Map a GGUF KV map to the portal's kv_bytes_at meta shape (arch prefix
/// stripped; per-layer arrays kept under the *_layers names the pricer reads).
fn gguf_meta_map(m: &std::collections::BTreeMap<String, Kv>) -> Value {
    let mut out = serde_json::Map::new();
    for (k, v) in m {
        let tail = k.split_once('.').map(|(_arch, rest)| rest).unwrap_or(k.as_str());
        let (name, val) = match (tail, v) {
            ("block_count", Kv::N(x)) => ("block_count", json!(*x as u64)),
            ("embedding_length", Kv::N(x)) => ("embedding_length", json!(*x as u64)),
            ("attention.head_count", Kv::N(x)) => ("attention_count", json!(*x as u64)),
            ("attention.head_count", Kv::Arr(_)) => ("attention_count_layers", json!(arr_nums(v).iter().map(|x| *x as u64).collect::<Vec<_>>())),
            ("attention.head_count_kv", Kv::N(x)) => ("attention_kv_count", json!(*x as u64)),
            ("attention.head_count_kv", Kv::Arr(_)) => ("attention_kv_count_layers", json!(arr_nums(v).iter().map(|x| *x as u64).collect::<Vec<_>>())),
            ("rope.dimension_count", Kv::N(x)) => ("rope_dimension_count", json!(*x as u64)),
            ("attention.full_attention_interval", Kv::N(x)) => ("full_attention_interval", json!(*x as u64)),
            ("attention.sliding_window", Kv::N(x)) => ("sliding_window", json!(*x as u64)),
            ("attention.sliding_window_pattern", Kv::Arr(_)) => ("sliding_window_pattern", json!(arr_nums(v).iter().map(|x| *x as u64).collect::<Vec<_>>())),
            ("feed_forward_length", Kv::N(x)) => ("feed_forward_length", json!(*x as u64)),
            ("vocab_size", Kv::N(x)) => ("vocab_size", json!(*x as u64)),
            ("expert_count", Kv::N(x)) => ("expert_count", json!(*x as u64)),
            ("expert_used_count", Kv::N(x)) => ("expert_used_count", json!(*x as u64)),
            ("feed_forward_length_expert", Kv::N(x)) => ("feed_forward_length_expert", json!(*x as u64)),
            ("attention.key_length", Kv::N(x)) => ("attention_key_length", json!(*x as u64)),
            ("attention.value_length", Kv::N(x)) => ("attention_value_length", json!(*x as u64)),
            ("context_length", Kv::N(x)) => ("context_length", json!(*x as u64)),
            // tokenizer.ggml.tokens survives as its LENGTH (the skip law):
            // that length is the vocab size, the same derivation the
            // portal's modelmeta uses.
            ("ggml.tokens", Kv::N(x)) => ("vocab_size", json!(*x as u64)),
            _ => continue,
        };
        out.insert(name.into(), val);
    }
    match m.get("general.file_type").or_else(|| m.get("general.filetype")) {
        // llama.cpp writes the numeric id (spec table); some tools write a string.
        Some(Kv::S(s)) => { out.insert("filetype".into(), json!(s.to_uppercase())); }
        Some(Kv::N(x)) => {
            // llama.cpp writes the ggml.h llama_ftype enum (Q8_0=7, Q5_K_M=17,
            // Q6_K=18) - witnessed on all 27 tagged models on this box; the
            // gguf-py "spec" numbering matches zero real files. Parity with
            // app/core/modelmeta.py GGUF_FILE_TYPE_NAMES.
            let name = match *x as u32 {
                0 => "F32", 1 => "F16", 2 => "BF16", 3 => "Q4_0", 4 => "Q4_1",
                7 => "Q8_0", 8 => "Q5_0", 9 => "Q5_1", 10 => "Q2_K", 11 => "Q3_K_S",
                12 => "Q3_K_M", 13 => "Q3_K_L", 14 => "Q4_K_S", 15 => "Q4_K_M",
                16 => "Q5_K_S", 17 => "Q5_K_M", 18 => "Q6_K", 19 => "IQ2_XXS",
                20 => "IQ2_XS", 21 => "Q2_K_S", 22 => "IQ3_XS", 23 => "IQ3_XXS",
                24 => "IQ1_S", 25 => "IQ4_NL", 26 => "IQ3_S", 27 => "IQ3_M",
                28 => "IQ2_S", 29 => "IQ2_M", 30 => "IQ4_XS", 31 => "IQ1_M",
                32 => "BF16",
                _ => "",
            };
            if !name.is_empty() {
                out.insert("filetype".into(), json!(name));
            }
        }
        Some(Kv::Arr(a)) => {
            if let Some(Kv::S(s)) = a.first() {
                out.insert("filetype".into(), json!(s.to_uppercase()));
            }
        }
        _ => {}
    }
    Value::Object(out)
}

fn gguf_meta(path: &str) -> Option<Value> {
    use std::io::Read;
    let mut f = std::fs::File::open(path).ok()?;
    // The KV section can run to several MiB: a 262k-token vocabulary is
    // ~2-3 MB of strings, and HF writers put general.file_type AFTER it.
    // 16 MiB covers every producer's head we have seen; weights are never
    // touched (that byte range is metadata, tensors live far below).
    let mut buf = Vec::with_capacity(1 << 20);
    let mut taken = f.take(16 << 20);
    taken.read_to_end(&mut buf).ok()?;
    let m = gguf_kv(&buf)?;
    Some(gguf_meta_map(&m))
}

/// Parsed header, cached by (path, size, mtime). caps() runs on every
/// status request; re-reading megabytes of vocabulary per poll is the
/// startup-cost law's "no burden on the node controller", and a model file
/// that changed size/mtime is re-read (the migration lane rewrites files).
fn gguf_meta_cached(path: &str) -> Option<Value> {
    use std::sync::{Mutex, OnceLock};
    static CACHE: OnceLock<Mutex<std::collections::HashMap<String, (u64, u64, Option<Value>)>>> = OnceLock::new();
    let cache = CACHE.get_or_init(Default::default);
    let st = std::fs::metadata(path).ok()?;
    let mt = st.modified().ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs()).unwrap_or(0);
    let key = path.to_string();
    {
        let g = cache.lock().ok()?;
        if let Some((l, m, v)) = g.get(&key) {
            if *l == st.len() && *m == mt {
                return v.clone();
            }
        }
    }
    let v = gguf_meta(path);
    if let Ok(mut g) = cache.lock() {
        g.insert(key, (st.len(), mt, v.clone()));
    }
    v
}

fn model_path_from(row: &Value) -> Option<String> {
    let args = row.get("status")?.get("args")?.as_array()?;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        if a.as_str() == Some("--model") {
            return it.next().and_then(|v| v.as_str()).map(String::from);
        }
    }
    None
}

/// Scalar-branch KV price at ctx (f16 cache) - the node-side floor.
/// Per-layer array models are priced fully by the PORTAL audit (kv_bytes_at);
/// the node estimate says so in capacity_src when arrays exist.
fn kv_bytes_meta(meta: &Value, ctx: u64) -> Option<u64> {
    let layers = meta.get("block_count")?.as_u64()?;
    let n_kv = meta.get("attention_kv_count")?.as_u64()?;
    let emb = meta.get("embedding_length")?.as_u64()?;
    let n_head = meta.get("attention_count").and_then(|v| v.as_u64())
        .or_else(|| meta.get("attention_count_layers").and_then(|a| a.as_array())
            .and_then(|a| a.iter().filter_map(|x| x.as_u64()).max()))
        .unwrap_or(0);
    if n_head == 0 {
        return None;
    }
    let head = meta.get("rope_dimension_count").and_then(|v| v.as_u64()).unwrap_or(emb / n_head);
    let interval = meta.get("full_attention_interval").and_then(|v| v.as_u64()).unwrap_or(1).max(1);
    let kv_layers = layers.div_ceil(interval);
    Some(kv_layers * n_kv * head * 2 * 2 * ctx)
}

fn ram_gb_min(weights: u64, kv: Option<u64>) -> u64 {
    let gibs = (weights + kv.unwrap_or(0)) as f64 / 1_073_741_824.0;
    gibs.ceil() as u64 + 1 // +1 GiB compute/graph margin (floor, honest label)
}

fn time_est(brow: Option<&Value>, weights_bytes: u64, disk_mb_s: f64) -> Value {
    let probe = 2048.0f64;
    let g = |k: &str| brow.and_then(|b| b.get(k)).and_then(|v| v.as_f64());
    let load_s = g("load_s").unwrap_or(weights_bytes as f64 / 1_048_576.0 / disk_mb_s.max(1.0));
    let pt = g("prompt_tps").unwrap_or(60.0); // unbenchmarked CPU-lane default
    let warm = probe / pt;
    let cold = load_s + warm;
    let tw = (warm * 2.5 + 15.0).ceil().max(30.0) as u64;
    let tc = (cold * 2.5 + 60.0).ceil().max(300.0) as u64;
    json!({
        "probe_tokens": 2048,
        "load_s": (load_s * 10.0).round() / 10.0,
        "prompt_tps": pt,
        "decode_tps": g("decode_tps"),
        "first_token_warm_s": (warm * 10.0).round() / 10.0,
        "first_token_cold_s": (cold * 10.0).round() / 10.0,
        "timeout_s": [tw, tc],
        "src": if brow.is_some() { "measured" } else { "computed" }
    })
}

/// New-model detection: ids the router reports that this node has never
/// advertised. First run after install is the baseline (no flood).
fn seen_diff(ids: &[String], seen: &[String]) -> Vec<String> {
    ids.iter().filter(|i| !seen.contains(i)).cloned().collect()
}

fn model_extras(row: &Value, bench: &Value) -> Value {
    let id = row.get("id").and_then(|i| i.as_str()).unwrap_or("");
    let ctx = row.get("context_length").and_then(|c| c.as_u64()).unwrap_or(0);
    let path = model_path_from(row);
    let meta = path.as_deref().and_then(gguf_meta_cached).unwrap_or(Value::Null);
    let weights = path.as_deref()
        .and_then(|p| std::fs::metadata(p).ok())
        .map(|m| m.len()).unwrap_or(0);
    let kv = meta.as_object().and_then(|_| kv_bytes_meta(&meta, ctx.max(8192)));
    let brow = bench.get("router").and_then(|r| r.get(id));
    let disk = bench.get("disk_read_mb_s").and_then(|v| v.as_f64()).unwrap_or(300.0);
    let t = time_est(brow, weights, disk);
    json!({
        "ram": if weights > 0 { json!(ram_gb_min(weights, kv)) } else { json!(8) },
        "time": t,
        "gguf": meta,
        "model_path": path.map(|p| json!(p)).unwrap_or(Value::Null),
        "weights_bytes": if weights > 0 { json!(weights) } else { Value::Null },
        "kv_bytes": kv.map(|x| json!(x)).unwrap_or(Value::Null),
        "capacity_src": if weights > 0 { "gguf-header" } else { "default (no file stat)" }
    })
}

// one tools/call to the LOCAL forge-mcp door; parse the JSON-RPC envelope
// (payload rides result.content[0].text as json; errors -> {ok:false}).
async fn forge_mcp_call(tool: &str, args: Value) -> Value {
    let client = reqwest::Client::new();
    let body = json!({ "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": { "name": tool, "arguments": args } });
    let r = match client
        .post(format!("{}/mcp", mcp_base()))
        .header("Accept", "application/json")
        .json(&body)
        .timeout(Duration::from_secs(300))
        .send()
        .await
    {
        Ok(r) => r,
        Err(e) => return json!({"ok": false, "error": e.to_string()}),
    };
    let v = match r.json::<Value>().await {
        Ok(v) => v,
        Err(e) => return json!({"ok": false, "error": e.to_string()}),
    };
    if let Some(e) = v.get("error") {
        return json!({"ok": false, "error": e
            .get("message").and_then(|m| m.as_str()).unwrap_or("jsonrpc error")});
    }
    match v
        .pointer("/result/content/0/text")
        .and_then(|t| serde_json::from_str::<Value>(t.as_str().unwrap_or("")).ok())
    {
        Some(p) => p,
        None => json!({"ok": false, "error": "forge-mcp envelope unparseable"}),
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

fn normalize_cards(raw: &Value, models: &Value, engine_state: &str,
                   bench: &Value, new_ids: &[String]) -> Vec<Value> {
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
                // RAM floors (owner 2026-11-17): renders mix CPU and GPU -
                // ComfyUI offloads to system RAM. Measured peak RSS per
                // contract comes from bench.json ("image": {name: {ram_peak_gib}});
                // unbenchmarked contracts carry a documented CPU-lane floor.
                let ram = bench.get("image").and_then(|i| i.get(name.as_str()))
                    .and_then(|b| b.get("ram_peak_gib")).and_then(|v| v.as_u64())
                    .unwrap_or(match name.as_str() {
                        "chroma_key" => 8,          // pure CPU lane: heaviest on RAM
                        "birefnet_cutout" => 12,
                        "svg_writer_llm" => 10,
                        "z_image_turbo_fast" => 12,
                        _ => 16,
                    });
                cards.push(json!({
                    "node": node_name(), "kind": "image-forge", "service": name,
                    "tier": c.get("tier").cloned().unwrap_or(json!("")),
                    "fills": c.get("fills").cloned().unwrap_or(json!([])),
                    "requires": { "vram_gb_min": vram, "ram_gb_min": ram, "backend": backend },
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
            let id = m.get("id").and_then(|i| i.as_str()).unwrap_or("").to_string();
            let ex = model_extras(m, bench);
            cards.push(json!({
                "node": node_name(), "kind": "llm-router",
                "service": m.get("id").cloned().unwrap_or(json!("?")),
                "tier": "router-preset",
                "fills": ["prompt"],
                "requires": { "ram_gb_min": ex["ram"], "backend": "llama-cuda-cpu" },
                "limits": {
                    "context": m.get("context_length").cloned().unwrap_or(json!(0)),
                    "timeout_s": ex["time"]["timeout_s"]
                },
                "idle_policy": { "sleep_idle_s": 900, "models_max": 1, "memory_margin_mib": 16384 },
                "state": m.get("state").cloned().unwrap_or(json!("catalog")),
                "new": new_ids.contains(&id),
                "measured": {
                    "note": "lanes measured 2026-11-16 on llama 9581154 (sm_75): CPU flint 18.14 t/s tg128; GPU qwen3-4b 135.3 t/s tg128; -t 16, -pg 512,128, r=2",
                    "gguf": ex["gguf"], "model_path": ex["model_path"],
                    "weights_bytes": ex["weights_bytes"], "kv_bytes": ex["kv_bytes"],
                    "capacity_src": ex["capacity_src"]
                },
                "time": ex["time"],
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
    // capacity inputs (all read-side): measured bench + the router id set vs
    // the seen-file (new-model detection; first run = baseline, no flood)
    let bench = read_json_file(&bench_file());
    let ids: Vec<String> = models.get("models").and_then(|m| m.as_array())
        .map(|a| a.iter().filter_map(|r| r.get("id").and_then(|i| i.as_str()).map(String::from)).collect())
        .unwrap_or_default();
    let seen_v = read_json_file(&seen_file());
    let seen: Vec<String> = seen_v.get("ids").and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str()).map(String::from).collect())
        .unwrap_or_default();
    let new_ids: Vec<String> = if seen_v.get("ids").is_some() { seen_diff(&ids, &seen) } else { vec![] };
    if !ids.is_empty() {
        if let Some(par) = std::path::Path::new(&seen_file()).parent() {
            let _ = std::fs::create_dir_all(par);
        }
        let _ = std::fs::write(seen_file(), json!({"ids": ids, "at": now_unix()}).to_string());
    }
    let cards = normalize_cards(&raw, &models, &engine_state, &bench, &new_ids);
    json!({
        "node": node_name(),
        "cards": cards,
        "models_total": ids.len(),
        "models_new": new_ids,
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

#[derive(Debug, Clone, Serialize, Deserialize, schemars::JsonSchema)]
struct OffloadArgs {
    model: String,
    lease_id: String,
    ctx: Option<u64>,
    ngl: Option<u64>,   // apply: clamp 1..99 (99 = full offload, owner law)
    action: String,     // apply | revert
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

    #[tool(description = "VRAM offload of ONE router model on THIS node (owner law 2026-11-17: mesh presets stay -ngl 0; -ngl 99 only as a lease-backed placement). action=apply requires a LIVE mesh_lease id (the admission ticket: no lease, no GPU; expired lease, no renewal) and forwards forge-mcp forge_router_offload (preset -ngl patch + on-demand load + measured report). action=revert sets the preset back to -ngl 0 and releases GPU weights at the router's idle sleep / next model switch; release the lease with lease_release. Never touches halo: this is the node's own file, launcher and router.")]
    async fn mesh_router_offload(&self, Parameters(a): Parameters<OffloadArgs>) -> Result<CallToolResult, McpError> {
        let ok_model = !a.model.is_empty()
            && a.model.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
        if !ok_model {
            return Ok(CallToolResult::success(vec![ContentBlock::json(
                json!({"ok": false, "state": "refused", "reason": "model id required ([A-Za-z0-9_-])"}))?]));
        }
        // guard in a block: std MutexGuard dtors run at scope end, and the
        // await below needs a Send future (guard must die before it)
        let lease = {
            let mut g = LEDGER.lock().unwrap();
            prune(&mut g);
            g.iter().find(|l| l.id == a.lease_id).cloned()
        };
        let tool = if a.action == "revert" { "forge_router_offload_revert" } else { "forge_router_offload" };
        if a.action != "revert" {
            match &lease {
                None => return Ok(CallToolResult::success(vec![ContentBlock::json(
                    json!({"ok": false, "state": "refused",
                           "reason": format!("no live lease {} - mesh_lease is the admission ticket (expired leases are refused; re-grant first)", a.lease_id)}))?])),
                Some(l) if l.vram_mib == 0 => return Ok(CallToolResult::success(vec![ContentBlock::json(
                    json!({"ok": false, "state": "refused",
                           "reason": format!("lease {} commits no VRAM - apply needs a vram_mib lease", a.lease_id)}))?])),
                Some(_) => {}
            }
        }
        let mut args = json!({"model": a.model, "lease_id": a.lease_id});
        if a.action != "revert" {
            args["ngl"] = json!(a.ngl.unwrap_or(99).clamp(1, 99));
            args["ctx"] = json!(a.ctx.unwrap_or(8192));
        }
        let out = forge_mcp_call(tool, args).await;
        Ok(CallToolResult::success(vec![ContentBlock::json(json!({
            "node": node_name(), "model": a.model, "lease_id": a.lease_id,
            "lease_vram_mib": lease.map(|l| l.vram_mib).unwrap_or(0),
            "forge": out }))?]))
    }
}

#[tool_handler(name = "meshd", version = "0.3.5",
               instructions = "Node controller (phase 2+): mesh_status aggregates health+services+caps+leases; mesh_lease/lease_release reserve vram/ram with TTLs; mesh_router_offload applies/reverts lease-backed VRAM offload via forge-mcp (presets stay -ngl 0; apply requires a live lease); mesh_service starts/stops registry services (deny-list: llama-router, opencode-agent, meshd, portal). All tools bearer-gated (ONE-token, hub-first).")]
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

// ---------------- tests (capacity math is pure; wire lanes are proven on-box) ----------------

#[cfg(test)]
mod cap_tests {
    use super::*;

    fn push_str_be(b: &mut Vec<u8>, s: &str) {
        b.extend((s.len() as u64).to_be_bytes());
        b.extend(s.as_bytes());
    }
    fn push_str(b: &mut Vec<u8>, s: &str) {
        b.extend((s.len() as u64).to_le_bytes());
        b.extend(s.as_bytes());
    }
    fn kv_str(b: &mut Vec<u8>, k: &str, v: &str) {
        push_str(b, k); b.extend(8u32.to_le_bytes()); push_str(b, v);   // STRING = 8 (v3)
    }
    fn kv_u32(b: &mut Vec<u8>, k: &str, v: u32) {
        push_str(b, k); b.extend(4u32.to_le_bytes()); b.extend(v.to_le_bytes());   // U32 = 4 (portal table), type is u32
    }
    fn kv_u32arr(b: &mut Vec<u8>, k: &str, vs: &[u32]) {
        push_str(b, k); b.extend(9u32.to_le_bytes()); b.extend(4u32.to_le_bytes()); // ARRAY=9 of U32=4 (portal table)
        b.extend((vs.len() as u64).to_le_bytes());
        for v in vs { b.extend(v.to_le_bytes()); }
    }
    fn synth_gguf_n(n_kv: u64) -> Vec<u8> {
        let mut b = vec![];
        b.extend(b"GGUF");
        b.extend(3u32.to_le_bytes());
        b.extend(0u64.to_le_bytes()); // n_tensors
        b.extend(n_kv.to_le_bytes());
        kv_str(&mut b, "general.architecture", "llama");
        kv_u32(&mut b, "general.file_type", 7);   // llama_ftype enum: 7 = Q8_0
        kv_u32(&mut b, "llama.block_count", 32);
        kv_u32(&mut b, "llama.embedding_length", 4096);
        kv_u32(&mut b, "llama.attention.head_count", 32);
        kv_u32(&mut b, "llama.attention.head_count_kv", 8);
        kv_u32(&mut b, "llama.rope.dimension_count", 128);
        kv_u32(&mut b, "llama.context_length", 131072);
        kv_u32arr(&mut b, "llama.attention.sliding_window_pattern", &[1, 0, 1, 0]);
        kv_u32(&mut b, "llama.vocab_size", 128000);
        kv_u32(&mut b, "llama.expert_count", 64);
        kv_u32(&mut b, "llama.expert_used_count", 8);
        b
    }

    fn synth_gguf() -> Vec<u8> { synth_gguf_n(12) }

    #[test]
    fn gguf_header_parses_to_pricer_shape() {
        let m = gguf_kv(&synth_gguf()).expect("parses");
        let meta = gguf_meta_map(&m);
        assert_eq!(meta["block_count"], json!(32));
        assert_eq!(meta["attention_kv_count"], json!(8));
        assert_eq!(meta["filetype"], json!("Q8_0"));
        assert_eq!(meta["sliding_window_pattern"], json!([1, 0, 1, 0]));
        assert_eq!(meta["vocab_size"], json!(128000));
        assert_eq!(meta["expert_count"], json!(64));
        assert_eq!(meta["expert_used_count"], json!(8));
        // garbage and truncation refuse, never guess
        assert!(gguf_kv(b"nope").is_none());
        let part = gguf_kv(&synth_gguf()[..40]).expect("best-effort partial");
        assert!(part.is_empty() || part.values().len() < 9);
    }

    #[test]
    fn kv_math_matches_the_pricer_formula() {
        let meta = gguf_meta_map(&gguf_kv(&synth_gguf()).unwrap());
        // 32 layers * 8 kv heads * 128 dim * 2 (K,V) * 2 bytes * 8192 tokens = 1 GiB
        assert_eq!(kv_bytes_meta(&meta, 8192), Some(32 * 8 * 128 * 2 * 2 * 8192));
        // interval-aware: every 4th layer caches
        let mut v = meta.as_object().unwrap().clone();
        v.insert("full_attention_interval".into(), json!(4));
        assert_eq!(kv_bytes_meta(&Value::Object(v), 8192), Some((32 / 4) * 8 * 128 * 2 * 2 * 8192));
        // missing kv heads: None, never a zero-cost lie
        assert!(kv_bytes_meta(&json!({"block_count": 32}), 8192).is_none());
    }

    #[test]
    fn timeouts_carry_the_generous_buffer() {
        // unbenchmarked 8 GiB model, 300 MB/s: load ~27.3 s; warm 2048/60 ~34.1 s
        let t = time_est(None, 8 << 30, 300.0);
        assert_eq!(t["src"], json!("computed"));
        let pair = t["timeout_s"].as_array().unwrap();
        assert!(pair[0].as_u64().unwrap() >= 30 && pair[1].as_u64().unwrap() >= 300);
        // measured bench rows flip the source and use measured numbers
        let t2 = time_est(Some(&json!({"load_s": 4.6, "prompt_tps": 660.0, "decode_tps": 21.1})),
                          8 << 30, 300.0);
        assert_eq!(t2["src"], json!("measured"));
        assert_eq!(t2["decode_tps"], json!(21.1));
        assert!((t2["first_token_warm_s"].as_f64().unwrap() - 3.1).abs() < 0.1);
    }

    #[test]
    fn ram_floor_includes_kv_and_margin() {
        assert_eq!(ram_gb_min(2 << 30, Some(1 << 30)), 4);   // 2+1 GiB ceil +1 margin
        assert_eq!(ram_gb_min(0, None), 1);
    }

    #[test]
    fn big_endian_gguf_parses_too() {
        // HF transformers writers produce byte-swapped GGUFs: the version
        // field reads 0x02000000 LE (= (v & 0xFFFF) == 0) and every number
        // after it is BE. A LE reader desyncs on the first pair.
        let mut b = vec![];
        b.extend(b"GGUF");
        b.extend(2u32.to_be_bytes());
        b.extend(0u64.to_be_bytes());
        b.extend(2u64.to_be_bytes());
        push_str_be(&mut b, "general.architecture"); b.extend(8u32.to_be_bytes()); push_str_be(&mut b, "gemma");
        push_str_be(&mut b, "general.file_type"); b.extend(4u32.to_be_bytes()); b.extend(8u32.to_be_bytes()); // 8 = Q5_0 (BE)
        let m = gguf_kv(&b).expect("BE file parses");
        assert_eq!(m.get("general.architecture"), Some(&Kv::S("gemma".into())));
        let meta = gguf_meta_map(&m);
        assert_eq!(meta["filetype"], json!("Q5_0"));
    }

    #[test]
    fn keys_after_big_arrays_survive() {
        // the scores/tokenizer law: HF writers put general.file_type AFTER
        // the 262k-token vocab and scores arrays. Re-dispatching the skip
        // past the consumed et/n header desynced the walk and lost them.
        let mut b = synth_gguf_n(15);   // 12 synth + tokens + scores + file_type dup
        // tokens: ARRAY of STRING, 5000 entries (> 4096 skip threshold)
        push_str(&mut b, "tokenizer.ggml.tokens"); b.extend(9u32.to_le_bytes());
        b.extend(8u32.to_le_bytes()); b.extend((5000u64).to_le_bytes());
        for i in 0..5000u32 { push_str(&mut b, &format!("tok{}", i)); }
        // scores: ARRAY of F32, 5000 entries
        push_str(&mut b, "tokenizer.ggml.scores"); b.extend(9u32.to_le_bytes());
        b.extend(6u32.to_le_bytes()); b.extend((5000u64).to_le_bytes());
        for _ in 0..5000 { b.extend(1.0f32.to_le_bytes()); }
        kv_u32(&mut b, "general.file_type", 7);   // after the arrays
        let m = gguf_kv(&b).expect("parses past the arrays");
        assert_eq!(m.len(), 14, "12 synth + tokens + scores (file_type dup dedups)");
        assert!(m.contains_key("tokenizer.ggml.tokens") && m.contains_key("tokenizer.ggml.scores"));
        assert_eq!(gguf_meta_map(&m)["filetype"], json!("Q8_0"));
    }

    #[test]
    fn aligned_kv_section_is_detected_by_arch_string() {
        // gguf-py producers pad every key to 8 bytes. A plain walk reads the
        // pad as a type tag and dies after one pair; quality selection must
        // pick the aligned walk, not the first Some.
        let mut b = vec![];
        b.extend(b"GGUF"); b.extend(3u32.to_le_bytes());
        b.extend(0u64.to_le_bytes()); b.extend(3u64.to_le_bytes());
        let mut pad = |b: &mut Vec<u8>| { let pad = (8 - (b.len() % 8)) % 8; b.extend(vec![0u8; pad]); };
        pad(&mut b); push_str(&mut b, "general.architecture"); b.extend(8u32.to_le_bytes()); push_str(&mut b, "gemma");
        pad(&mut b); push_str(&mut b, "general.file_type"); b.extend(4u32.to_le_bytes()); b.extend(7u32.to_le_bytes());
        pad(&mut b); push_str(&mut b, "gemma.block_count"); b.extend(4u32.to_le_bytes()); b.extend(18u32.to_le_bytes());
        let m = gguf_kv(&b).expect("aligned file parses");
        assert_eq!(m.len(), 3, "aligned walk beats the plain walk's one-pair success");
        assert_eq!(gguf_meta_map(&m)["filetype"], json!("Q8_0"));
    }

    #[test]
    fn seen_detection_is_quiet_on_baseline() {
        assert_eq!(seen_diff(&["a".into(), "b".into()], &["a".into()]), vec!["b".to_string()]);
        assert!(seen_diff(&["a".into()], &["a".into(), "b".into()]).is_empty());
    }

    #[test]
    fn model_path_comes_from_the_router_args() {
        let row = json!({"status": {"args": ["--model", "/m/x.gguf", "--alias", "x"]}});
        assert_eq!(model_path_from(&row).as_deref(), Some("/m/x.gguf"));
        assert_eq!(model_path_from(&json!({"status": {"args": []}})), None);
        // extras on an unstatable path degrade honestly: default ram, computed time
        let ex = model_extras(&json!({"id": "ghost", "context_length": 8192,
                                      "status": {"args": ["--model", "/no/such.gguf"]}}),
                              &json!({}));
        assert_eq!(ex["ram"], json!(8));
        assert_eq!(ex["capacity_src"], json!("default (no file stat)"));
        assert_eq!(ex["time"]["src"], json!("computed"));
    }
}

#[cfg(test)]
mod dbg_real {
    use super::*;
    #[test]
    fn real_gguf_head() {
        // real fixtures beat synthetic ones: parse an actual model on this box
        let p = "/home/cortana/ai/models/llama/DeepSeek-Coder-V2-Lite-Instruct-Q8_0/DeepSeek-Coder-V2-Lite-Instruct-Q8_0.gguf";
        if !std::path::Path::new(p).exists() { return; }
        let meta = gguf_meta(p).expect("real gguf parses");
        assert!(meta["block_count"].as_u64().unwrap() > 0);
        // the file declares llama_ftype 7 = Q8_0: the header agrees with the
        // directory name. (Under the old gguf-py-numbered table this came back
        // "Q5_1" - one more proof the header lane must speak the real enum.)
        assert_eq!(meta["filetype"].as_str().unwrap(), "Q8_0");
        assert!(meta["attention_kv_count"].as_u64().unwrap() > 0);
        let kv = kv_bytes_meta(&meta, 8192).expect("priceable");
        assert!(kv > 0);
    }
}



