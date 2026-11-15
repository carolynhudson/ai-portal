// meshctl: mesh hub client (node side). ALL encryption/decryption
// happens HERE, on the client: the hub only ever stores/transports
// age ciphertext. ACLs therefore live in cryptography, not hub config.
use std::io::{Read, Write};
use std::net::TcpStream;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use rustls::pki_types::ServerName;
use rustls::{ClientConfig, RootCertStore};

use meshhub::{age_decrypt, age_encrypt, cert_fp_pem, gen_identity, http_request, load_certs, load_key, Node, Resp};

#[derive(serde::Deserialize, serde::Serialize)]
struct Cfg {
    hub: Vec<String>,
    root_ca: String,
    cert: String,
    key: String,
    identity: String,
}

fn meshdir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    Path::new(&home).join(".mesh")
}

fn resolve(p: &str) -> PathBuf {
    let pp = Path::new(p);
    if pp.is_absolute() {
        pp.to_path_buf()
    } else {
        meshdir().join(pp)
    }
}

fn load_cfg() -> Result<Cfg, String> {
    let p = meshdir().join("config.json");
    let s = std::fs::read_to_string(&p).map_err(|_| format!("no config at {} -- run meshctl enroll", p.display()))?;
    serde_json::from_str(&s).map_err(|e| e.to_string())
}

fn client_cfg(c: &Cfg, need_cert: bool) -> Result<Arc<ClientConfig>, String> {
    let mut roots = RootCertStore::empty();
    roots.add_parsable_certificates(load_certs(&resolve(&c.root_ca).to_string_lossy())?);
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let b = ClientConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS12, &rustls::version::TLS13])
        .map_err(|e| e.to_string())?
        .with_root_certificates(roots);
    let cc = if need_cert {
        b.with_client_auth_cert(
            load_certs(&resolve(&c.cert).to_string_lossy())?,
            load_key(&resolve(&c.key).to_string_lossy())?,
        )
        .map_err(|e| e.to_string())?
    } else {
        b.with_no_client_auth()
    };
    Ok(Arc::new(cc))
}

fn call(c: &Cfg, need_cert: bool, method: &str, path: &str, body: Option<&[u8]>) -> Result<Resp, String> {
    let cc = client_cfg(c, need_cert)?;
    let mut last = "no hubs configured".to_string();
    for h in &c.hub {
        let (host, port) = match h.rsplit_once(':') {
            Some((h, p)) => (h.to_string(), p.parse().unwrap_or(9443)),
            None => (h.clone(), 9443),
        };
        let attempt = (|| -> Result<Resp, String> {
            let sn = ServerName::try_from(host.clone()).map_err(|e| e.to_string())?;
            let mut conn = rustls::ClientConnection::new(cc.clone(), sn).map_err(|e| e.to_string())?;
            let mut tcp = TcpStream::connect((host.as_str(), port)).map_err(|e| format!("connect {h}: {e}"))?;
            tcp.set_read_timeout(Some(std::time::Duration::from_secs(20))).map_err(|e| e.to_string())?;
            tcp.set_nodelay(true).map_err(|e| e.to_string())?;
            let mut stream = rustls::Stream::new(&mut conn, &mut tcp);
            http_request(&mut stream, method, &format!("{host}:{port}"), path, body)
        })();
        match attempt {
            Ok(r) => return Ok(r),
            Err(e) => last = e,
        }
    }
    Err(format!("all hubs failed: {last}"))
}

fn need(r: &Resp) -> Result<serde_json::Value, String> {
    if r.status == 200 {
        r.json::<serde_json::Value>()
    } else {
        Err(format!("{} (http {})", r.msg(), r.status))
    }
}

fn nodes_map(c: &Cfg) -> Result<Vec<Node>, String> {
    let v = need(&call(c, true, "GET", "/nodes", None)?)?;
    serde_json::from_value(v["nodes"].clone()).map_err(|e| e.to_string())
}

fn self_name(c: &Cfg, nodes: &[Node]) -> Result<String, String> {
    let fp = cert_fp_pem(&resolve(&c.cert).to_string_lossy())?;
    nodes.iter().find(|n| n.fp == fp).map(|n| n.name.clone()).ok_or("this identity is not registered".into())
}

fn age_of(nodes: &[Node], name: &str) -> Result<String, String> {
    nodes.iter().find(|n| n.name == name).map(|n| n.agepub.clone()).ok_or(format!("unknown node: {name}"))
}

fn encrypt_for(c: &Cfg, plain: &[u8], to: &[String], nodes: &[Node]) -> Result<String, String> {
    let me = self_name(c, nodes)?;
    let mut set: Vec<String> = vec![me];
    for t in to {
        if !set.contains(t) {
            set.push(t.clone());
        }
    }
    let keys: Vec<String> = set.iter().map(|n| age_of(nodes, n)).collect::<Result<_, _>>()?;
    age_encrypt(plain, &keys)
}

fn chmod600(p: &Path) {
    let _ = std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o600));
}

fn arg(args: &[String], flag: &str) -> Option<String> {
    args.iter().position(|a| a == flag).and_then(|i| args.get(i + 1)).cloned()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let cmd = args.get(1).map(|s| s.as_str()).unwrap_or("help");
    let rest: &[String] = if args.len() > 2 { &args[2..] } else { &[] };
    let r = match cmd {
        "enroll" => cmd_enroll(rest),
        "nodes" | "ls" | "healthz" | "nodesjson" | "lsjson" => load_cfg().and_then(|c| match cmd {
            "nodes" => {
                let v = need(&call(&c, true, "GET", "/nodes", None)?)?;
                let nodes: Vec<Node> = serde_json::from_value(v["nodes"].clone()).unwrap();
                println!("{:<12} {:<14} {:<14} {:<14} last", "name", "identity:12", "agepub:12", "roles");
                for n in nodes {
                    println!("{:<12} {:<14} {:<14} {:?}  {}s-ago", n.name, &n.fp[..12], &n.agepub[..12.min(n.agepub.len())], n.roles, meshhub::now().saturating_sub(n.last));
                }
                Ok(())
            }
            "nodesjson" => {
                let v = need(&call(&c, true, "GET", "/nodes", None)?)?;
                println!("{v}");
                Ok(())
            }
            "lsjson" => {
                let v = need(&call(&c, true, "GET", "/secrets", None)?)?;
                println!("{v}");
                Ok(())
            }
            "ls" => {
                let v = need(&call(&c, true, "GET", "/secrets", None)?)?;
                println!("{:<14} {:<6} {:>7} {:<14} recipients", "name", "kind", "size", "owner:12");
                if let Some(arr) = v["secrets"].as_array() {
                    for s in arr {
                        println!(
                            "{:<14} {:<6} {:>7} {:<14} {}",
                            s["name"].as_str().unwrap_or("?"),
                            s["kind"].as_str().unwrap_or("?"),
                            s["size"].as_u64().unwrap_or(0),
                            &s["owner"].as_str().unwrap_or("?")[..12],
                            s["recipients"].as_array().map(|a| a.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join(",")).unwrap_or_default()
                        );
                    }
                }
                Ok(())
            }
            _ => {
                let v = need(&call(&c, false, "GET", "/healthz", None)?)?;
                println!("{v}");
                Ok(())
            }
        }),
        "get" => cmd_get(rest),
        "put" => cmd_put(rest),
        "share" => cmd_share(rest),
        "rm" => load_cfg().and_then(|c| {
            let name = rest.first().ok_or("usage: meshctl rm NAME")?.clone();
            need(&call(&c, true, "DELETE", &format!("/secrets/{name}"), None)?)?;
            println!("deleted {name}");
            Ok(())
        }),
        "unenroll" => load_cfg().and_then(|c| {
            // Self-unenroll: the hub removes THIS identity (refuses if it
            // still owns secrets). Local key files stay: the node can
            // re-enroll later with a fresh mint, same age identity.
            let v = need(&call(&c, true, "DELETE", "/nodes", None)?)?;
            println!("unenrolled {}", v["unenrolled"].as_str().unwrap_or("?"));
            Ok(())
        }),
        _ => {
            println!(
                "meshctl v{} -- mesh hub client (hub never sees plaintext)\n\
                 commands: enroll --name N --token T --ca-url URL --root ROOT --root-ca FILE\n\
                 \t\t nodes | ls | healthz\n\
                 \t\t put NAME [--to a,b] [--from FILE|-] [--blob]\n\
                 \t\t get NAME [--out FILE]\n\
                 \t\t share NAME --to a,b   (owner; re-encrypts locally)\n\
                 \t\t rm NAME   |   unenroll (self, from the registry)",
                meshhub::VERSION
            );
            Ok(())
        }
    };
    if let Err(e) = r {
        eprintln!("meshctl: {e}");
        std::process::exit(1);
    }
}

fn cmd_enroll(args: &[String]) -> Result<(), String> {
    let name = arg(args, "--name").ok_or("usage: meshctl enroll --name N [--mint-via meshops@pihole.local --ssh-key KEY | --cert-file C --key-file K | --token T --ca-url URL --root ROOT] --root-ca FILE")?;
    if !meshhub::valid_name(&name) {
        return Err("bad node name (lowercase, digits, . - _)".into());
    }
    let rootca_local = arg(args, "--root-ca").ok_or("--root-ca required (path to mesh root_ca.crt)")?;
    let hubs = arg(args, "--hub").map(|h| h.split(',').map(|s| s.to_string()).collect()).unwrap_or_else(|| vec!["pihole.home.lan:9443".into(), "pihole2.home.lan:9443".into()]);
    let roles: Vec<String> = arg(args, "--roles").map(|r| r.split(',').map(|s| s.to_string()).collect()).unwrap_or_default();

    let dir = meshdir();
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;

    let idpath = dir.join(format!("identity-{name}.key"));
    // The age identity is STABLE across re-enrolls: grants key to the
    // pubkey, so rotating the cert leaf must never rotate the key.
    let (idstr, pubstr) = match std::fs::read_to_string(&idpath) {
        Ok(existing) => {
            let id: age::x25519::Identity =
                existing.trim().parse().map_err(|e| format!("stored identity corrupt: {e}"))?;
            (existing.trim().to_string(), id.to_public().to_string())
        }
        Err(_) => {
            let pair = gen_identity();
            std::fs::write(&idpath, format!("{}\n", pair.0)).map_err(|e| e.to_string())?;
            pair
        }
    };
    chmod600(&idpath);

    let certp = dir.join("cert.crt");
    let keyp = dir.join("cert.key");
    let not_after = arg(args, "--not-after").unwrap_or_else(|| "24h".into());

    if let Some(host) = arg(args, "--mint-via") {
        // Admin-mint lane: a pi-root-capable operator signs a fresh leaf
        // off the mesh root (key never unloads beyond root's own
        // decrypt); the bundle rides the ssh channel, temp files die.
        let ssh_key = arg(args, "--ssh-key").unwrap_or_default();
        let tag = meshhub::rand_hex(4);
        let cmd = format!(
            "sudo step certificate create {name} /tmp/me-{tag}.crt /tmp/me-{tag}.key --profile leaf --ca /etc/step-ca/certs/root_ca.crt --ca-key /etc/step-ca/secrets/root_ca_key --ca-password-file /etc/step-ca/secrets/password --san {name} --not-before 0m --not-after {not_after} --no-password --insecure -f >/dev/null 2>&1 && sudo sh -c 'cat /tmp/me-{tag}.crt; echo --MESHSEP--; cat /tmp/me-{tag}.key'; sudo rm -f /tmp/me-{tag}.crt /tmp/me-{tag}.key"
        );
        let out = std::process::Command::new("ssh")
            .args(["-i", &ssh_key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new", &host, &cmd])
            .output()
            .map_err(|e| format!("ssh {host}: {e}"))?;
        let so = String::from_utf8_lossy(&out.stdout).to_string();
        let (crt, key) = so
            .split_once("--MESHSEP--")
            .ok_or_else(|| format!("mint failed: {}", String::from_utf8_lossy(&out.stderr).lines().next().unwrap_or("")))?;
        std::fs::write(&certp, crt.trim().as_bytes()).map_err(|e| e.to_string())?;
        std::fs::write(&keyp, key.trim().as_bytes()).map_err(|e| e.to_string())?;
    } else if let (Some(cf), Some(kf)) = (arg(args, "--cert-file"), arg(args, "--key-file")) {
        std::fs::copy(&cf, &certp).map_err(|e| format!("cert: {e}"))?;
        std::fs::copy(&kf, &keyp).map_err(|e| format!("key: {e}"))?;
        let _ = std::fs::remove_file(&cf);
        let _ = std::fs::remove_file(&kf);
    } else {
        // legacy JWK-token lane (needs a wrapped provisioner key; parked)
        let token = arg(args, "--token").ok_or("one of --mint-via / --cert-file+--key-file / --token")?;
        let ca_url = arg(args, "--ca-url").ok_or("--ca-url required")?;
        let root = arg(args, "--root").ok_or("--root required")?;
        let st = std::process::Command::new("step")
            .args([
                "ca", "certificate", &name,
                certp.to_str().unwrap(), keyp.to_str().unwrap(),
                "--ca-url", &ca_url, "--root", &root, "--token", &token,
                "--not-after", &not_after,
            ])
            .status()
            .map_err(|e| format!("run step: {e} (step CLI required)"))?;
        if !st.success() {
            return Err("step ca certificate failed".into());
        }
    }
    chmod600(&keyp);
    let root_dest = dir.join("root_ca.crt");
    std::fs::copy(&rootca_local, &root_dest).map_err(|e| format!("root-ca: {e}"))?;

    let cfg = Cfg {
        hub: hubs.clone(),
        root_ca: "root_ca.crt".into(),
        cert: "cert.crt".into(),
        key: "cert.key".into(),
        identity: format!("identity-{name}.key"),
    };
    std::fs::write(dir.join("config.json"), serde_json::to_vec_pretty(&cfg).unwrap()).map_err(|e| e.to_string())?;
    chmod600(&dir.join("config.json"));

    let body = serde_json::to_vec(&serde_json::json!({ "name": name, "agepub": pubstr, "roles": roles })).unwrap();
    // POST /enroll over the fresh cert
    let cc = client_cfg(&cfg, true)?;
    let (host, port) = match hubs[0].rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse().unwrap_or(9443)),
        None => (hubs[0].clone(), 9443),
    };
    let sn = ServerName::try_from(host.clone()).map_err(|e| e.to_string())?;
    let mut conn = rustls::ClientConnection::new(cc, sn).map_err(|e| e.to_string())?;
    let mut tcp = TcpStream::connect((host.as_str(), port)).map_err(|e| format!("hub unreachable at {}:{port}: {e} -- is meshhub running?", hubs[0]))?;
    tcp.set_read_timeout(Some(std::time::Duration::from_secs(15))).ok();
    let mut stream = rustls::Stream::new(&mut conn, &mut tcp);
    let resp = http_request(&mut stream, "POST", &format!("{host}:{port}"), "/enroll", Some(&body))?;
    need(&resp)?;
    let fp = cert_fp_pem(&certp.to_string_lossy())?;
    println!("enrolled {name}\n  age pub: {pubstr}\n  cert fp: {}", &fp[..16]);
    Ok(())
}

fn cmd_get(args: &[String]) -> Result<(), String> {
    let c = load_cfg()?;
    let name = args.first().ok_or("usage: meshctl get NAME [--out FILE]")?.clone();
    let v = need(&call(&c, true, "GET", &format!("/secrets/{name}"), None)?)?;
    let armor = v["data"].as_str().ok_or("no data")?.to_string();
    let id = std::fs::read_to_string(resolve(&c.identity)).map_err(|e| e.to_string())?;
    let plain = age_decrypt(&armor, &id)?;
    if let Some(out) = arg(args, "--out") {
        std::fs::write(&out, &plain).map_err(|e| e.to_string())?;
        chmod600(Path::new(&out));
        eprintln!("wrote {} bytes to {out} (0600)", plain.len());
    } else {
        std::io::stdout().write_all(&plain).map_err(|e| e.to_string())?;
    }
    Ok(())
}

fn read_input(args: &[String]) -> Result<Vec<u8>, String> {
    match arg(args, "--from") {
        Some(p) if p == "-" => {
            let mut b = Vec::new();
            std::io::stdin().read_to_end(&mut b).map_err(|e| e.to_string())?;
            Ok(b)
        }
        Some(p) => std::fs::read(&p).map_err(|e| format!("read {p}: {e}")),
        None => Err("secret material from --from FILE or --from - (stdin); never argv".into()),
    }
}

fn do_put(c: &Cfg, name: &str, plain: &[u8], to: &[String], kind: &str) -> Result<(), String> {
    let nodes = nodes_map(c)?;
    let armor = encrypt_for(c, plain, to, &nodes)?;
    let body = serde_json::to_vec(&serde_json::json!({ "recipients": to, "kind": kind, "data": armor })).unwrap();
    let v = need(&call(c, true, "PUT", &format!("/secrets/{name}"), Some(&body))?)?;
    println!("stored {name}: {} bytes ciphertext, recipients {}, kind {kind}", armor.len(), to.join(","));
    let _ = v;
    Ok(())
}

fn cmd_put(args: &[String]) -> Result<(), String> {
    let c = load_cfg()?;
    let name = args.first().ok_or("usage: meshctl put NAME [--to a,b] [--from FILE|-] [--blob]")?.clone();
    let to: Vec<String> = arg(args, "--to").map(|t| t.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect()).unwrap_or_default();
    let kind = if args.iter().any(|a| a == "--blob") { "blob" } else { "secret" };
    let plain = read_input(args)?;
    do_put(&c, &name, &plain, &to, kind)
}

fn cmd_share(args: &[String]) -> Result<(), String> {
    // owner-only re-designation: read (as owner) -> decrypt locally ->
    // re-encrypt to the new set -> PUT. The hub never widens its view.
    let c = load_cfg()?;
    let name = args.first().ok_or("usage: meshctl share NAME --to a,b")?.clone();
    let to: Vec<String> = arg(args, "--to").ok_or("--to required")?.split(',').map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();
    let v = need(&call(&c, true, "GET", &format!("/secrets/{name}"), None)?)?;
    let armor = v["data"].as_str().ok_or("no data")?.to_string();
    let id = std::fs::read_to_string(resolve(&c.identity)).map_err(|e| e.to_string())?;
    let plain = age_decrypt(&armor, &id)?;
    let kind = v["record"]["kind"].as_str().unwrap_or("secret");
    do_put(&c, &name, &plain, &to, kind)
}
