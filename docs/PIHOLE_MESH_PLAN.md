# Pi-hole pair — findings, HA research, mesh plan (2026-11-13)

Owner side quest, safety law in force: these are production DNS/DHCP
servers (partner works from home); read-only first, pihole2 before
pihole.local, mesh services purely additive, permission before any
availability-touching step. This doc = the pihole2 read pass + research +
the change list awaiting owner approval.

## 1. pihole2.local — measured inventory (read-only, 2026-11-13)

- **Raspberry Pi 4 Model B, 8 GB** (owner note: the pair was expected to
  be Pi 5s -- pihole2 is a Pi 4; pihole.local model to verify in its own
  read pass), Debian 13 trixie aarch64, kernel 6.18, 235 GB SD (220 G
  free), uptime 19 d, load 0.08, RAM ~7.4 Gi available. Headroom:
  enormous for Headscale + step-ca + relay (all SQLite, tiny writes).
- **Pi-hole v6.5 core / web 6.6.1 / FTL 6.8** (upstream has v6.6 --
  updates are a permission-gated decision, not a reflex).
- **Key discovery: v6 serves its own console.** pihole-FTL listens on
  80 AND 443; lighttpd is absent. `/etc/pihole/tls_ca.crt/tls.crt/tls.pem`
  exist (self-signed, pihole-owned, mode 600). So "HTTPS on the console"
  = REPLACE THE CERT PAIR with CA-issued certs (SANs: pi.hole +
  pihole2.local + pihole.local VIP name), FTL reload, done. Rollback =
  restore the self-signed pair (back it up first). The v6 console is
  HTTPS-CAPABLE TODAY; today it is the warning-fatigue kind of HTTPS.
- **unbound.service is running** (DNSSEC-validating local resolver) --
  good upstream hygiene already present.
- DHCP: 27 active leases; DHCP config lives in pihole.toml (v6 model),
  not dnsmasq.d as in v5. `webserver.domain = "pi.hole"`.
- meshops access verified: ssh key auth + NOPASSWD sudo, root confirmed;
  the root-owned /tmp/meshops.sudo left from setup was removed (the one
  pre-approved mutation of this pass).

## 2. HA research: the real gaps and the safe fixes

Measured reality (owner's warning confirmed): no settings sync, no
gravity sync, DHCP lease tables independent, no failover mechanic.

**DNS (stateless -- the easy, safe win).** Keepalived VIP: a floating
address (e.g. 192.168.1.13) that migrates between the Pis; both answer
DNS simultaneously today, so a floating VIP adds clean failover with zero
DNS software change. Clients (via DHCP option 6) learn the VIP as their
DNS; a dead Pi moves the VIP in ~seconds. This is the keystone: it makes
"one Pi dies" a non-event for browsing, which is exactly the guarantee
the CA needs to sit on this pair.

**DHCP (stateful -- three options, honest trade):**
a) **Consolidate**: primary serves DHCP (static reservations for the
   important devices: printer, pis, NAS; long lease times), secondary's
   DHCP OFF; secondary takes over manually (web UI toggle) on primary
   loss. Simple, predictable, matches home scale.
b) **dnsmasq/FTL failover peer**: FTL embeds dnsmasq, which speaks the
   DHCP failover protocol to a peer (shared lease database). True
   automatic failover, least well-trodden with Pi-hole v6 -- test on
   pihole2 first, with the web UI toggle as the fallback path.
c) **Status quo**: two independent DHCP servers -- the current IP-
   collision risk on lease shuffles; the thing we're fixing.
Recommendation: (a) now, evaluate (b) in the pihole2 sandbox; never
run (c) once the CA lives here.

**Config + gravity sync.** A small, dumb cron on the secondary: pull
`pihole.toml`, `gravity.db`, adlists from the primary (rsync over the
mesh ssh key), snapshot before overwrite (pi-hole's config_backups +
gravity_backups dirs already exist -- native safety). The web API has
teleporter exports; filesystem rsync is the predictable path. Note:
pihole.toml contains secrets (API/password hashes) -- the sync channel
must be the ssh'd pair, never an open port.

## 3. Mesh additions plan (purely additive, owner approval required)

Phase M0 (read): pihole.local inventory (same read-only pass; requested).
Phase M1 (HA first, per "fix the foundation, then the crown"): keepalived
VIP + DHCP consolidation to primary + gravity/config cron. Acceptance:
DNS answers on both AND via VIP; lease table intact (>=27 entries);
console loads on both; partner's workday unaffected; failover drill:
drain VIP (systemctl stop keepalived on one, secondary takes it in <5 s,
dig via VIP succeeds).
Phase M2 (the crown): step-ca on pihole.local (primary signer) + key
replica on pihole2 (hot standby; takeover = signing continues); ACME
endpoint for Caddy; SSH CA issuance (host + user certs, 24 h). Acceptance
CA suite (the owner's "validated as good", demonstrated not trusted):
issue cert -> replace pihole2's self-signed tls pair (backup kept) ->
root installed on halo/forge/Mac -> padlock from all three machines ->
renewal fires -> rollback rehearsed.
Phase M3 (fabric): Headscale (tailnet control) on the pair + mesh relay
(store-and-forward, classed retention). Acceptance: halo+forge join,
café-roaming simulation (client off-LAN via tailnet), one Pi drained ->
tailnet survives on the other.
Phase M4 (retire the stopgap): meshops -> CA-issued identities; static
tokens die (forge ONE-token migration); secrets broker P0/P1 built on
the CA (AI_MESH_PROPOSAL 2.2 + the NODE_CONTRACT secured pipe).

## 4. Standing cautions

Pi-hole updates (v6.6) are a separate permission-gated decision.
SD-card wear: all new services are low-write SQLite; relay retention is
classed (telemetry drop-oldest) to cap writes. No new listening ports
beyond 443/53/853-tbd + tailscale; ACME and API over the pair's ssh/LAN
only. Reboot windows (keepalived/step installs) need owner timing --
partner's workday respected; the 19-day uptime says reboots are rare
here, so each one is a scheduled event, not a surprise.

## 5. pihole.local read pass (owner-approved, 2026-11-13) -- roles inverted

**The primary is the stronger box AND the wear-safe one**: Raspberry Pi 5
(4 GB), Debian 13 trixie, root on **NVMe** (Patriot P300 512 GB, ext4
noatime, 445 G free), zram 2 G swap, journal 8 MB, load 0.00, 19 d
uptime. pihole2 is the Pi 4 8 GB on micro SD (the wear-sensitive box).
Same software stack (v6.5/6.6.1/6.8, FTL self-TLS on 80+443, unbound).
**Drift confirmed with numbers: 18 DHCP leases on the primary vs 27 on
the secondary -- the two lease databases are diverged, the IP-collision
risk is real, M1 matters.**

**SD wear discipline for pihole2 (the Pi 4/SD box):**
- Write-heavy mesh state (relay store-and-forward, CA DB) lives on the
  PRIMARY (NVMe); pihole2 holds the key replica + cold standby, not the
  busy SQLite.
- pihole2: add `noatime` to the root mount (fstab one-liner, effective
  at the scheduled reboot), cap the journal (`SystemMaxUse=50M`), and
  tune FTL query-DB retention (privacy + writes: pihole-FTL.db is the
  biggest steady writer, 624 MB on the primary).
- Gravity pushes (230 MB pulls on sync) ride the nightly cron, snapshot
  rotation kept small -- SD-friendly cadence, not per-hour.

## 7. DNS/IPv6 deep findings (2026-11-13, live wire capture + dig tests)

The owner's "rush job" story, verified on the wire:

- **IPv4 side WON**: the cable router's DHCP hands out **192.168.1.11
  (pihole.local) as DNS** (proved: router answered a query FOR
  pihole.local by name, resolving it to .11). FTL on .11 answers LAN
  UDP+TCP DNS fine. `dns.listeningMode=LOCAL` means localhost+LAN
  (not localhost-only) -- no change needed.
- **IPv6 side NOT won**: passive RA capture (RS-solicited, 30 s) shows
  **only the router** sends RAs, advertising **its own IPv6 as RDNSS**
  (link-local-derived addrs) = the ISP DNS override, alive on IPv6.
  FTL sent ZERO RAs despite dhcp.ipv6=true: the IPv6 takeover never
  engaged. Clients therefore run Pi-hole (IPv4) and ISP DNS (IPv6)
  in parallel, and modern resolvers use the IPv6 server first.
- **pihole2 UDP DNS: HEALTHY (false alarm retracted same day).** The
  "dead UDP" reading came from testing on-box via ssh (localhost /
  self-IP UDP times out = loopback quirk). From halo (real LAN
  client) UDP to .12 answers 8.8.4.4. No firewall rules, no UDF
  module on FTL 6.8, no bind conflicts: unbound sits at
  127.0.0.1:5335 (v6 standard layout), FTL owns :53 on both Pis,
  zero bind complaints in 14 d of journal.
- **FTL upstreams are raw 8.8.8.8/8.8.4.4** on both Pis; unbound
  (DNSSEC-validating) runs but is NOT in the FTL upstream chain.
- **Replication archaeology**: native config_backups (3 deep) +
  gravity_backups (weekly, Nov 8 freshest) exist on both Pis; NO
  cross-Pi sync tool/cron exists (owner memory of "backup and
  restore" = the native backups). Static-lease files diverged:
  pi = 17 entries (fresh), pi2 = 27 (stale, expiry ~Oct 2025) with
  conflicting IPs for the same MACs.

Fix ladder (revised, owner-GO-gated, pihole2-first law):
D1 CANCELLED -- UDP DNS healthy on both Pis; false alarm retracted;
   monitor the on-box loopback quirk only.
D2 FTL IPv6 RA/RDNSS: announce FTL (stable fe80 source) so Pi-hole
   joins IPv6 DNS handout. Honest limit: the ISP router's RDNSS
   cannot be removed from the RA soup from our side; clients pick
   among advertised servers. Full defeat of the override needs a
   router-side change (bridge mode / own router) -- later option.
D3 FTL upstreams -> unbound loopback 127.0.0.1:5335 (DNSSEC, cache),
   both Pis one at a time (FTL restart = seconds-scale DNS blip).
D4 keepalived VIP 192.168.1.13 + DHCP option-6 -> VIP (staged,
   drill-gated, per M1).
D5 lease-file sync: primary's dhcp.leases (17 fresh rows) is the
   authority; nightly rsync to pihole2 replaces its 27 stale rows.
   Zero client impact (pi2 DHCP is inactive). Owner to provide the
   statics to KEEP (printer/NAS/partner gear) so pi's file gets
   cleaned of ancient rows before it becomes the synced master.

## 8. Hardware: DS3231 RTC for pihole2 (owner task, 2026-11-13)

Owner found a DS3231 I2C RTC module (bought 2, spare exists). Needs
vendor pinout matching to Pi 4 GPIO (SDA/SCL/3V3/GND), physical adapter
+ case inside the Pi 4 enclosure, CR2032/LiR2032 battery (not included
in kit). No schedule pressure. Software when hardware lands:
`dtoverlay=i2c-rtc,ds3231` in /boot/firmware/config.txt + reboot in a
scheduled window + `hwclock -w` seed + cold-boot verify; rollback =
delete overlay line. Until then pihole2 rides the systemd clock-state
file (already proven working). Low-write law: RTC sync at boot/shutdown
only, not periodic.


1. **keepalived VIP 192.168.1.13** on both Pis (apt install; drop-in
   configs; primary priority 100 pre-empt, secondary 90). Acceptance:
   VIP answers DNS; drill: `systemctl stop keepalived` on primary ->
   VIP migrates <5 s, `dig` via VIP succeeds; rollback = disable the
   service (one command).
2. **DHCP consolidation**: primary's DHCP stays authoritative; pihole2's
   FTL DHCP switched off (`pihole-FTL --config dhcp.active false`);
   static reservations for critical devices on the primary (OWNER: give
   me the device list -- printer, NAS, Pis, work gear). Secondary
   takeover path documented (web UI toggle). Acceptance: leases continue,
   renewal works, no collisions.
3. **DNS handout to the VIP** (DHCP option 6 -> 192.168.1.13) staged
   AFTER the VIP drill passes, at lease-renewal cadence (no forced
   renew; clients pick it up gracefully).
4. **Nightly sync cron on pihole2**: rsync gravity.db + pihole.toml +
   adlists from the primary over the mesh ssh key; snapshot-first into
   the v6 config_backups/gravity_backups; retention small.
5. **SD care on pihole2**: noatime in fstab (applies at the next
   scheduled reboot), journal cap 50 MB, FTL query retention reduced.
6. **NTP pinning (time for the CA)**: measured 2026-11-13 -- clock health
   is excellent (jitter 3.3 ms, dispersion 1.2 ms, stratum 2); journal
   noise = occasional IPv6 pool-server timeouts, self-healing, ~1/19 d.
   Owner-reported time errors = that noise, normal. Pin
   systemd-timesyncd to working IPv4 servers (time.cloudflare.com,
   1.debian.pool.ntp.org, time.nist.gov -- all measured OK from the Pis;
   Google's NTP endpoint times out both families and is NOT the problem)
   via /etc/systemd/timesyncd.conf.d/ drop-in + restart timesyncd. Hot,
   one-file rollback.
7. **Pi 5 RTC enable (pihole.local)**: battery installed, RTC disabled by
   firmware default (measured: /dev/rtc0 exists, no rx6110 probe, no
   rtc dtparam). Steps: backup config.txt; append `dtparam=rtc=on` under
   [all]; apt install util-linux-extra (hwclock was split out in Debian
   13); REBOOT (scheduled window); dmesg verify rx6110 probe;
   `sudo hwclock -w`; verify cold-boot date before NTP. Rollback =
   delete one line. Post-state: the CA primary starts every boot with
   true time, fully off the systemd clock-state file.
8. pihole2 (Pi 4): no RTC hardware (verified) -- adequate mitigation is
   already live (clock-state file + NTP pinning). If a spare I2C RTC
   (DS3231-class) surfaces, `dtoverlay=i2c-rtc,ds3231` is the one-liner;
   not required.
9. Reboot needs: #5 (noatime, pihole2) and #7 (RTC, pihole.local) each
   want one scheduled reboot; everything else is hot. Suggested
   window: one quiet evening per box, your call.

**CA time dependency (validated 2026-11-13):** cert validation is
clock arithmetic; the measured jitter (ms-scale) is far inside tolerance.
Cold-boot 1970 risk is mitigated -- systemd saves/restores the clock
state at shutdown/boot (pihole2 boot log proves it; boot->sync ~30-60 s).
Optional hardening for the CA primary (Pi 5, RTC header): official RTC
module (~$5) so cold boot starts with true time. Rule: the CA issues
only when `timedatectl` says synchronized; meshd's renewal loop checks
clock health before ACME calls, defers (not fails) on skew.
