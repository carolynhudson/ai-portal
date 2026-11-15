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

## 9. D3 execution log

pihole2 (2026-11-13, owner GO): FTL upstreams were ALREADY
["127.0.0.1#5335"]; now ["127.0.0.1#5335","::1#5335"]; dns.dnssec
false->true; FTL restarted (4 s active); verified: LAN UDP from halo
answers, console 302, backup /root/pihole.toml.pre-D3.2026-11-13.
Rollback: sudo pihole-FTL --config dns.upstreams ... original list +
dns.dnssec false + restart (file backup on box).
FINDING (pre-existing): unbound 1.26.2 validator module active
(modules: validator iterator), root.key RFC5011 anchor fresh
(last_success 2026-11-12 23:26, query_failed 0) -- yet responses carry
no ad flag / no RRSIG: validator treats secure zones as INSECURE.
Trace queued (suspects: outgoing-path fragmentation/IPv6 filtering of
large DNSSEC replies). pihole.local D3 PENDING: FTL upstreams there
are raw 8.8.8.8 -- change = ALL-client 2-5 s blip at restart; needs
owner GO + backup-first.

## 10. Upstream DNS research + config (owner preference: no Google,
##     no-log privacy, real security, full Pi-hole features)

MEASURED FACTS driving the choice:
- unbound on the Pis runs PURE RECURSION (no forward zones) with
  prefetch; the "Google upstreams" were illusory (8.8.8.8 = the
  authoritative ANSWER for dns.google).
- ISP TAMPERING CONFIRMED ON NEW SURFACE: direct +dnssec query to
  Quad9 9.9.9.9 from pihole2 returns NO ad flag (tampered/stripped on
  plain UDP/53); recursive path loses validation marks the same way.
  Same enemy as the IPv6 RDNSS override -- this ISP edits DNS in
  transit. Plaintext egress is not trustworthy on this link.
- Anchor health EXCELLENT: root.key carries both KSKs (20326 old +
  38696 new) state=VALID since Oct 2025 -- the 2026 global KSK
  rollover completed on these boxes. RRSIGs land in cache (16),
  validator module active, zero log warnings.

DECISION (live on pihole2; pihole.local pending GO):
  FTL -> unbound (loopback) -> Quad9 over DNS-over-TLS
  (9.9.9.9@853 + 149.112.112.112@853, forward-tls-upstream,
  tls-cert-bundle) via drop-in /etc/unbound/unbound.conf.d/
  99-mesh-quad9dot.conf. Encrypted egress = the ONLY tamper-proof
  choice against this ISP; Quad9 = Swiss nonprofit, no client-IP
  logs, no ECS, malware blocking, DNSSEC-validating (2026 reviews:
  best privacy-of-the-large-resolvers; Cloudflare 1.1.1.1 = faster,
  audited-minimal-logs, no default blocking -- kept as documented
  fallback). Pi-hole features unaffected: gravity blocks at FTL
  layer; future: FTL-side DoT/DoQ SERVER for mesh clients (certs
  from the Pi CA = LAN-encrypted DNS).
OPEN TASK (cosmetic-class): responses lack ad flag despite
  functioning validator (RRSIG cache, VALID anchors, clean logs);
  Pi-hole UI lock may stay grey. Next trace: verbosity 3 + real
  'flush' + fresh query; suspects: unbound 1.26 ad-marking policy,
  cached pre-DoT insecure entries (flush cache after forwarder
  switchover).
Rollback: rm the drop-in + reload = pure recursion restored.

pihole.local (2026-11-13, standing GO): same package applied --
backup /root/pihole.toml.pre-D3.2026-11-13; Quad9 DoT drop-in +
reload; FTL upstreams -> [127.0.0.1#5335, ::1#5335]; dns.dnssec
true; FTL restart (4 s active). Verified: on-box + halo dig via .11
OK, console 302, leases unchanged (18), unbound clean restart.
Google now OUT of the chain on BOTH Pis; egress = encrypted DoT to
Quad9 on the pair. Pair-consistent DNS architecture achieved.

## 11. D2 (IPv6 RDNSS takeover) -- radvd phase (2026-11-13)

Design: FTL 6.8 has NO RA/RDNSS keys (IPv6 engine = DHCPv6 server
only; router RA sets no M-flag, so clients never solicit it -- the
root cause of the failed IPv6 takeover). Fix = **radvd 2.20 in
RDNSS-only mode** on the Pis: pure DNS advertisement RA
(AdvDefaultLifetime 0 = never claims the gateway; Comcast stays the
path, Pi becomes the resolver). RDNSS = the box's STABLE fe80
link-local (survives prefix changes).

pihole2 (secondary, law-compliant first): radvd installed + enabled;
/etc/radvd.conf (RDNSS fe80::a828..., lifetime 600, intervals 30/100);
kernel gates: net.ipv6.conf.all.forwarding=1 (the global gate --
interface-only is NOT enough; kernel suppresses RA TX otherwise),
eth0.forwarding=1, eth0.accept_ra=2 (hybrid router: Pi keeps its
SLAAC GUA -- halo is pinned to that GUA, must not break); persisted
via /etc/NetworkManager/dispatcher.d/90-radvd (NM resets sysctls on
reactivation). Config validated, service active.
PROOF STATUS: send NOT yet proven on the wire. Sniffing on the
sender cannot see its own TX; RS-solicited reply not observed from
the box itself (suspicious). Next proof: capture from a THIRD machine
(halo raw capture needs sudo interaction; forge harness can run it).
If proven silent, suspects: radvd send path with forwarding=1 set
after start (restart-after-sysctl ordering is now correct).
Debian radvd 2.20 CLI gotchas banked: no -x/-f facility flags; -m
wants pidfile; DebugLevel invalid top-level; interface block needs
{ }; braces.
pihole.local deployment PENDING owner GO: RA from the primary puts
Pi-hole fe80 into every client's IPv6 DNS list (the real
ISP-override fight; clients merge RDNSS from router + Pi; first-
server preference makes Pi-hole compete; client pinning = the sure
path -- halo already pinned both families by owner's rush config).
Netgear CAX80: OpenWrt/third-party firmware = nil (locked DOCSIS
combo, no project exists); RDNSS injection is the ROUTER's own RA
(Comcast DNS) -- Netgear firewall cannot be pointed at it. Bridge
mode = documented later option (owner's troubleshooting objection
is legitimate).

PROOF STATUS FINAL (2026-11-13): root cause of silence found and
fixed -- my earlier sed deleted AdvSendAdvert (substring match), and
radvd defaults to NOT sending. Canonical config now on pihole2:
AdvSendAdvert on + RDNSS fe80 + AdvDefaultLifetime 0, validated,
service active, sysctl gates set + dispatcher-persisted. Wire proof
deferred to a third-machine capture (sender's sniffer can't see own
TX; box-side raw socket saw zero RAs including router's -- vantage
unreliable). Next: capture from forge harness (RS solicitation,
expect RA source fe80::a828 with RDNSS) before deploying to primary.

## 12. Router (Netgear CAX80) pages read (owner screenshots, 2026-11-13)

The IPv6 override's SOURCE is a settings page, not firmware -- fixable.

- **Internet Setup (IPv4 WAN DNS)**: "Use These DNS Servers" =
  Primary 192.168.1.11 / Secondary 192.168.1.12 = Pi-hole. CORRECT.
  WAN IP dynamic; MTU 1500; firewall enabled. Nothing to change.
- **LAN Setup**: **"Use Router as DHCP Server" is OFF** -> Pi-hole is
  the SOLE DHCP server. That is why IPv4 is won (no router DHCP to
  fight) and there is no router/Pi dual-DHCP risk. Address
  Reservation confirms the Pi MACs:
    pihole.local  .11  2c:cf:67:70:3c:ae   (Pi5/NVMe, primary)
    pihole2.local .12  e4:5f:01:a0:2c:44   (Pi4/SD,  secondary)
  Both MAC-pinned. These are D5 keepers; the Pi's dhcp.leases MUST
  carry matching statics (verify next D5 step).
- **IPv6 page (THE FIND)**: Auto Config (SLAAC); "Disable IPv6
  Firewall" unchecked (firewall ON, good). Delegated prefix
  2601:602:57f:ee84::/64. **"IPv6 DNS = Use These DNS Servers"**:
  Primary 2601:602:57e:90ba:9618:65ff:fea5:2ba5 (the ROUTER's own
  WAN IPv6 -- suffix 9618:65ff:fea5:2ba5 = router EUI-64 = Comcast's
  relay), Secondary 2601:602:57e:90ba:40a8:a573:196b:1154 (Comcast
  resolver). These two are exactly what the RA advertises as RDNSS
  (our capture). **THE OVERRIDE IS SET HERE.** The fix: repoint
  Primary/Secondary at Pi-hole IPv6 = the router's RA advertises
  Pi-hole, Comcast RDNSS dies at the source.
- **Dynamic DNS**: OFF (good). **UPnP**: ON, one map UDP 3074 ->
  192.168.1.162 (a LAN device, not infra) -- note, leave.
  **Web Service Mgmt**: Remote Management OFF (good; "Everyone" is
  moot while off); Local "Force HTTPS" unchecked = later hardening.

## 13. IPv6 DNS fix (router-side, the real D2 win)

Router IPv6 DNS page -> enter Pi-hole's STABLE link-local (RDNSS via
link-local is RFC 6106 legal; link-local is MAC-derived so it never
rotates, immune to Comcast prefix churn -- the fragility of the
owner's original GUA-based attempt):
  Primary   fe80:0000:0000:0000:d6a3:4602:f25d:e171   (pihole.local)
  Secondary fe80:0000:0000:0000:a828:a424:5673:846    (pihole2.local)
Both VERIFIED answering IPv6 DNS (dig @fe80..%eth0 -> 8.8.8.8 /
8.8.4.4). If Netgear firmware rejects fe80 in the field, fall back to
the Pi GUAs (2601:0602:057f:ee84:72c0:abdb:26c2:2a84 /
2601:0602:057f:ee84:9e38:e994:e647:24cf) -- work now, break on
prefix rotation; durable fix then = radvd advertising a stable ULA.
Applies: owner makes the router change (availability-touching, their
box). On Apply the router re-issues RAs; clients repoint IPv6 DNS to
Pi-hole within the RA interval, no client/Pi/router reboot. Verify:
RA capture shows RDNSS = fe80 Pi addrs; halo resolvectl IPv6 DNS
becomes Pi. Pi-hole IPv6 chain is already Quad9 DoT (D3). radvd on
pihole2 becomes complementary (it advertises the same fe80 RDNSS).

## 14. IPv6 fix, final shape (2026-11-13): router accepts GUAs only

Netgear validates the IPv6 DNS fields as GLOBAL UNICAST -> fe80 AND
fd00 (ULA) are rejected at the keyboard (the owner's original defeat
explained). Two composed layers:

1. ROUTER PAGE (owner applies, will validate): IPv6 DNS "Use These
   DNS Servers" =
   Primary   2601:0602:057f:ee84:72c0:abdb:26c2:2a84  (pihole.local GUA)
   Secondary 2601:0602:057f:ee84:9e38:e994:e647:24cf  (pihole2.local GUA)
   Kills Comcast relay/resolver from the RA. Rides Comcast's
   delegated prefix: on rotation the two fields go STALE -> clients
   fall back to IPv4 .11 (Pi-hole, graceful); owner repastes from
   the watchdog's prompt.
2. RADVD (durable layer, deployed on pihole2): advertises RDNSS =
   fe80 (MAC-derived, rotation-immune, outside Netgear's
   validation). Third-machine wire proof owed (own-box sniffer
   can't see own TX) -> task posted for forge capture.
3. WATCHDOG (queued, portal-side): watch delegated prefix on the
   Pis; on rotation, alert owner with the two fresh paste strings.

Verification after owner Applies: RA capture (RDNSS must be the two
GUAs), halo resolvectl, and a dig over IPv6 through Pi-hole.

## 15. IPv6 VALIDATION (post-Apply, 2026-11-13) -- WON

Ground truth from pihole.local's query log (last 2000 lines):
357 queries = 290 IPv6-source + 49 IPv4 + cache chatter -> 8+
distinct LAN devices now resolve IPv6 THROUGH PI-HOLE (privacy
addresses, real client devices). Router change took effect network-
wide within minutes, no reboots.
halo link DNS list (RA-derived): pihole GUA + pihole2 GUA (owner's
router entries) + fe80::a828 (pihole2 radvd -- TX PROVEN by a real
client receiving it) + ONE residual: 2601:..9618.. = Netgear
self-appends its relay address to every RDNSS list (firmware, not
removable). Comcast's resolver pair (40a8:a573...) GONE from the
list. Router itself (192.168.1.1) queries Pi-hole (its WAN DNS =
.11) = router lookups governed too.
RA-capture forensics: kernel snmp6 counters proved RA flow healthy
(InRA +15/45s; radvd OutRA incrementing; pihole.local FTL emits RAs
too -- its engine works, just never answered my RS pattern); raw-
socket capture vantage was the flaw, not the wire. Forge bench
capture (#23) now optional.
Residual law: last-mile = client pinning on key machines (partner
PC: pin .11/.12 + Pi v6; meshd automates this for mesh nodes).

## 16. D5: statics + nightly sync (2026-11-13)

Policy (owner): dynamics for most; statics for servers/infrastructure;
names in Pi-hole everywhere. Statics live in pihole.toml [dhcp].hosts
(the .leases files are the lease DB, not statics -- plan corrected).
Written on the primary (and synced to pihole2):
  2c:cf:67:70:3c:ae -> .11 pihole (Pi5)   e4:5f:01:a0:2c:44 -> .12 pihole2 (Pi4)
  9c:bf:0d:00:e6:ca -> .228 Halo          f0:b2:b9:18:40:80 -> .25 RainbowBlock
  10:9c:70:27:9f:04 -> .170 prusa-xl      44:5c:e9:1e:1b:8c,ignore
All were already their current IPs = zero client movement. prusa-xl
name now resolves (statics act as local DNS records); the printer's
IPv4-preferring software gets a permanently stable address. SteamOS
dual-boot caveat: same NIC MAC -> same lease in both boots; name
display follows the static entry.
NIGHTLY SYNC (03:15 root cron on the primary): pihole-sync.sh rsyncs
gravity.db + adlists.list + statics-JSON (regenerated from toml) to
pihole2 staging; pihole-apply-sync.sh on pihole2 snapshots to
/root/pihole-sync-backups (rotate 7), swaps, chowns, re-applies
statics, restarts FTL, verifies dig. First run: APPLY-OK, exit 0,
pi2 DHCP confirmed still OFF (full pihole.toml deliberately NOT
synced -- it would re-enable pi2 DHCP and recreate dual-DHCP).
Deploy key: /root/.ssh/id_ed25519_sync (PI) -> meshops@pihole2
authorized_keys (infra-to-infra, revocable one line).

D5 verification (same day): generator bug (regex matched [dns]'s empty
hosts = [] first) fixed by scoping to [dhcp]; re-run APPLY-OK, pi2
toml carries 5 statics, dhcp.active false preserved. pi2 name
lookups forward (statics inert while its DHCP is off -- staged for
takeover, by design). Deploy key + scripts + cron all in place;
sync-hosts regenerated nightly from the primary's toml.

## 17. D4 keepalived VIP -- DRILL PASSED (2026-11-13)

keepalived 2.x on both Pis: vrrp_instance PIHOLE, VRID 51, VIP
192.168.1.13/24 eth0, advert 1 s, PASS auth (stored root-only
/root/keepalived_authpass.txt, 600). Priorities: pihole=100
(master), pihole2=90. Configs identical apart from router_id +
priority; enabled at boot both sides.
Drill (live): primary preempt -> pi2 released (0); stop keepalived
on primary -> VIP on pi2 at +5s, dig via VIP answered (8.8.4.4 =
pi2 chain, i.e. quad9 DoT failover path intact); start primary ->
preempt back (pi2 zero, dig 8.8.8.8). DNS answered THROUGHOUT --
one Pi dying is a DNS non-event. Console rides the VIP too (FTL
serves web on all addrs).
STAGED NEXT (owner GO, availability-touching): DHCP option 6 -> VIP
via /etc/pihole/dnsmasq.conf dhcp-option=6,192.168.1.13 on the
primary; clients adopt .13 as DNS at lease renewal (no forced
renews); halo's pin (.11) and router WAN DNS (.11) unaffected.
Verify: renew pi2's own lease (nmcli reapply) -> nmcli shows
IP4.DNS=192.168.1.13.

## 18. Final pieces: option 6, RTC reboot, boot hardening (2026-11-13)

- DHCP option 6 -> VIP live on the primary (dnsmasq.conf
  dhcp-option=6,192.168.1.13); clients adopt .13 at lease renewal.
  Bonus find: the legacy dnsmasq.conf advertised ntp-server
  0.0.0.0 (unusable) -> repointed to 192.168.1.13 (FTL's NTP server
  answers there).
- Both Pis' IPv4 addressing confirmed STATIC in NetworkManager
  (manual .11/.24 + .12/24, IPv6 SLAAC preserved) -- the
  self-serve-DHCP boot race is a non-issue; infra addresses depend
  on no peer.
- RTC on pihole.local: dtparam=rtc=on (config.txt backed up
  .pre-rtc); reboot (owner window) -> kernel 'rpi-rtc: setting
  system clock' from battery at boot; hwclock via util-linux-extra;
  NTP confirms seconds later. Cold-boot-1970 risk CLOSED on the
  CA-primary-to-be.
- Live-fire during reboot: pihole2 took the VIP while the primary
  was down and served DNS; preemption returned clean. All boot
  units enabled (FTL/unbound/keepalived/radvd); leases 17 intact;
  radvd + names resolving from fresh boot.
OPTION-6 VERIFICATION: handout rides lease renewals; spot-check any
client's next renewal shows DNS=192.168.1.13 (halo's pin and router
WAN DNS stay .11 by design).
Reboot ledger: pihole.local RTC DONE. Remaining: pihole2 noatime
(+ DS3231 when owner builds it).

## 19. pihole2 DS3231 RTC + SD-wear round (2026-11-13)

Owner installed DS3231 on pihole2's I2C bus. Enabled in firmware
(config.txt backed up .pre-rtc): dtparam=i2c_arm=on +
dtoverlay=i2c-rtc,ds3231. First boot after overlay: chip found at
0x68 (rtc-ds1307 1-0068, registered rtc0) but unseeded (kernel
"SET TIME!", hctosys read refused -- factory state). Seeded with
hwclock -wu; direct reads tick 1 s/s; SECOND reboot proved the
whole chain: kernel "setting system clock to 2026-11-14T01:26:36
UTC" from the battery at boot. fake-hwclock not installed (the
classic shadow package absent -- nothing to disable). timesyncd
enabled (writes RTC at shutdown); NTPSynchronized=yes.
Cold-boot-1970 risk CLOSED on BOTH Pis. Pair now boots with true
time with zero network: the CA's dependency chain is complete.

SD-wear round (pihole2, same reboot window): noatime was ALREADY
set on root (owner's prior care; ledger item closed as verified),
journal capped SystemMaxUse=50M (usage now 8M), FTL query
retention 91d -> 30d. Reboot ledger: COMPLETE (pi RTC done §18,
pi2 RTC done §19, noatime verified, journal capped).
Verification method note: /tmp scripts streamed via 'ssh bash -s'
(nested-heredoc forms choke the portable shell scanner).

## 20. Owner directive: HTTPS rollout order (2026-11-13)

Once the CA can issue HTTPS-usable certs (M2): enable HTTPS on BOTH
Pi-hole consoles, PILOT ORDER -- pihole2 first as the test.
Procedure per box: back up the existing self-signed tls pair
(/etc/pihole/tls.key/tls.crt, kept), swap in the CA-signed pair for
that box's names (pihole2.local + VIP SANs), FTL serves 443 natively
(no proxy), "Force HTTPS" decision left to owner after the padlock
is seen. Gate: builder announces "pihole2 is on HTTPS" and STOPS;
owner tests .12 from his machines (padlock + identity); only on
owner's pass does the same land on pihole.local, and only after that
do other services (router pages, forge, portal-adjacent) get pointed
at the CA. Nothing self-signs, ever; the swap is reversible by
restoring the kept backup.

## 21. M2: the mesh CA is LIVE, and pihole2 is the HTTPS pilot (2026-11-13)

step-ca 0.30.2 + step CLI 0.31 (arm64 tarballs from dl.smallstep.com /
GitHub; hashes banked in memory 184) on BOTH Pis, systemd unit
step-ca.service, port 9000 (additive; console/DNS untouched). Root
fingerprint d570fecfd1a1c8ee3b0120426030b13d352d3fbbc944da1d4fce851d8a890ed5
(public; copy in docs/mesh-root_ca.crt + ~/ai/state/mesh/). Key
custody: root key on pihole (root-only 600), tree replicated to
pihole2 (hot standby; same root, ACME+JWK provisioners live on both).
CA lessons (0.31 reality, hard-won): standalone init puts NO
provisioner key on disk and the CLI cannot decrypt encrypted JWK
provisioners headless (JWE format unsupported) -> JWK-token issuance
is dead headless; ACME (init --acme) is the working lane (passwordless
+ certbot renewals + Caddy-ready). ca.json provisioners seed the DB
once (wipe db dir to re-seed). ACME http-01 needs resolvable names:
FTL special-cases .local/.arpa/.internal/TLD-only; the working zone
is TWO-LABEL home.lan via dns.localiseDomains=["lan"] + /etc/hosts
entries (pihole/pihole2/halo/rainbowblock/prusa-xl .home.lan now
resolve mesh-wide through FTL on both Pis + VIP). certbot 4.0 manual
plugin: CERTBOT_VALIDATION/CERTBOT_TOKEN env (not KEY_AUTHORIZATION);
serving docroot must contain .well-known/acme-challenge/ literally;
background hook children must not inherit hook stdout (hangs certbot
cleanup); hooks persisted in /etc/letsencrypt/renewal/*.conf.
PILOT (directive 20): certbot (apt certbot 4.0) issues for
pihole2.home.lan (ECDSA, ~48h validity, ACME default); deploy hook
assembles FTL-style PEM (KEY-first, pihole:pihole 600) into
/etc/pihole/tls.pem + FTL restart; FTL pauses ~15s per issuance/
renewal window (auth-hook), announced. Renewal: certbot-renew-mesh
.service/.timer (02:30/14:30 + jitter, REQUESTS_CA_BUNDLE env).
DRILLS PASSED: issuance (Verify return code 0 against root from halo
+ loopback), FORCED RENEWAL (serial 8EAB->D24B, gen-2 archive, FTL
serving new pair), rollback = self-signed pairs kept on-box
(/root/tls.pem.pre-CA.*, regenerated FTL original).
NEXT (owner gate per directive): owner installs root on halo
(sudo cp docs/mesh-root_ca.crt /usr/local/share/ca-certificates/
halo-home-mesh-ca.crt && sudo update-ca-certificates), Windows/Mac
commands in announcement; owner browses https://pihole2.home.lan
for the padlock; on his PASS -> same lane for pihole.local, then
other services (Caddy via ACME for forge/portal-adjacent).

Pilot polish (owner test day): FTL logged CERTIFICATE_DOMAIN_MISMATCH
(cert lacks pi.hole, webserver.domain said pi.hole). pi.hole is a
LOOPBACK name -> ACME can never issue for it (CA would validate its
own box). Fix = the FTL side: webserver.domain ["pihole2.home.lan",
"pi.hole","192.168.1.12"] on pi2 (toml backed up .pre-webdomain);
warning gone; cert SAN pihole2.home.lan verified 0 from loopback,
VIP-path and halo. Owner PASSes: installed root on halo + iPad,
tested https://pihole2.home.lan = good.

## 22. Both Pi-hole consoles on mesh-CA HTTPS (2026-11-13)

Directive 20 complete: pihole.local joined the lane (certbot + hooks
+ deploy + certbot-renew-mesh timer 02:40/14:40, CA-bundle env).
Issuance for pihole.home.lan PASSED (one announced ~15 s FTL window
on .11); webserver.domain ["pihole.home.lan","pi.hole","192.168.1.11"]
(toml backup .pre-webdomain on pi too); FTL warnings 0 on both boxes.
Witnesses: halo system-trust https://pihole.home.lan/admin -> 302
(padlock, root already trusted on halo/iPad); VIP path -> 302;
pihole2 -> 302; chain Verify 0 everywhere.
NOTE (queued refinement): VIP SNI serves the cert of whoever holds
.13 -- during failover, pihole-vip visitors get the survivor's name
mismatch. Fix shape = coordinated multi-name certs (each box issues
with -d for its own name AND the VIP name; http-01 per-identifier
requires the challenge served at that identifier's IP, so the VIP
name must resolve to BOTH boxes in turn -- dns-01 lane or staged
co-issuance; documented, not built).
Owner applied OS security updates + Pi-hole v6.5->v6.9 on pihole2 +
reboot; post-reboot battery: RTC boot-from-battery proven (kernel
line), VIP correctly 0, noatime/journal-cap survive, radvd TX, all
six units enabled, TLS pair survived v6.9 intact (FTL did NOT
self-regenerate). FTL v6.9 now on pi2 (primary stays v6.8 --
update parity decision queued, permission-gated).

## 23. Primary patched + v6.9 parity (owner method, 2026-11-14)

Owner-recommended symmetry: apt update/upgrade + pihole -up + reboot
on pihole.local (same as owner did on pihole2). Results: kernel
6.18.50+rpt-rpi-2712 RUNNING (security patches live), FTL v6.9 on
BOTH Pis, RTC boot proof, 18 leases intact, renew timer enabled,
cert pair survived v6.9 untouched, and the reboot re-proved failover
(VIP -> pi2, DNS answered on .13 throughout, preemption back clean;
pi2 released). pihole -up QUIRKS caught: (a) the updater RESET
webserver.domain to pi.hole; (b) FTL 6.9 semantics: webserver.domain
is a SINGLE bare string via --config (JSON arrays and IPs rejected;
6.8-era accepted arrays); set 'pihole.home.lan' on pi,
'pihole2.home.lan' on pi2 (toml ### CHANGED live on both); pi.hole
remains a built-in alias (302 loopback). (c) --config writes while
FTL runs can be clobbered by FTL's own writer -- stop FTL for cold
writes if a write must stick. (d) sed/regex on pihole.toml must
target LIVE lines (the file's default-comment block mirrors keys;
naive regex hits comments and the value silently stays default).
Post-window: FTL warnings 0/0, consoles 302 from halo system trust,
DNS .11/.12/.13 answering. Pair state: fully patched, v6.9-matched,
HA-proven twice, CA + both HTTPS lanes live, timers on both.

## 24. Trust day: iPad root + cert lifetime hardening (2026-11-14)

Owner-side truth sequence (iOS): the installed root needs
Settings -> General -> About -> Certificate Trust Settings ->
ENABLE FULL TRUST. With the toggle on, *.home.lan padlocks in both
Safari and Firefox iOS (Firefox iOS rides the system store). Root
installed as plain profile without the toggle = SEC_ERROR_UNKNOWN_
ISSUER-flavored distrust (Firefox shows its NSS strings). Root
fingerprint to expect: d570fecf... (docs/mesh-root_ca.crt).
STANDING LAW (now proven on clients): *.local and pi.hole show
trust warnings FOREVER -- .local is shadowed by every Pi-hole (no
CA can validate a challenge there) and pi.hole is loopback; FTL's
self-signed default vhost serves them, correctly. Padlock names =
pihole.home.lan / pihole2.home.lan. Convenience for the owner's
typing speed: iOS Text Replacements ph -> pihole.home.lan,
ph2 -> pihole2.home.lan.
CERT LIFETIME: first expiry rode out on the 48h ACME default
(step-ca 0.30.2) and bit the iPad exactly at window end -- caught
fast, lesson kept. CA claims now set on the ACME provisioner:
min 1h / max 8760h / DEFAULT 720h (30 days). Re-seed via ca.json +
db wipe (accounts die with the wipe -> certbot caches dead account
ids: clear /etc/letsencrypt/accounts/192.168.1.11:9000 on each box
before re-issuing; certbot 4.0 does not auto re-register). Timers
(02:30/14:30) renew inside certbot's last-third window with ~10
days of slack. Re-issuance in flight with announced FTL windows.

## 24.5 Cert-lifetime saga, closed (2026-11-14)

The 48h->24h ACME default expired onto the owner's iPad; the fix
hunt found: (1) step-ca 0.30.2 honors provisioner claims ONLY on
some seed paths -- pi's DB seeds strip claims (provisioner list
shows null) while pi2's honored them once; authority-global claims
also ignored; db wipe+reseed cycles strip ACME accounts => certbot
caches dead account ids (clear /etc/letsencrypt/accounts/<host>:9000
before reissue). (2) apt has NO step-ca on either box (both
RaspberryPi-OS trixie: pi2's 0.31 memory was wrong -- both run the
0.30.2 tarball binary at /usr/local/bin/step-ca). (3) memory db
backend = ACME unsupported (activating-loop trap). (4) badger db
block in 0.30.2 = {"type":"badgerv2","dataSource":...,
"badgerFileLoadingMode":""} -- "backend" key = "database not
supported" boot fail. ACCEPTED STEADY STATE: 24-day certs are fine
IF renewals are phase-proof; the 02:30/14:30 calendar pair can
land in certbot's no-renewal dead zone (last-third rule) around a
24h anchor -- that is exactly the trap that expired the iPad cert.
Fix on BOTH timers: added monotonic triggers OnActiveSec=2h +
OnUnitActiveSec=5h alongside the calendars; fires are no-ops
unless renewal is due; max elapsed at first qualifying fire ~21h
=> >=2h margin before expiry. PI unit now carries
--password-file /etc/step-ca/secrets/password (mirrors pi2).
Final: both consoles 302 via halo system trust, chain Verify 0,
CA active on pi (ACME live, root d570fecf...), 10/10 services,
DNS .13 answering. PARKED: claims tuning (needs newer step-ca),
30-day certs (cosmetic; 24h+5h timers is a supported pattern).
Note: /root/ca.json.* backups on pi vanished mid-session (un-
explained; pihole.toml backups intact) -- keep durable CA config
backups in the repo/~/ai/state, not only on-box /root.

## 26. DNSSEC warnings demystified: zone probes, not CA trouble (2026-11-14)

Owner saw FTL DNSMASQ_WARN "insecure DS reply received for
pihole.home.lan" on pi2 and asked: is the CA foundation sound before
we build the hub on it? Full forensics:
- Warnings are 100% OUR private zone: 65/65 pi2 + 113 pi, all
  home.lan/lan names; ZERO internet-name warnings.
- Cause: home.lan has no DNSSEC chain (it cannot -- .lan is not a
  delegated TLD; we are the authority). FTL 6.9's dnsmasq (with
  dns.dnssec=true) probes DS for the zone, the internet answers
  "no such zone" (proven insecure = a FACT about private zones),
  dnsmasq logs it as a warning. Verbose, not broken.
- delv (unbound's proof tool) on pi2: dns.google = "fully validated"
  (fresh RRSIGs) -> the REAL DNSSEC security path (FTL -> unbound
  validator -> Quad9 DoT) demonstrably works; root auto-anchor
  files on BOTH boxes updated same-day (14:33/14:36).
- delv on pihole.home.lan = "broken trust chain" (purist refuses
  unsigned) while dig answers the right IP: the DNSSEC-true-for-
  internet, unsigned-by-design for us split, exactly as designed.
- Trust law for the CA era: name->identity binding is the CA's job
  (padlock = cert verified vs mesh root); DNSSEC protects internet
  chains; private-zone names are protected by TLS. DNSSEC is not a
  dependency of the CA/hub/tailnet/ssh-cert work. CA foundation:
  verified sound (chain Verify-0 everywhere, renewal drilled).
PROPOSED (owner gate, not applied): FTL dns.dnssec=false on both
Pis -- dnsmasq-layer validation is redundant theater when the sole
upstream is a validating resolver over encrypted DoT (unbound IS
our validator, proven by delv). Kills ALL 65/113 warnings;
internet validation unchanged. Window: 4s FTL restart per box,
DNS rides VIP + standby (drilled shape). Awaiting owner GO.

## 25. MESH HUB LIVE: registry + mirrored secrets broker (2026-11-14)

Owner GO "work on the pihole network services today" -> meshhub
built + deployed on the pair (design: docs/MESH_HUB_DESIGN.md).
TCP 9443, mTLS (mesh CA), additive; mesh/hub/ Rust crate in repo
(meshhub daemon + meshctl client; halo + pair binaries built).

Answered owner asks: (1) NO per-ACL duplication -- age
multi-recipient: ONE ciphertext, N recipient wraps (measured: 694B
-> 824B for 2 grants). (2) enumeration = GET /nodes + meshctl
nodes; designate = put --to a,b; owner-scoped; share = client-side
RE-ENCRYPT (hub never holds plaintext -> ACL lives in crypto, hub
compromise cannot widen access). (3) GUID indirection: metadata
index (id/name/owner/recipients/kind/size/sha) -> blobs/<id>.age;
leaked id = inert ciphertext. Growth: kind=blob roundtripped 2 MiB
byte-exact; max_bytes 32 MiB config.

Pair law: WRITER marker (primary, OUTSIDE store dir), standby
503s writes (TAKEOVER promotes), mirror = per-mutation rsync -aq
--delete via root->meshops staging + sudo apply (proven nightly
channel; pi2 root stays locked), store reload-per-request (disk =
truth; standby registry advances with replication; deletions
prune; stale blobs inert by design). Audit = journald metadata
only (op/who/obj/res; no payloads -- witnessed). Node identities:
admin-mint lane = root-signed 24h leaf (step certificate create
--ca-key encrypted --ca-password-file; root key never
plaintext-on-disk; step-kms-plugin 0.11.3 installed to satisfy
0.31 crypto; token/JWK lane parked (plain mesh.json unreadable
by LoadKey; CLI 0.31 sign needs plugin protocol this build
lacks)). 24h node cert expiry = revocation: pi2 showed 401 on
expiry mid-test (model proven). Enrolled trio: halo/labtop/pihole2.

BONUS FINDING (renewal hardening): certbot units had
Environment=REQUESTS_CA_BUNDLE in [Unit] (ignored) -> first real
renewal would have failed TLS; renewal oneshots had default 90 s
timeout vs certbot's 0-60min non-interactive random delay -> every
real renewal would have been KILLED mid-delay. Both fixed:
/usr/local/bin/certbot-renew-run.sh (env + renew + meshhub restart)
+ TimeoutSec=3600, on BOTH boxes; forced renewal in flight with
serial rotation witness (background). Nightly pihole-sync.sh gains
meshhub store backstop (snapshot tar + staging rsync).

E2E witnessed: put/get/recipient-read/DENY-403/share/grant-read,
standby-503, mirror witness on pi2, blob exactness, audit lines,
enumeration, 401-after-cert-expiry. Hub code traps fixed during
build: persist_nodes/index must serialize per-map (whole-Store
write poisoned typed loads); Stream::new(&mut conn, &mut sock);
WebPkiClientVerifier replaces AllowAnyAuthenticatedClient; age
0.11.5 API (with_recipients(Iter), ArmoredWriter, Identity::
generate, Decryptor::new(ArmoredReader)); standby memory staleness
(reload per request).

Renewal witness (forced, 2026-11-14 evening): pi serial 4E05->5A24
(24h window, full chain incl. meshhub TLS reload, console 302
throughout); pi2 NOT-due -> clean no-op (timer catches it in the
last-third; phase-proof timers live). Hub battery green post-churn:
enumeration + put/get/rm probe + DNS on VIP.

## 25b. Hub round 2: rotation, automation, portal P0, migration (2026-11-14)

- HUB ENROLL ROTATION: name may move to a new cert fp (daily 24h
  re-enroll); authorized by the CA itself (only admin-mint yields a
  valid cert); ownership of records MIGRATES on rotation (audit
  "ROTATED owner-migrated=N"); recipient grants are name-based and
  ride through. meshctl keeps the age identity key STABLE across
  re-enrolls (grants key to the pubkey).
- AUTOMATION: halo = systemd USER timer mesh-reenroll.timer
  (daily 07:30 + boot 30m + Persistent; import lane: mint on pi via
  ssh -> scp -> enroll -> purge). pi2 = root cron on pi
  /etc/cron.d/mesh-reenroll 07:45 (mint+stage+enroll, purge). Both
  run live tonight (halo fp 2c33->5c59, pi2 eb30->0f87).
- PORTAL P0: app/core/meshhub.py (meshctl subprocess client: nodes/
  listing/get/put/share/rm; put purges its temp file; probes are
  authenticated -- hub TLS is client-cert-required by design, no
  allow_anonymous in rustls 0.23.46). tests/test_meshhub.py: 8
  green incl. LIVE hub e2e (put/get/list/rm).
- MIGRATION: subagent-key IN the hub (owner halo, recipients
  halo-only until forge enrolls), sha256 roundtrip verified, source
  file untouched (retire at agent cutover).
- FORGE KIT: docs/mesh-forge-enroll.md + deploy/forge_enroll.ps1
  (ASCII PS5.1; bundle purge after enroll; meshctl.exe built by the
  forge via the proven meshd relay route).
- Scripts live in repo scripts/: mesh_migrate.py,
  mesh_reenroll_{halo,pi2}.sh.

Round-2 witnesses (evening): PARTNER SCENARIO PROVEN with a
enrolled 'macbook' node (roles=partner): node writes its own key
(sole recipient itself) -> invisible in owner ls, owner get = 403
not-in-recipient-set, hub journal = metadata only (who=macbook
put; who=halo get DENIED), partner reads + deletes its own.
Nuance documented: meshctl put ALWAYS includes the writer as a
recipient (owner cannot lock self out); invisibility requires the
partner to be the WRITER -- exactly the MacBook scenario. Mirror
witness: subagent-key blob replicated to pi2 as armored age text.
FTL: zero DS warnings since dns.dnssec=false (last 14:38, logs
fresh 19:30 pairwide; file totals 158/118 = history). pi2 console
cert due Nov 15 10:20 PST (monotonic timers armed). Registry:
halo/labtop/macbook/pihole2.

## 25c. pi2 cert-lane incident, closed (2026-11-15, owner GO)

pi2 console cert (due 10:20 PST) failed to renew twice (02:52,
07:52): ACME `accountDoesNotExist` -- the CA-db-wipe residue: pi's
certbot account cache was cleared at the wipe; pi2's was NOT.
Timers + phase-proof shape ACQUITTED (fired on time; account died).

Repair lane (pi's proven reissue): clear account -> certonly
--manual (auth-hook http-01, mesh CA dir, ecdsa, deploy hook).
Three traps caught mid-repair:
1) certonly landed lineage `pihole2.home.lan-0001` (bare-name
   archive collision at the first attempt), and
2) the deploy hook on BOTH boxes was PINNED to
   /etc/letsencrypt/live/<bare name> -> wrote an EMPTY tls.pem ->
   FTL TLS dead + meshhub crash-loop (DNS rode through; FTL stayed
   active). Hook now RENEWED_LINEAGE-driven + refuses empty PEMs,
   installed on pi AND pi2 (pi pinned = same latent trap).
3) orphaned renewal conf (bare name, dead account) reappeared ->
   would poison every timer run; removed; `certbot renew` now
   passes clean on one lineage.
Also: `set -e` + `is-active` 3 s after the deploy-hook FTL restart
= recovery script died mid-window (transient state, not failure).
Post-repair witness: serial AE9948->485931E9 (24h), tls.pem 2 certs
KEY-first, FTL active, console 302 under full system trust from
halo, pi2 hub healthz over mTLS {"ok":true}, DNS local+VIP green.
Durable laws banked: wipe CA db -> clear certbot account caches on
EVERY ACME client (pi, pi2); deploy hooks must derive from
RENEWED_LINEAGE; probes of name-SAN certs use names, not IPs.
