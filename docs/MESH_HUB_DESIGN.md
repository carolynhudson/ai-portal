# MESH_HUB_DESIGN — the hub: registration + mirrored secrets broker (2026-11-14)

The service mesh nodes connect to, register with, and use as the
secrets store. Runs on the Pi pair (primary = pihole.local writer on
NVMe; pihole2 = read replica + cold standby). Trust root = the mesh
CA (step-ca); transport = mTLS; payloads = age-encrypted at rest and
in flight (the hub never holds plaintext secrets). Lineage:
NODE_CONTRACT "secrets through the secured pipe" + AI_MESH_PROPOSAL
2.2 + M3/M4 in PIHOLE_MESH_PLAN. Portal-side P0 (app/core/secrets.py)
consumes this service.

## The three owner asks, answered

1. **"Store would have to duplicate data per node able to access it"**
   -- NO. Payloads are encrypted with **age multi-recipient**
   (X25519): ONE ciphertext carries N recipient wraps (~100 B each).
   The store keeps exactly one copy per secret; the ACL is the
   recipient list. Changing an ACL = client re-encrypts to the new
   set (store is tiny, re-encrypt is milliseconds).

2. **"Clients can enumerate registered clients and designate who may
   access their secret"** -- YES: `GET /nodes` serves the registry
   (name, cert fingerprint, age pubkey, roles, last-seen) to any
   enrolled identity; `meshctl put --to a,b` designates recipients by
   name (resolved via /nodes). `meshctl ls` shows only what the
   caller can read (own + granted). ACL change = owner-only
   (owner = cert fingerprint recorded at put).

3. **"Guid id can no longer link directly to the encrypted data"** --
   CORRECT, so there is an indirection layer: **metadata index
   (index.json) sits between identity and ciphertext**. A record =
   {id (random), name, owner_fp, recipients, kind, size, sha256,
   created, updated}. The hub authorizes against metadata, then
   serves blobs/<id>.age. Leaking an id leaks nothing: blobs are age
   ciphertext, unreadable without a recipient identity key. Plaintext
   never touches the hub: encryption happens in meshctl on the
   client; the hub stores, mirrors, authorizes, and audits.

## Growth path (owner: future larger secret chunks/code)

- `kind` field: `secret` (small, inline string) | `blob` (bytes,
  file in, file out). Same API, same encryption, same ACL.
- Size cap is a hub config (default 32 MiB); blobs are plain files
  on the primary's NVMe -- the store stays small by design (owner
  law), and the mesh keeps big secrets OFF the nodes (NODE_CONTRACT:
  injected at launch, never on disk on the node).
- Content-addressing (sha256 of ciphertext) gives dedupe + integrity
  for free if a future version wants snapshots.

## Wire (JSON over mTLS, TCP 9443 on the pair -- port ledger entry)

| verb | meaning |
|---|---|
| POST /enroll | first contact registers identity (name must equal cert CN; declares age pubkey + roles) |
| GET /nodes | registry listing (any enrolled cert) |
| GET /secrets | records the CALLER can read (owner ∪ recipients) |
| PUT /secrets/<name> | store age-armored ciphertext + recipient list (caller becomes owner) |
| GET /secrets/<name> | ciphertext IF caller is owner or recipient; else 403 (audited) |
| DELETE /secrets/<name> | owner only |
| GET /healthz | liveness, no auth (no secret data) |

meshctl verbs: `enroll`, `nodes`, `ls`, `put`, `get`, `share --to`
(= get+re-encrypt+put, owner-only), `rm`. `share` client-side
re-encryption is deliberate: the hub never gains plaintext, so a
compromised hub cannot widen its own view -- the ACL lives in
cryptography, not just in hub config.

## Identity + enrollment

- Node identity = mesh-CA cert (step CA, JWK `mesh` provisioner,
  short-lived: 24 h, expiry = revocation; renew = re-issue) + an age
  keypair in ~/.mesh/identity-*.key (0600). Cert CN = node name;
  hub enforces CN == registry name; identity key = cert SHA256 fp.
- Enrollment is a two-party act (registration law): an operator with
  pi-root access mints a one-time token (`step ca token`, provisioner
  key on the pi); the node runs `meshctl enroll --token`. Silently
  connecting is not registration; SILENCE = DEPARTED applies to
  last-seen, not to identity.
- Nodes live in ~/.mesh/ (cert, key, root_ca.crt, hub hosts).

## Mirror + failover (pair law)

- Single writer: the primary. Every mutating op ends with
  `rsync -a /var/lib/meshhub/store/ root@pi2:/var/lib/meshhub/store/`
  over the existing Pi-to-Pi root key (id_ed25519_sync). Store ops
  are fsync + rename (atomic); index rewrites are temp+rename.
- pihole2: unit runs, serves READS from the replica; writes return
  503 unless /var/lib/meshhub/TAKEOVER exists (manual owner takeover
  -- no split-brain, same shape as the DHCP single-authority rule).
- meshctl failover: tries pihole.home.lan, then pihole2.home.lan.
- Nightly 03:15 sync (existing) gains /var/lib/meshhub as backstop +
  snapshot-first tar to /var/backups/meshhub/ (keep 7). SD wear:
  pi2's writes come only via replication (tiny), takeover is rare.

## TLS

Server cert = the existing per-box console pairs (/etc/pihole/tls.pem,
names pihole.home.lan / pihole2.home.lan -- the hub reads as root;
certbot renewal rotates it, hub reopens on restart; certbot renew
service gains a quiet `systemctl restart meshhub` tail hook).
Clients verify against the mesh root. Clients present node certs;
hub verifies against the mesh root and requires a registered
identity for everything except /healthz.

## Audit + privacy

journalctl only (no log files; SD care): event kind, identity name,
secret name, result. NEVER payload bytes, never ciphertext, never
tokens. Access grants/denials are auditable history; retention rides
journald caps.

## Boundary law (inherited, restated for the record)

- Bearer-token honesty: a secret is visible to the client that
  legitimately reads it -- the mesh arbitrates custody, not
  invisibility (NODE_CONTRACT). The partner scenario (MacBook-only
  keys invisible to owner) works: ACL grants that node ONLY; the
  hub (owner-operated) stores ciphertext it cannot read.
- Secrets never in git, config files, logs, or argv. meshctl takes
  secret material from stdin/file, never a flag.
- Hub is additive on production Pis: dedicated unit, port 9443, no
  FTL/DNS/CA mutation; install = announced, availability-neutral.

## P0 portal tie-in (next round)

app/core/secrets.py: portal becomes a meshctl-shaped client;
subagent_key.txt migrates INTO the hub (owner grants recipients =
forge+halo); confirm-gated git-push consumes hub-served PAT via
GIT_ASKPASS. Forge ONE-token law: every RainbowBlock surface fetches
its key from the hub at launch, nothing on disk.

## Files

- mesh/hub/  -- Rust crate: meshhub (daemon) + meshctl (CLI)
- mesh/hub/deploy/ -- systemd unit, install/replication scripts
- Port ledger: 9443 = meshhub (pair). 9000 = step-ca. 443 = FTL.
