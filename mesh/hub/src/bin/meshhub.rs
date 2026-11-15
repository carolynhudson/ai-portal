// meshhub: the mesh hub daemon (node registry + secrets broker).
// Runs on the Pi pair. Primary = single writer (NVMe store) with live
// rsync mirror to the standby; standby serves reads and 503s writes
// unless TAKEOVER exists. Custody = ciphertext only: the hub never
// sees plaintext, so ACLs live in age cryptography, not hub config.
use std::collections::HashMap;
use std::fs;
use std::io::{BufReader, Write};
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::Command;
use std::sync::{Arc, Mutex};

use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use rustls::server::WebPkiClientVerifier;
use rustls::{RootCertStore, ServerConfig};

use meshhub::{err, http_read, http_write, is_age_armor, j, now, rand_hex, sha256_hex, valid_name, Node, Rec, VERSION};

#[derive(serde::Deserialize)]
struct Mirror {
    host: String,
    key: String,
    #[serde(default)]
    enabled: bool,
}

#[derive(serde::Deserialize)]
struct Cfg {
    listen: String,
    tls_pem: String, // box console pair: KEY-first PEM (FTL layout), read as root
    root_ca: String, // mesh CA root for client verification
    store: String, // /var/lib/meshhub/store
    takeover: String,
    max_bytes: u64,
    mirror: Mirror,
}

#[derive(Default, serde::Serialize, serde::Deserialize)]
struct Store {
    nodes: HashMap<String, Node>, // key: cert fp
    recs: HashMap<String, Rec>, // key: secret name
}

struct Hub {
    cfg: Cfg,
    store: Mutex<Store>,
}

fn audit(op: &str, who: &str, obj: &str, res: &str) {
    // journald only: metadata, never payload/ciphertext/tokens.
    println!("MESH-HUB op={op} who={who} obj={obj} res={res} t={}", now());
}

fn can_read(rec: &Rec, fp: &str, name: &Option<String>) -> bool {
    rec.owner == fp
        || name.as_deref().map(|n| rec.recipients.iter().any(|r| r == n)).unwrap_or(false)
}

impl Hub {
    fn paths(&self) -> (String, String, String) {
        (
            format!("{}/nodes.json", self.cfg.store),
            format!("{}/index.json", self.cfg.store),
            format!("{}/blobs", self.cfg.store),
        )
    }

    fn load(&self) -> Store {
        let (np, ip, _) = self.paths();
        let nodes = fs::read_to_string(&np).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
        let recs = fs::read_to_string(&ip).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
        Store { nodes, recs }
    }

    fn persist_nodes(&self, st: &Store) -> Result<(), String> {
        let (np, _, _) = self.paths();
        write_atomic(&np, &serde_json::to_vec_pretty(&st.nodes).unwrap())
    }

    fn persist_index(&self, st: &Store) -> Result<(), String> {
        let (_, ip, _) = self.paths();
        write_atomic(&ip, &serde_json::to_vec_pretty(&st.recs).unwrap())
    }

    fn mirror(&self, res: &str) {
        if !self.cfg.mirror.enabled {
            return;
        }
        let ssh = format!(
            "ssh -i {} -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new",
            self.cfg.mirror.key
        );
        // proven pair channel: root -> meshops@pihole2.local staging -> sudo apply
        let a = Command::new("rsync")
            .args(["-aq", "--delete", "-e", &ssh, &format!("{}/", self.cfg.store), &format!("{}:meshhub-staging/", self.cfg.mirror.host)])
            .output();
        let b = Command::new("ssh")
            .args(["-i", &self.cfg.mirror.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", &self.cfg.mirror.host, "sudo /usr/local/bin/meshhub-apply-sync.sh"])
            .output();
        match (a, b) {
            (Ok(o1), Ok(o2)) if o1.status.success() && o2.status.success() => audit("mirror", "hub", res, "ok"),
            (Ok(o1), Ok(o2)) => {
                let e = format!(
                    "{} | {}",
                    String::from_utf8_lossy(&o1.stderr).replace('\n', " "),
                    String::from_utf8_lossy(&o2.stderr).replace('\n', " ")
                );
                audit("mirror", "hub", res, &format!("FAIL {e}"))
            }
            (a, b) => audit("mirror", "hub", res, &format!("FAIL {a:?} {b:?}")),
        }
    }

    // Single-writer law (mirror of the DHCP single-authority rule):
    // writes accepted only where a WRITER marker (primary, set at
    // install, OUTSIDE the mirrored store dir) or a TAKEOVER file
    // (manual promotion) exists.
    fn write_guard(&self) -> Option<Vec<u8>> {
        if Path::new(&self.cfg.takeover).exists() || Path::new("/var/lib/meshhub/WRITER").exists() {
            return None;
        }
        Some(err(503, "standby: writes go to the primary (touch TAKEOVER on this box to promote)"))
    }
}

fn write_apply_script() {
    // pi2-side applier for the staging channel (installed on BOTH boxes;
    // a root script that only runs when we ssh in as meshops+sudo).
    let body = "#!/bin/sh\nset -e\nmkdir -p /var/lib/meshhub/store\nrsync -aq --delete /home/meshops/meshhub-staging/ /var/lib/meshhub/store/\n";
    let _ = std::fs::write("/usr/local/bin/meshhub-apply-sync.sh", body);
    let _ = std::fs::set_permissions("/usr/local/bin/meshhub-apply-sync.sh", std::fs::Permissions::from_mode(0o755));
}

fn write_atomic(path: &str, bytes: &[u8]) -> Result<(), String> {
    let tmp = format!("{path}.tmp");
    {
        let mut f = fs::File::create(&tmp).map_err(|e| format!("create: {e}"))?;
        f.write_all(bytes).map_err(|e| format!("write: {e}"))?;
        f.sync_all().map_err(|e| format!("fsync: {e}"))?;
    }
    fs::rename(&tmp, path).map_err(|e| format!("rename: {e}"))?;
    Ok(())
}

fn cert_fp(chain: &[CertificateDer]) -> String {
    sha256_hex(chain.first().map(|c| c.as_ref()).unwrap_or(&[]))
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let cfg_path = args.get(1).cloned().unwrap_or_else(|| "/etc/meshhub/config.json".into());
    let cfg: Cfg =
        serde_json::from_str(&fs::read_to_string(&cfg_path).expect("read config")).expect("parse config");
    let hub = Arc::new(Hub { cfg, store: Mutex::new(Store::default()) });
    *hub.store.lock().unwrap() = hub.load();
    write_apply_script();

    // TLS: server cert = the box console pair; clients verified vs mesh root.
    let certs: Vec<CertificateDer> = {
        let f = fs::File::open(&hub.cfg.tls_pem).expect("open tls_pem");
        rustls_pemfile::certs(&mut BufReader::new(f)).collect::<Result<Vec<_>, _>>().expect("parse certs")
    };
    let key: PrivateKeyDer = {
        let f = fs::File::open(&hub.cfg.tls_pem).expect("open tls_pem");
        let mut br = BufReader::new(f);
        loop {
            match rustls_pemfile::read_one(&mut br).expect("read pem") {
                Some(rustls_pemfile::Item::Pkcs8Key(k)) => break PrivateKeyDer::Pkcs8(k),
                Some(rustls_pemfile::Item::Pkcs1Key(k)) => break PrivateKeyDer::Pkcs1(k),
                Some(rustls_pemfile::Item::Sec1Key(k)) => break PrivateKeyDer::Sec1(k),
                None => panic!("no key in {}", hub.cfg.tls_pem),
                Some(_) => continue,
            }
        }
    };
    let mut roots = RootCertStore::empty();
    let root_certs: Vec<CertificateDer> = {
        let f = fs::File::open(&hub.cfg.root_ca).expect("open root_ca");
        rustls_pemfile::certs(&mut BufReader::new(f)).collect::<Result<Vec<_>, _>>().expect("parse root")
    };
    roots.add_parsable_certificates(root_certs);

    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let verifier = WebPkiClientVerifier::builder(Arc::new(roots)).build().expect("client verifier");
    let sc = ServerConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS12, &rustls::version::TLS13])
        .expect("protocols")
        .with_client_cert_verifier(verifier)
        .with_single_cert(certs, key)
        .expect("server config");
    let sc = Arc::new(sc);

    let listener = TcpListener::bind(&hub.cfg.listen).expect("bind");
    println!("MESH-HUB listening {} v{VERSION}", hub.cfg.listen);
    for tcp in listener.incoming() {
        let Ok(tcp) = tcp else { continue };
        let hub = hub.clone();
        let sc = sc.clone();
        std::thread::spawn(move || {
            let _ = handle(hub, sc, tcp);
        });
    }
}

fn handle(hub: Arc<Hub>, sc: Arc<ServerConfig>, mut tcp: std::net::TcpStream) -> std::io::Result<()> {
    tcp.set_read_timeout(Some(std::time::Duration::from_secs(15)))?;
    tcp.set_nodelay(true);
    let mut conn = rustls::ServerConnection::new(sc)
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    let mut stream = rustls::Stream::new(&mut conn, &mut tcp);
    let Some(req) = http_read(&mut stream)? else { return Ok(()) };
    let fp: String = stream.conn.peer_certificates().map(cert_fp).unwrap_or_default();
    let (status, body) = route(&hub, &req.method, &req.path, &req.body, &fp);
    http_write(&mut stream, status, &body)
}

fn route(hub: &Hub, method: &str, path: &str, body: &[u8], fp: &str) -> (u16, Vec<u8>) {
    if path == "/healthz" {
        return (200, j(&serde_json::json!({ "ok": true, "version": VERSION })));
    }
    // Disk is the truth: reload per request. The standby's registry
    // advances with replication (no stale boot snapshot), and the pair
    // shares one code path. Store is KB-scale by law.
    {
        let mut stg = hub.store.lock().unwrap();
        *stg = hub.load();
    }
    if fp.is_empty() {
        return (400, err(400, "no client certificate"));
    }

    // ENROLL is the registration act: mesh-CA-authenticated cert (TLS
    // already verified) + declaration. Two-party: the cert only exists
    // if a pi-root operator minted the token.
    if method == "POST" && path == "/enroll" {
        #[derive(serde::Deserialize)]
        struct En {
            name: String,
            agepub: String,
            #[serde(default)]
            roles: Vec<String>,
        }
        let e: En = match serde_json::from_slice(body) {
            Ok(e) => e,
            Err(e) => return (400, err(400, &format!("json: {e}"))),
        };
        if !valid_name(&e.name) || !e.agepub.starts_with("age1") || e.agepub.len() < 20 {
            return (400, err(400, "bad name or age pubkey"));
        }
        let mut st = hub.store.lock().unwrap();
        if let Some(existing) = st.nodes.get(fp) {
            let mut n = existing.clone();
            if n.name != e.name {
                return (409, err(409, &format!("this identity is registered as {}", n.name)));
            }
            n.agepub = e.agepub;
            n.roles = e.roles;
            n.last = now();
            st.nodes.insert(fp.to_string(), n.clone());
            if let Err(er) = hub.persist_nodes(&st) {
                return (500, err(500, &er));
            }
            drop(st);
            hub.mirror("nodes");
            audit("enroll", &e.name, "-", "refresh");
            return (200, j(&n));
        }
        if st.nodes.values().any(|n| n.name == e.name) {
            audit("enroll", &e.name, "-", "DENIED:name-taken");
            return (409, err(409, "name already registered to a different identity (remove it first)"));
        }
        let n = Node { name: e.name.clone(), agepub: e.agepub, roles: e.roles, added: now(), last: now(), fp: fp.to_string() };
        st.nodes.insert(fp.to_string(), n.clone());
        if let Err(er) = hub.persist_nodes(&st) {
            return (500, err(500, &er));
        }
        drop(st);
        hub.mirror("nodes");
        audit("enroll", &e.name, &format!("fp={}", &fp[..16]), "registered");
        return (200, j(&n));
    }

    // everything else requires a registered identity
    let st = hub.store.lock().unwrap();
    let node = st.nodes.get(fp).cloned();
    let who = node.as_ref().map(|n| n.name.clone()).unwrap_or_else(|| format!("unregistered:{}", &fp[..fp.len().min(12)]));
    let node = match node {
        Some(n) => n,
        None => return (401, err(401, "identity not registered: run meshctl enroll first")),
    };
    let name = Some(node.name.clone());
    // RELEASE the guard: every branch below takes its own lock. Holding
    // this across write paths would deadlock.
    drop(st);

    // touch last-seen on any authenticated call
    {
        let mut stg = hub.store.lock().unwrap();
        if let Some(n) = stg.nodes.get_mut(fp) {
            n.last = now();
        }
    }

    match (method, path) {
        ("GET", "/nodes") => {
            let st = hub.store.lock().unwrap();
            let mut v: Vec<&Node> = st.nodes.values().collect();
            v.sort_by(|a, b| a.name.cmp(&b.name));
            return (200, j(&serde_json::json!({ "nodes": v })));
        }
        ("GET", "/secrets") => {
            let st = hub.store.lock().unwrap();
            let mine: Vec<&Rec> = st.recs.values().filter(|r| can_read(r, fp, &name)).collect();
            return (200, j(&serde_json::json!({ "secrets": mine })));
        }
        _ => {}
    }

    if let Some(sec) = path.strip_prefix("/secrets/") {
        let secname = sec.trim_end_matches('/');
        if !valid_name(secname) {
            return (400, err(400, "bad secret name"));
        }
        match method {
            "PUT" => {
                if let Some(block) = hub.write_guard() {
                    audit("put", &who, secname, "standby-503");
                    return (503, block);
                }
                #[derive(serde::Deserialize)]
                struct Put {
                    recipients: Vec<String>,
                    #[serde(default)]
                    kind: String,
                    data: String,
                }
                let p: Put = match serde_json::from_slice(body) {
                    Ok(p) => p,
                    Err(e) => return (400, err(400, &format!("json: {e}"))),
                };
                if !is_age_armor(&p.data) {
                    audit("put", &who, secname, "denied:not-age");
                    return (400, err(400, "data must be age-armored ciphertext (meshctl encrypts)"));
                }
                if p.data.len() as u64 > hub.cfg.max_bytes {
                    return (413, err(413, &format!("too large (max {} bytes)", hub.cfg.max_bytes)));
                }
                let kind = if p.kind == "blob" { "blob" } else { "secret" };
                let mut st = hub.store.lock().unwrap();
                for r in &p.recipients {
                    if !st.nodes.values().any(|n| &n.name == r) {
                        return (400, err(400, &format!("unknown recipient node: {r}")));
                    }
                }
                let existing = st.recs.get(secname).cloned();
                let owner = match &existing {
                    Some(r) if r.owner != fp => {
                        audit("put", &who, secname, "denied:not-owner");
                        return (403, err(403, "not the owner of this secret"));
                    }
                    Some(r) => r.owner.clone(),
                    None => fp.to_string(),
                };
                let id = existing.as_ref().map(|r| r.id.clone()).unwrap_or_else(|| rand_hex(12));
                let sha = sha256_hex(p.data.as_bytes());
                let blobpath = format!("{}/blobs/{id}.age", hub.cfg.store);
                if let Err(e) = write_atomic(&blobpath, p.data.as_bytes()) {
                    return (500, err(500, &e));
                }
                let rec = Rec {
                    id,
                    name: secname.to_string(),
                    owner,
                    recipients: p.recipients.clone(),
                    kind: kind.to_string(),
                    size: p.data.len() as u64,
                    sha,
                    created: existing.as_ref().map(|r| r.created).unwrap_or_else(now),
                    updated: now(),
                };
                st.recs.insert(secname.to_string(), rec.clone());
                if let Err(e) = hub.persist_index(&st) {
                    return (500, err(500, &e));
                }
                drop(st);
                hub.mirror(secname);
                audit("put", &who, secname, &format!("ok {}B rcpts={}", p.data.len(), p.recipients.join(",")));
                return (200, j(&rec));
            }
            "GET" => {
                let st = hub.store.lock().unwrap();
                let r = match st.recs.get(secname) {
                    Some(r) => r.clone(),
                    None => return (404, err(404, "no such secret")),
                };
                if !can_read(&r, fp, &name) {
                    audit("get", &who, secname, "DENIED");
                    return (403, err(403, "not in recipient set"));
                }
                let data = match fs::read(format!("{}/blobs/{}.age", hub.cfg.store, r.id)) {
                    Ok(d) => d,
                    Err(_) => return (410, err(410, "blob missing (restore from mirror/backup)")),
                };
                audit("get", &who, secname, "ok");
                return (200, j(&serde_json::json!({ "record": r, "data": String::from_utf8_lossy(&data) })));
            }
            "DELETE" => {
                if let Some(block) = hub.write_guard() {
                    return (503, block);
                }
                let mut st = hub.store.lock().unwrap();
                let r = match st.recs.get(secname) {
                    Some(r) => r.clone(),
                    None => return (404, err(404, "no such secret")),
                };
                if r.owner != fp {
                    audit("rm", &who, secname, "DENIED:not-owner");
                    return (403, err(403, "not the owner"));
                }
                st.recs.remove(secname);
                if let Err(e) = hub.persist_index(&st) {
                    return (500, err(500, &e));
                }
                drop(st);
                let _ = fs::remove_file(format!("{}/blobs/{}.age", hub.cfg.store, r.id));
                hub.mirror(secname);
                audit("rm", &who, secname, "ok");
                return (200, j(&serde_json::json!({ "ok": true })));
            }
            _ => return (405, err(405, "method")),
        }
    }

    (404, err(404, "no such endpoint"))
}
