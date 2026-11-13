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
