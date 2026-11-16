# Forge mesh enrollment (RainbowBlock node)

Goal: the forge joins the mesh hub as node `forge`, so the ONE-token
law becomes real: the forge's surfaces fetch `subagent-key` from the
hub at launch (decrypted client-side), and the key file on the forge
is retired.

## Build meshctl.exe on the forge (forge agent task)

The crate lives in the portal repo at `mesh/hub/` (it is the repo;
nodes pull). On the forge, with the Rust toolchain the meshd relay
already uses:

    cd C:\forge\src\ai-portal\mesh\hub
    cargo build --release          # MSVC route; gnu route also proven

Produces `target\release\meshctl.exe` (no runtime deps).

## Mint the cert (owner/portal acts; on pihole.local as root)

Mint AT enrollment time (leaves are 24 h; expiry = revocation):

    step certificate create forge /tmp/f.crt /tmp/f.key --profile leaf \
      --ca /etc/step-ca/certs/root_ca.crt \
      --ca-key /etc/step-ca/secrets/root_ca_key \
      --ca-password-file /etc/step-ca/secrets/password \
      --san forge --not-before 0m --not-after 24h \
      --no-password --insecure -f

Copy `f.crt`, `f.key` and the mesh root (`/root/root_ca.crt`, also at
`docs/mesh-root_ca.crt`) to the forge (any private channel; the key
is the node's, the cert is CA-signed).

## Enroll on the forge

Run `mesh/hub/deploy/forge_enroll.ps1` from an elevated-enough shell
(plain user is fine) with the bundle in `C:\forge\etc\mesh\`:

    C:\forge\etc\mesh\forge_enroll.ps1

It calls meshctl.exe enroll --name forge --cert-file cert.crt
--key-file cert.key --root-ca root_ca.crt --roles forge.
Enrollment auto-generates the node's stable age keypair
(identity-forge.key) -- keep that file; it is the node's grant
identity.

## After enrollment (portal side)

    meshctl share subagent-key --to halo,forge   # owner action
Daily cert rotation: a mint+enroll round-trip (the portal's
reenroll automation covers halo + pihole2; forge rotation =
the forge agent mints via a fresh bundle from the owner, same
lane as enrollment).

## Verification on the forge

    meshctl.exe nodes            (registry enumeration)
    meshctl.exe get subagent-key (prints the key = grant works)

## Re-enroll (self-heal) -- portal automates the mint (2026-11-16)

The portal rotates a forge leaf EVERY NIGHT and stages it here (the
mesh-mint-forge systemd user timer, ~07:35 PST +/-15m; the pair-side CA
signs, the pi temps are purged, files land 0600):

    cortana@halo.local:~/ai/state/mesh/forge-bundle/cert.crt
    cortana@halo.local:~/ai/state/mesh/forge-bundle/cert.key

Self-heal at the forge's launch (and after any long sleep): if
`%USERPROFILE%\.mesh\cert.crt` has `notAfter` under ~6 h, pull the
bundle (ssh/scp as the same identity that clones the repo), copy over
`%USERPROFILE%\.mesh\cert.crt` + `cert.key` -- **keep
`identity-forge.key` untouched** (grants key to the age pubkey; the hub
migrates record ownership name -> new fingerprint). Verify:
`meshctl.exe nodes` and `meshctl.exe get subagent-key`. Do not print
the key. Staged leaf is short-lived (24 h) and carries no CA key
material; expiry = revocation, so a stale bundle is simply refused.
