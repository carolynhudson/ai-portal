// meshhub shared library: identity, crypto, wire, records.
// The hub stores ONLY age ciphertext; encryption/decryption happen in
// meshctl on the client. ACL lives in the recipient list (age
// multi-recipient: one ciphertext, N grants, no per-ACL copies).
use std::io::{BufRead, BufReader, Read, Write};
use std::time::{SystemTime, UNIX_EPOCH};

use rustls::pki_types::{CertificateDer, PrivateKeyDer};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const VERSION: &str = "0.1.0";

// ---------- records ----------

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Node {
    pub name: String,
    pub agepub: String,
    #[serde(default)]
    pub roles: Vec<String>,
    pub added: u64,
    pub last: u64,
    pub fp: String, // sha256 hex of cert DER = identity
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Rec {
    pub id: String,
    pub name: String,
    pub owner: String, // cert fp of the owner
    pub recipients: Vec<String>, // node NAMES granted read
    pub kind: String, // "secret" | "blob"
    pub size: u64, // ciphertext bytes
    pub sha: String, // sha256 hex of ciphertext
    pub created: u64,
    pub updated: u64,
}

pub fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

pub fn rand_hex(nbytes: usize) -> String {
    use rand::RngCore;
    let mut v = vec![0u8; nbytes];
    rand::rngs::OsRng.fill_bytes(&mut v);
    v.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn valid_name(n: &str) -> bool {
    !n.is_empty()
        && n.len() <= 64
        && n.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '-' || c == '_')
}

// ---------- certs ----------

pub fn load_certs(path: &str) -> Result<Vec<CertificateDer<'static>>, String> {
    let f = std::fs::File::open(path).map_err(|e| format!("open {path}: {e}"))?;
    let mut br = BufReader::new(f);
    let certs: Vec<CertificateDer> =
        rustls_pemfile::certs(&mut br).collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())?;
    if certs.is_empty() {
        return Err(format!("no certificates in {path}"));
    }
    Ok(certs)
}

pub fn load_key(path: &str) -> Result<PrivateKeyDer<'static>, String> {
    let f = std::fs::File::open(path).map_err(|e| format!("open {path}: {e}"))?;
    let mut br = BufReader::new(f);
    // Scan PEM items for the first key (works with FTL's KEY-first tls.pem).
    loop {
        match rustls_pemfile::read_one(&mut br).map_err(|e| e.to_string())? {
            Some(rustls_pemfile::Item::Pkcs8Key(k)) => return Ok(PrivateKeyDer::Pkcs8(k)),
            Some(rustls_pemfile::Item::Pkcs1Key(k)) => return Ok(PrivateKeyDer::Pkcs1(k)),
            Some(rustls_pemfile::Item::Sec1Key(k)) => return Ok(PrivateKeyDer::Sec1(k)),
            None => return Err(format!("no private key in {path}")),
            Some(_) => continue,
        }
    }
}

// ---------- age (client-side only) ----------

pub fn age_encrypt(plaintext: &[u8], recipients_b32: &[String]) -> Result<String, String> {
    use age::armor::{ArmoredWriter, Format};
    let mut recips: Vec<age::x25519::Recipient> = Vec::new();
    for r in recipients_b32 {
        recips.push(r.parse().map_err(|e| format!("bad recipient {r}: {e}"))?);
    }
    if recips.is_empty() {
        return Err("no recipients".into());
    }
    let enc = age::Encryptor::with_recipients(recips.iter().map(|r| r as &dyn age::Recipient))
        .map_err(|e| e.to_string())?;
    let mut out: Vec<u8> = Vec::new();
    {
        let aw = ArmoredWriter::wrap_output(&mut out, Format::AsciiArmor).map_err(|e| e.to_string())?;
        let mut w = enc.wrap_output(aw).map_err(|e| e.to_string())?;
        w.write_all(plaintext).map_err(|e| e.to_string())?;
        w.finish().map_err(|e| e.to_string())?.finish().map_err(|e| e.to_string())?;
    }
    String::from_utf8(out).map_err(|e| e.to_string())
}

pub fn age_decrypt(armored: &str, identity_str: &str) -> Result<Vec<u8>, String> {
    let id: age::x25519::Identity = identity_str.trim().parse().map_err(|e| format!("identity: {e}"))?;
    let dec = age::Decryptor::new(age::armor::ArmoredReader::new(armored.as_bytes()))
        .map_err(|e| format!("parse: {e}"))?;
    let mut r = dec
        .decrypt(std::iter::once(&id as &dyn age::Identity))
        .map_err(|e| format!("decrypt: {e}"))?;
    let mut out = Vec::new();
    r.read_to_end(&mut out).map_err(|e| e.to_string())?;
    Ok(out)
}

pub fn gen_identity() -> (String, String) {
    use age::secrecy::ExposeSecret;
    let id = age::x25519::Identity::generate();
    (id.to_string().expose_secret().to_string(), id.to_public().to_string())
}

pub fn is_age_armor(s: &str) -> bool {
    s.trim_start().starts_with("-----BEGIN AGE ENCRYPTED FILE-----")
}

pub fn cert_fp_pem(path: &str) -> Result<String, String> {
    let certs = load_certs(path)?;
    Ok(sha256_hex(certs[0].as_ref()))
}

// ---------- minimal HTTP/1.1 over rustls::Stream ----------

pub struct Resp {
    pub status: u16,
    pub body: Vec<u8>,
}

impl Resp {
    pub fn json<T: serde::de::DeserializeOwned>(&self) -> Result<T, String> {
        serde_json::from_slice(&self.body).map_err(|e| format!("json: {e}"))
    }
    pub fn msg(&self) -> String {
        #[derive(Deserialize)]
        struct E {
            #[serde(default)]
            error: String,
        }
        serde_json::from_slice::<E>(&self.body).map(|e| e.error).unwrap_or_else(|_| format!("http {}", self.status))
    }
}

pub fn http_request<S: Read + Write>(
    s: &mut S,
    method: &str,
    host: &str,
    path: &str,
    body: Option<&[u8]>,
) -> Result<Resp, String> {
    let mut req = format!(
        "{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\nContent-Type: application/json\r\n"
    );
    if let Some(b) = body {
        req.push_str(&format!("Content-Length: {}\r\n", b.len()));
    }
    req.push_str("\r\n");
    s.write_all(req.as_bytes()).map_err(|e| format!("write: {e}"))?;
    if let Some(b) = body {
        s.write_all(b).map_err(|e| format!("write: {e}"))?;
    }
    let mut br = BufReader::new(s);
    let mut line = String::new();
    br.read_line(&mut line).map_err(|e| format!("read: {e}"))?;
    let status = line.split_whitespace().nth(1).and_then(|c| c.parse().ok()).unwrap_or(0);
    let mut clen: usize = 0;
    loop {
        let mut h = String::new();
        let n = br.read_line(&mut h).map_err(|e| format!("read: {e}"))?;
        if n == 0 || h.starts_with("\r\n") {
            break;
        }
        if let Some(v) = h.split_once(':') {
            if v.0.trim().eq_ignore_ascii_case("content-length") {
                clen = v.1.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut body_v = vec![0u8; clen];
    br.read_exact(&mut body_v).map_err(|e| format!("body: {e}"))?;
    Ok(Resp { status, body: body_v })
}

// server-side request read
pub struct ReqLine {
    pub method: String,
    pub path: String,
    pub body: Vec<u8>,
}

pub fn http_read<S: Read + Write>(s: &mut S) -> std::io::Result<Option<ReqLine>> {
    let mut br = BufReader::new(s);
    let mut line = String::new();
    let n = br.read_line(&mut line)?;
    if n == 0 {
        return Ok(None);
    }
    let mut it = line.split_whitespace();
    let method = it.next().unwrap_or("").to_string();
    let path = it.next().unwrap_or("").to_string();
    let mut clen: usize = 0;
    loop {
        let mut h = String::new();
        let n = br.read_line(&mut h)?;
        if n == 0 || h.starts_with("\r\n") {
            break;
        }
        if let Some((k, v)) = h.split_once(':') {
            if k.trim().eq_ignore_ascii_case("content-length") {
                clen = v.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut body = vec![0u8; clen];
    br.read_exact(&mut body)?;
    Ok(Some(ReqLine { method, path, body }))
}

pub fn http_write<S: Write>(s: &mut S, status: u16, body: &[u8]) -> std::io::Result<()> {
    let head = format!(
        "HTTP/1.1 {status} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        if status == 200 { "OK" } else { "ERR" },
        body.len()
    );
    s.write_all(head.as_bytes())?;
    s.write_all(body)?;
    s.flush()
}

pub fn j(v: &impl Serialize) -> Vec<u8> {
    serde_json::to_vec(v).unwrap_or_else(|_| b"{}".to_vec())
}

pub fn err(_status: u16, m: &str) -> Vec<u8> {
    j(&serde_json::json!({ "error": m }))
}
