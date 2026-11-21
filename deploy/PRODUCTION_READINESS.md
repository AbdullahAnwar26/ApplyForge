# ApplyForge production readiness (Milestone 8.4e)

Assessment date: 2026-11-20. Scope: the private, VPN-only single-server deployment described in `deploy/README.md`.

## 1. Verdict

**Not production-ready yet.** The deployment configuration, the backup tool and the runtime have been validated as far as a local system allows: Ubuntu 26.04 under WSL, real nginx, Gunicorn, systemd, PostgreSQL and age, with loopback NFS and SMB. **No real production host, VPN, certificate, firewall, backup storage or production database exists yet**, so the requirements in section 5 are unverified by definition.

What exists now:

- a configuration that has passed every local test;
- an automated, read-only host check (`deploy/host_check.py`) and a storage probe (`applyforge-backup probe-destination`) that turn the remaining real-host requirements into repeatable checks;
- runbooks for deployment, operations, incidents and disaster recovery.

The first deployment can proceed once the blockers in section 8 are closed and the authorizations in section 9 are given.

**Update after the 2026-11-20 dress rehearsal (Step 1.1).** The runbook was executed end to end, sections 2–13 in order, on a fresh disposable Ubuntu 26.04 instance with fictional data (section 3a). The deployment procedure, backup, offline verification, restore, upgrade and rollback all worked as written. The documented **switch-to-restored-data** procedure did not work at all; it has been rewritten and the new version rehearsed successfully. Smaller defects were fixed in `host_check.py` and the runbook. The verdict stands: the remaining blockers all need real infrastructure.

**Update 2026-11-21 (later): the two-server architecture was rehearsed in disposable VMs (section 3c).** Private DNS, DNS-01 (with Let's Encrypt's test server), backup isolation with a sealed vault, monitoring (mock receiver) and application-server loss were all exercised. Six defects were found and fixed. Nothing is production-verified.

**Earlier on 2026-11-21: the production architecture was planned.** `deploy/INFRASTRUCTURE_DECISIONS.md` holds the decision register:

- Hetzner app and backup servers in different countries;
- WireGuard device and backup links;
- split DNS with Let's Encrypt DNS-01;
- storage-side snapshots;
- key custody;
- Healthchecks.io monitoring;
- emergency access.

The proposed procedures are in runbook section 15. They are **proposals**: nothing in them is counted as verified until rehearsed (U12–U17). The verified procedures in sections 2–14 are unchanged.

## 2. Selected architecture

| Layer | Decision | Why |
|---|---|---|
| OS | **Ubuntu 26.04 LTS** | validated end to end; packages for nginx 1.28, PostgreSQL 18, Python 3.14, age 1.2 |
| Python | **distribution `python3` (3.14.4)** in a per-release venv, installed **wheel-only** from `backend/requirements.txt` + `backend/constraints.txt` | Ubuntu 26.04 has no `python3.12`; a second interpreter adds patching burden. psycopg2-binary had to move 2.9.9 → 2.9.13 (no 3.14 wheel for 2.9.9). Full suite passes; `pip-audit` clean. |
| Web | nginx on the VPN address only, TLS, HSTS (1 year, no subdomains/preload); Gunicorn on 127.0.0.1:8000, 1 worker × 8 threads | unchanged from 8.4c; now validated with the real binaries |
| Service | `applyforge.service`, `Type=notify`, strict sandbox (exposure 1.2), loopback-only IP | validated under real systemd |
| Database | PostgreSQL on the same host, localhost only, scram-sha-256, separate owner and isolated restore roles, PUBLIC rights revoked on the app database | section 6 |
| Backups | daily encrypted (`age`) archive, trusted hash index, **NFSv4.1+ `sync` export, mounted `hard`**, storage-side snapshots | section 7 |
| Access | VPN (WireGuard or equivalent) + host firewall default-deny; nginx allowlist as a second layer | the firewall is the primary control |

## 3. Evidence: tests run for this assessment (all repeatable)

| Test | Where | Result |
|---|---|---|
| Backend suite | Windows, Python 3.12 | 557 OK |
| Backend suite | Ubuntu 26.04, Python 3.14.4, pinned constraints | 557 OK (12 browser tests skipped by design) |
| Backup tool suite (`deploy/backup/tests`) | Ubuntu 26.04, as root and as a normal user | 152 OK each (4 root-only skips as root, none as a normal user) |
| Mutation check (safety mechanisms disabled one at a time) | Ubuntu 26.04 | 36 of 36 mutants killed |
| Host-check suite (`deploy/tests/test_host_check.py`) | Ubuntu 26.04 | 24 OK after the dress-rehearsal fixes (1 opt-in real-host smoke skipped) |
| nginx + Gunicorn end to end (`deploy/tests/stack_test.sh`) | nginx 1.28.3, Gunicorn, Django production mode | **61/61** |
| Application unit sandbox (transient unit with `applyforge.service`'s directives) | systemd 259 | `Type=notify` ready, health 200, exposure 1.2; writes, outbound IP, user namespaces and other users' processes denied |
| Backup unit sandbox + capability set | systemd 259 | everything the tool needs works; the negative control without DAC/KILL fails as expected; exposure 2.7 |
| Real `create` + `ExecStopPost` under systemd (normal, SIGKILL, SIGTERM, inactive service) | systemd 259, PostgreSQL 18.6, age 1.2.1 | 26/26 |
| `probe-destination` on NFSv4.2 (root_squash and not), NFS `soft`, SMB 3.1.1 (Samba 4.23), ext4, tmpfs, vfat | loopback, disposable | every type passes; warnings as expected (section 7) |
| Host check against an unprovisioned machine | WSL | correctly reports missing accounts, layout, nginx and database; PostgreSQL configuration passes |
| Dependency audit | `pip-audit` on the pinned set | no known vulnerabilities |
| Earlier milestones (still valid) | 8.4d real-age suite; PostgreSQL backup → restore rehearsal | identical data after restore |

Disposable resources used in 8.4e were uniquely named (`af84e-*`) and removed after each run, with two exceptions: the 8.4e runtime venv (`/var/lib/af84e-112002412667ce/`), which is kept for reruns, and the protected and retained 8.4d resources, which were not touched. Packages installed for the NFS and SMB tests were purged afterwards.

## 3a. End-to-end dress rehearsal (2026-11-20)

**Environment.**

- A new WSL instance, `af84f-dress-11200405d0b3`: Ubuntu 26.04.1, systemd 259, nginx 1.28.3, PostgreSQL 18.6, age 1.2.1, Python 3.14.4.
- The default distribution and its PostgreSQL were never used, and the instance is stopped, kept for inspection.
- The release was built from commit `948f58b`: `git archive`, and a frontend `npm ci` with lint, typecheck, 277 tests and build, plus checksums.

**Simulated, not verified.**

| Real-world element | Stand-in |
|---|---|
| VPN address | loopback alias `127.0.0.2` |
| VPN subnet | `127.0.0.0/29` |
| Private DNS | `/etc/hosts` |
| Private CA | a local CA |
| NAS | loopback NFSv4.2 in the same instance |
| Offline machine | a separate unprivileged account |
| Firewall | none: it would affect the other WSL distros, which share the network namespace |

PostgreSQL ran on port 55432, linked to the default socket, for the same reason.

| Runbook step | Result | Measured |
|---|---|---|
| 2–8 provision, install, preflight, migrate, start | 24/24 | venv + wheel install 94 s; migrate 2.1 s; start to ready 0.8 s; exposure 1.2 |
| 8.1 health table (server side and "VPN client"; outside client 403; not reachable from Windows) | 24/24 | |
| 13.1 host check | 0 failures (after fixes); 1 warning (firewall) | |
| 12.2–12.6 keys (primary + escrow), NFS + fstab, tool, units, `probe-destination`, restore role, PUBLIC revocation | 15/15 | |
| 12.4 first backup through the unit; offline verification (both identities); manual 12.9 check; timer | 14/14 | unit 1.54 s; **client-visible outage 1.06 s** (502) |
| 12.6 restore drill + 12.7 validation + recovery point | 12/12 | restore 0.46 s; data and media identical; a post-backup write is absent (RPO = capture time) |
| 12.7 switch **as documented** | **failed** (see the defect below); rolled back cleanly | |
| 12.7 switch, **corrected** (rename-based), timed as DR | 17/17 | restore 2.45 s; unavailable during the switch 7.68 s; restore start to healthy 10.88 s |
| 9 upgrade to a second release; 10 rollback | 20/20 | outage 0.97 s / 1.07 s |
| Unplanned instance restarts (WSL idle shutdown) | all services and the NFS mount came back through systemd | |

Timings are for a few KB of fictional data on local disks. Production timings must come from the first production drill (U11).

**Defects found.**

1. **High – documented production-switch procedure (runbook 12.7) cannot work.** It pointed `DATABASE_URL`/`MEDIA_ROOT` at the restored names.
   - The service sandbox allows writes only to `/var/lib/applyforge/media`, so the preflight refused to start, and the start limit was then exhausted.
   - `backup.conf` would keep backing up the old database and media.
   - The restored database gets default PUBLIC rights.

   **Fixed** by a rename-based procedure (restored copy → canonical names; old ones kept as `*-pre-restore-*`), with `reset-failed`, the timer stopped and a rollback path. Rehearsed: 17/17.
2. **Medium – `host_check.py` failed every `git archive` release.** It reported the tracked `.env.example` templates as a `.env`. **Fixed:** only `.env` fails, the templates are allowed, and other `.env.*` files warn. Regression tests fail on the old code.
3. **Medium – `host_check.py` did not report a failed `applyforge.service`** (it was only INFO). **Fixed:** failed is now FAIL; a failed backup unit is WARN. Tests added.
4. **Medium – private-CA certificate profile undocumented.** Strict clients (Python 3.13+) rejected a CA without `keyUsage`, while curl accepted it. The runbook now specifies an RFC 5280 profile and an `openssl verify -x509_strict` check.
5. **Low – PostgreSQL port assumption undocumented.** The backup tool scrubs `PG*` and uses the default socket, so the cluster must be on 5432. Documented.
6. **Low – certificate check right after `nginx reload`** can still see the old certificate. The runbook now says to check with a new connection a few seconds later.

**Environment artifacts (not product defects).**

- WSL's pre-systemd `mount -a` fails on the NFS entry. Real hosts don't have that step; `mountFsTab = false` emulates them.
- With the NFS server in the same instance, a `hard` mount delays shutdown by 60 s unless it is unmounted first. A real NAS is separate, but note B4.
- WSL idle-stops instances and was suspended once (about 20 min) by the host.

## 3b. Network rehearsal: VPN, firewall, SSH (2026-11-21)

**Why not Hyper-V.** The workstation runs Windows 11 Home (edition ID `Core`), which has no Hyper-V management stack. With the owner's approval, the rehearsal used real **QEMU/KVM virtual machines** with nested virtualization, inside a new disposable WSL instance (`af84g-net-11210049f1ea`). No Windows firewall, Windows adapter or shared WSL network was changed.

**Topology.** Three Ubuntu 26.04 VMs (kernel 7.0, cloud image verified against Ubuntu's GPG-signed checksums) on a virtual LAN `10.20.0.0/24`:

- the bridge `br-lan` lives in a private network namespace with no uplink, no routes and no link to any other namespace;
- the VMs have no default route and cannot reach the internet;
- the VMs were controlled out of band through the QEMU guest agent and serial console, never over the network under test.

| VM | LAN | VPN | Role |
|---|---|---|---|
| server (2 vCPU, 1.5 GiB) | 10.20.0.10 | 10.99.0.1 (wg0, UDP 51820) | ApplyForge deployed with the runbook (release `222ccd3`), nginx with the repository site and its real placeholder values, PostgreSQL, sshd (keys only), nftables `deploy/nftables/applyforge.nft` |
| client (1 vCPU, 1 GiB) | 10.20.0.20 | 10.99.0.2 | the admin/VPN client: WireGuard enabled at boot, the private CA trusted |
| outsider (1 vCPU, 768 MiB) | 10.20.0.30 | — | the attacker: holds the **valid** admin SSH key and a WireGuard key the server does not know |

**Results (all fictional credentials).**

| Area | Result |
|---|---|
| WireGuard | handshake and ping client ↔ 10.99.0.1. The server lists exactly one peer. The outsider's unknown key gets no handshake and no connectivity. |
| Firewall | inbound `policy drop` loaded from `/etc/nftables.conf`. The nftables drop counter climbed past 196,000 packets during the scans. Forwarding: drop. |
| SSH | through the VPN: key login and `sudo` work. Password login and root login are refused ("Permission denied (publickey)"). From the LAN (client or outsider, **even with the valid key**): timeout. |
| HTTP/HTTPS | through the VPN: health 200, SPA 200, HTTP→301, `/admin` 404, API 401 without the token and 200 with it, collector POST 403, HSTS. From the LAN: dropped (curl exit 28: timeout, not refused). With the VPN down: no access. |
| Routing trick | the outsider routing `10.99.0.0/24` via the server's LAN address: 22 and 443 on `10.99.0.1` dropped. The rule matches `iifname "wg0"`, not just the address. |
| Port scans (TCP 1–65535, completed) | outsider → server LAN: **0 open** (65,535 filtered). VPN client → server LAN address: **0 open**. VPN client → 10.99.0.1: **exactly 22, 80, 443 open**, 65,532 filtered. UDP: every probed port is `open|filtered`, because a silent drop leaves nmap nothing to distinguish. |
| Reboot persistence | admin reboot over SSH, client reboot and cold power cycle: everything came back on its own. The firewall loaded before WireGuard and nginx (5.1 s, 7.7 s, 7.9 s). HTTPS through the VPN answered 4 s after an admin reboot and 17 s after a cold start. The client tunnel returned within about 2 s. |
| Firewall change safety | `nft -c` rejected a typo and left the live rules untouched. A real lock-out (it also dropped the WireGuard port) applied **with** the rollback timer recovered automatically after 64 s and 87 s (two runs). Confirming a good change by cancelling the timer works. A lock-out **without** a timer was recovered through the serial console (afterwards SSH worked within 1 s) and, separately, by a hypervisor reset (the saved rules loaded at boot). |

**Defects found and fixed.**

1. **High – nginx did not survive a reboot.** nginx and `wg-quick@wg0` both start after `network-online.target`. nginx started first, failed with `bind() to 10.99.0.1:443 failed (99: Cannot assign requested address)`, and stayed down: the site was unreachable after reboot until a manual restart. **Fixed** by `deploy/systemd/nginx-applyforge-vpn.conf` (nginx `After=`/`Wants=wg-quick@wg0.service`), installed in runbook section 6. Verified across three reboot types.
2. **Medium – the host check could not see the ordering problem.** It now fails when an enabled `wg-quick@` instance exists and nginx is not ordered after it. A first version used `list-unit-files`, which never lists enabled template instances; the real VM caught that, and it is fixed and tested. Regression tests fail on the committed code.
3. **Medium – backup test suite (introduced in 8.4e).** The "age installed" skip decorator sat on the wrong class. Without `age`, the 11 `probe-destination` tests were silently skipped and `RealAgeTests` crashed; this showed up in the fresh VM-host instance. Fixed, with a guard test (`SuiteStructureTests`).
4. **Low – host check false warning.** `systemd-analyze verify` echoes Ubuntu 26.04's notes about its own units (`system-xfs_scrub.slice: CPUAccounting= removed`). Only lines about the verified unit now count. Tested.
5. **Documentation.** Runbook section 14 (new):
   - the WireGuard setup;
   - the firewall ruleset and its installation;
   - the change procedure with an automatic rollback timer;
   - console recovery;
   - boot order, including the 120 s boot stall when netplan configures no DNS server (`systemd-networkd-wait-online --dns`), which the rehearsal hit first.

   `deploy/nftables/applyforge.nft` is new, guarded by `deploy/tests/test_firewall_ruleset.py`. `nft -c` accepted the repository copy on the server, and its rules are identical to the ones loaded there.

**Rehearsal-harness errors (corrected; not product defects).**

- A first lock-out ruleset had a syntax error, so nothing was locked out. The test now refuses to run unless the lock-out file passes `nft -c`.
- Two full scans hit a time limit before completing. They now use a minimum rate and must print nmap's completion line.
- `pipefail` masked a correct SSH refusal message.

**Not covered.** The real provider network, router and NAT, the public interface and the real VPN endpoint. The Hyper-V virtual switch (not available). IPv6 reachability beyond the rules. A real provider console. Clock synchronisation (the VMs had no NTP).

## 3c. Two-server rehearsal: private DNS, DNS-01, sealed backups, monitoring, recovery (2026-11-21)

> **Everything in this section ran in disposable VMs** with fictional zones, keys and data. Nothing was verified on real infrastructure, real DNS providers, Let's Encrypt, Healthchecks.io or Hetzner.

**Environment.**

- A new WSL instance, `af84h-2srv-112103471598`, running four QEMU/KVM VMs (Ubuntu 26.04, kernel 7.0) on a private, routeless bridge (`10.20.0.0/24`).
- The VMs were controlled out of band through the guest agent; no Windows or shared-network change.

| VM | Role |
|---|---|
| **app** 10.20.0.10 | ApplyForge (release `431902b`, runbook 2–8); `wg0` 10.99.0.1 (devices); `wg1` 10.98.0.1 (backup link); unbound on 10.99.0.1; lego + deploy hook; monitoring timers; NFS client |
| **backup** 10.20.0.11 | admin `wg0` 10.97.0.1 (admin device only); `wg1` 10.98.0.2; NFSv4.2 export to 10.98.0.1 only (`sync`, `root_squash`); root-only vault; seal timer; heartbeat |
| **client** 10.20.0.20 | admin device: tunnels to both servers, split DNS, the "offline machine" for verification |
| **inet** 10.20.0.40 | the simulated internet: public DNS (BIND, fictional `af84h.test` + validation zone `af84h-acme.test`), Pebble ACME CA, mock Healthchecks receiver, attacker vantage point |

**Results.** Final runs; the first runs' failures are explained under defects.

| Area | Result |
|---|---|
| **U12 private DNS** | 30+ checks passed. |
| | Public DNS has no address for the app hostname (NOERROR/NODATA), only the `_acme-challenge` CNAME. |
| | The VPN resolver answers through `wg0` only; queries from the LAN and the routing trick are dropped. |
| | Laptop split DNS: the app name resolves via the VPN; public names are unaffected when the tunnel or resolver is down. |
| | Route-all clients (phones) lose all DNS while the resolver is down. Recovery by hosts entry, unbound restart or tunnel off works. |
| | unbound restarts itself after a crash. Cached answers survive tunnel-down until their TTL expires. |
| **DNS-01 (Pebble)** | Issuance through the CNAME into the validation zone, with the credential only in the environment. |
| | Scope: TXT in the validation zone allowed; A records in the owned zone and non-TXT records in the validation zone refused. |
| | The served certificate is trusted by the client without `-k`, and TXT records are cleaned up. |
| | Forced renewal installs and serves the new serial. A broken nginx config makes the hook roll back (the old certificate keeps serving) and fail, which alerts. |
| **U14 isolation** | 36 checks. The export is reachable only from 10.98.0.1 on `wg1`: dropped from the LAN, the admin tunnel and the internet; port 111 dropped. |
| | The vault and `/srv` cannot be mounted. Root on the app is squashed and cannot chown. |
| | With the **admin SSH key in hand**, the app server still cannot SSH to the backup server; of 20 likely ports, only 2049 is reachable on the link address. |
| | **Compromise:** the app server *can* delete and overwrite archives on the export. The vault copies kept their recorded hashes, and the next seal run alerted (exit 1). |
| **Backup server down** | The current unit **hung** (still running after 70 s; it would end only at 6 h, with no alert). With the new fast-fail pre-check it failed in **31 s**, ApplyForge stayed up, and the `/fail` ping was sent. |
| | When the server returned, the `hard` mount resumed without a remount and the backup succeeded. |
| **Reboots** | Backup server: NFS server after `wg1`; export served; backups resume. |
| | App server: NFS mounted at boot after `wg1`; boot order nftables → `wg0`/`wg1` → unbound → mount → nginx → ApplyForge; DNS answers; backups work. |
| **U16 monitoring** (mock receiver) | All 8 checks up. |
| | Detected failure **and recovery** for: application stopped (health), resolver stopped (dns, while health stayed up), disk at 90 %, certificate inside its threshold. |
| | Seal tamper → alert. Missed heartbeat with the backup server powered off; **all app checks went late with the app server powered off**, so monitoring is independent of it. Backup age → late alert. |
| | 20+ simulated notifications. |
| **Independent verification** | The admin device copied the hash record (via the app's `wg0`) and the newest sealed archive (via the backup's admin `wg0`). Verification passed with the primary **and** escrow identity. |
| | A vault copy that had been altered *before* it was sealed was **refused** by the trusted hash from the offline record. |
| **Restore drill** | The archive the "attacker" deleted from the export was recovered from the vault: restore 1.3 s; data and media identical. |
| **App server lost** | VM killed and **disk deleted**, then rebuilt from the image with the server's WireGuard keys from the "password manager". |
| | Devices and the backup link worked **without re-enrolment**; NFS mounted at boot. |
| | Data restored from the backup server using the **offline** hash record (the index died with the server), then switched. Data and media identical; backups resumed. |
| | The API token is new (users re-enter it); the SSH host key changed (admins re-pin it). |

**Recovery time for application-server loss**, small fictional dataset, nested VMs, packages from the internet:

| Phase | Time |
|---|---|
| Rebuild (image, packages, wheels, runbook 2–8) | 328 s |
| First boot on the network | 25 s |
| Configuration from the password manager | 21 s |
| Certificate | failed first (clock skew); about 10 s after the clock was aligned |
| Data restore | 1.4 s |
| Switch + health | 74 s (inflated: the health check waited on the failed certificate) |
| **Total** | **≈ 8 minutes**, plus human decision time and cloud provisioning |

Real-data timings remain U11.

**Defects found and fixed** (each with a regression test or a runbook change):

1. **High – seal skipped freshly published archives silently.** The NFS server holds a client delegation (lease) right after publication, so the server-side open returned `EAGAIN`. The seal skipped the archive with exit 0, so it stayed unprotected and an attacker could alter it before the next run. In the first run, the vault really did seal an altered copy.

   **Fixed:** retry for about 30 s, and fail (alert) on any archive-like file that cannot be sealed. Two tests added, plus mutant E6.
2. **High – the backup unit hung for up to 6 h when the backup server was unreachable** (`hard` NFS mount), with no alert. **Fixed:** an `ExecStartPre` fast-fail check (`timeout -s KILL 30 stat -f`) in `deploy/systemd/applyforge-backup.service`.
3. **High – Ubuntu 26.04's AppArmor `wg-quick` profile denies `resolve1.RevertLink`.** A client `PreDown = resolvectl revert` therefore failed, wg-quick aborted before deleting the interface, the tunnel could not be stopped, and the next start failed ("already exists"). **Fixed** in runbook 15.1: no `PreDown`. Verified on a clean boot.
4. **Medium – `unbound-checkconf` fails on a fresh install** until `/var/lib/unbound/root.key` exists. **Fixed** in runbook 15.1 (`unbound-anchor` first).
5. **Medium – `applyforge-acme renew` could not be forced:** `acme.env` overrode `ACME_RENEW_DAYS`. **Fixed** with `renew --days N`. Tested.
6. **Medium – DNS-01 with TSIG fails at clock skew above 5 minutes** ("bad authentication"); the rebuilt VM was 1.8 h off. **Documented:** check NTP before issuing (runbook 15.8 step 3).
7. **Low – rehearsal-fidelity items, not product defects:**
   - an authoritative-only "internet" DNS cannot chase the validation CNAME; real validators recurse;
   - unbound blocks the special-use `.test` TLD;
   - Pebble forgets accounts on restart;
   - commands run through the guest agent cannot make privileged D-Bus calls, so the harness used `systemd-run`;
   - concurrent boots on a busy host stalled one VM once.

**Single points of failure (assessment).**

| Component | Failure effect | Mitigation now | Residual |
|---|---|---|---|
| App server (one instance) | service down | rebuild + restore (≈ 8 min tool time, rehearsed); monitoring alerts | the RTO depends on provisioning; there is no standby |
| App server **public IP** | rebuild with a new IP breaks every client `Endpoint` and the backup server's firewall rule | **keep the Hetzner Primary IP when rebuilding** (it can be detached and reassigned) | unverified on Hetzner (D) |
| Private resolver (on the app server) | app name unresolvable | split DNS on laptops; hosts-file fallback; `dns` check | phones lose DNS while the tunnel is up |
| Server identity secrets (WireGuard keys) | lost → every device and the backup link must be re-enrolled | kept in the password manager (15.5) | the password manager itself |
| Offline hash record + age identities | both identities lost → archives unrecoverable; record lost → restores need an untrusted hash | the escrow key on paper; monthly offline record (15.4) | owner discipline |
| Backup server | no new backups (alerts); vault lost | Hetzner Backups of the backup server; monthly offline copy | up to 1 month of history if both are lost |
| Provider account (both servers) | lockout or compromise affects everything | 2FA, recovery codes offline, monthly offline copy | provider-level incident |
| Healthchecks.io | silence instead of alerts | none (external) | accepted; check the dashboard occasionally |
| Clock (NTP) | DNS-01 and TLS failures | host check warns; runbook 15.8 | add an NTP check to monitoring (backlog) |
| Admin devices | lockout | two enrolled devices; console (15.7) | — |

## 4. Verified requirements

| # | Requirement | Evidence |
|---|---|---|
| V1 | The runtime installs wheel-only and reproducibly on Ubuntu 26.04 / Python 3.14 | pinned install, `pip check`, 557 tests |
| V2 | psycopg2 connects to PostgreSQL 18.6 | read-only connection test |
| V3 | nginx config is valid and enforces: VPN allowlist, TLS-only with HTTP 301 to a fixed host, unknown Host → 444, unknown SNI rejected, `/admin` and `/media` 404 (including encoded and `//` variants), dotfiles 404, collector/generation writes 403 (11 bypass variants and methods), 12 MiB body limit, security headers, one HSTS header, forwarded headers overwritten | stack test 61/61 |
| V4 | Gunicorn binds 127.0.0.1 only; its access log shows the real client | stack test |
| V5 | `check --deploy` reports only the intentional W005/W021 | stack test |
| V6 | The app unit's sandbox works with Gunicorn `Type=notify` and denies what it should | transient-unit test |
| V7 | The backup unit's sandbox and capability set support `runuser`/PAM, peer-auth `pg_dump`, D-Bus `systemctl`, the lock, `age` and other-owner media reads | transient-unit test + negative control |
| V8 | Maintenance window and recovery: service stopped and restarted; SIGKILL recovered by `ExecStopPost`; SIGTERM handled; inactive service left inactive; nothing partial published | systemd test 26/26 |
| V9 | A timed-out `pg_dump` behind `runuser` is killed, not orphaned (**defect found and fixed in 8.4e**) | reproduction + regression test + mutant |
| V10 | NFSv4.2 and SMB 3.1.1 client semantics satisfy the tool's publication model (O_EXCL, fsync, read-back, inode stability, no-clobber hard link, O_NOFOLLOW, delete) | `probe-destination` |
| V11 | The RENAME_NOREPLACE fallback works where hard links don't | vfat probe |
| V12 | Encryption, verification and restore with real age and PostgreSQL | 8.4d real-age suite and rehearsal |
| V13 | The host check never reads secrets and runs only read-only commands | dedicated tests |
| V14 | The runbook deploys a release from scratch on a fresh Ubuntu 26.04 instance, and `host_check.py` passes there | dress rehearsal (3a) |
| V15 | First backup through the real unit with the real `Type=notify` app: about 1 s client-visible outage; offline verification by a separate account with primary and escrow identities | dress rehearsal |
| V16 | Restore → validate → rename-based switch → backups capture the live data; rollback paths for the switch and for code | dress rehearsal |
| V17 | Services and the NFS backup mount recover after an unplanned restart | dress rehearsal |
| V18 | WireGuard + nftables default-deny: only UDP 51820 is reachable from outside; SSH/HTTP/HTTPS only through `wg0`; full-range scans confirm it | network rehearsal (3b) |
| V19 | Key-only SSH administration through the VPN; password and root login refused; the valid key alone is not enough from the LAN | network rehearsal |
| V20 | Reboot and cold-start persistence of the firewall, VPN, nginx (with the ordering fix) and ApplyForge | network rehearsal |
| V21 | Safe firewall change: syntax check, automatic rollback timer, confirmation, serial-console and reset recovery | network rehearsal |
| V22 | Backup isolation: the app server can only write to its export; it cannot administer the backup server or reach the vault; archives deleted or altered through it survive in the vault and trigger an alert | two-server rehearsal (3c), in VMs |
| V23 | The backup unit fails fast (31 s) when the backup server is unreachable; the mount resumes on return; both servers survive reboots | two-server rehearsal, in VMs |
| V24 | Application-server loss: rebuild with restored identity, restore from the backup server with the offline hash record, no device re-enrolment (≈ 8 min tool time) | two-server rehearsal, in VMs |

## 5. Unverified requirements (need the real host or real infrastructure)

| # | Requirement | How it will be verified |
|---|---|---|
| U1 | Real server sizing, disks, kernel and Ubuntu 26.04 install | `host_check.py` |
| U2 | The **production** VPN: provider network, NAT/port forwarding to UDP 51820, real client devices and their keys, nginx `allow` lines matching. Configuration and behaviour were rehearsed in VMs (3b) | host check + section 8.1 client checks from a real VPN client |
| U3 | The firewall on the **production host and its real public interface** (the ruleset and procedures were rehearsed in VMs, 3b); provider-level firewall or security groups; a provider console that works | host check + an authorized external scan from outside the VPN |
| U4 | Private DNS name, certificate from the private CA, client trust, expiry monitoring | host check (expiry, host name) + client checks |
| U5 | Production PostgreSQL: roles, `pg_hba`, peer/`pg_ident`, PUBLIC revocation, connection limits, the restore tool's identity and `pg_shdepend` queries | host check + one authorized restore drill on the host |
| U6 | The real `applyforge.service` inside the backup maintenance window **on the production host**, and shutdown behaviour. Rehearsed locally: the window (about 1 s outage) and failed starts with `reset-failed` recovery | first authorized backup run + journal review |
| U7 | The real backup storage: export options, root squashing, snapshots, capacity, durability promises of the NAS, behaviour when unreachable | `probe-destination` + a full backup → offline verify → restore drill |
| U8 | `RequiresMountsFor` / automount on the real mount | first backup run |
| U9 | Monitoring and alerting (unit failures, health, disk, certificate) | operator setup (runbook 13.2) |
| U10 | Offline age key custody and the offline copy of the hash index | operator procedure (runbook 12.2, 12.4) |
| U11 | Restore and switch time on real data volumes (the RTO). Rehearsed: about 11 s for a tiny fictional dataset, which says nothing about real volumes | restore drill on the production host |
| U12 | **Rehearsed in VMs (3c), not production-verified:** split DNS. unbound on 10.99.0.1, answering only on `wg0`; the extra nftables rules for port 53 on `wg0`; WireGuard client `DNS =` on each real device type; behaviour with the tunnel up and the server down (runbook 15.1) | VM rehearsal, then each real device |
| U13 | **Rehearsed with Pebble (3c); the real Let's Encrypt and your DNS provider are untested:** Let's Encrypt DNS-01 with CNAME delegation to a validation zone and a zone-scoped token; renewal timer; deploy hook (atomic install, `nginx -t`, reload, delayed verification) (15.2) | staging issuance, then production issuance (authorized) |
| U14 | **Rehearsed in VMs (3c), not production-verified:** the backup server: `wg1` link, NFSv4.2 over WireGuard across regions, its own admin `wg0`, nfs-server ordered after `wg1`, its firewall, Hetzner Backups as storage-side history, capacity (37 × archive size × 1.3 + 2 GB) (15.3) | VM rehearsal of the two-server layout, then production (authorized) |
| U15 | **Offline copy and both-identity verification rehearsed (3c); the quarterly routine is untested:** the monthly offline copy and verification from an admin device (15.4); the quarterly key-access test and the escrow restore (15.5) | first month of operation |
| U16 | **Rehearsed against a mock receiver (3c); the real Healthchecks.io is untested:** Healthchecks.io checks and timers on both servers (`ExecStartPost=`/`OnFailure=` for backups); alert delivery (15.6) | VM rehearsal with a dummy endpoint, then a real account (authorized) |
| U17 | **Partly rehearsed (WireGuard-down recovery steps, rebuild with restored keys); the provider console and 2FA are untested:** emergency access: the provider console and 2FA, the second admin device, WireGuard-down recovery including the time-boxed SSH exception (15.7) | console test on the real servers before the first firewall change |

## 6. Host and PostgreSQL requirements

**Host.**

- 2+ vCPU and 2+ GiB RAM; 4 GiB is comfortable.
- Free space: 5+ GiB for `/`, `/srv` and `/var/lib/postgresql`, plus room in `BACKUP_WORK_DIR` for about twice (dump + media).
- NTP synchronised; unattended security updates or a monthly patch window.
- Accounts: `applyforge` and `applyforge_restore` are system accounts with `nologin`, no home directory and no privileged groups.

**PostgreSQL** (validated on 18.6; 14 or later is required):

- **Network.** `listen_addresses = 'localhost'` and `password_encryption = scram-sha-256`. In `pg_hba.conf`, use `local all all peer` for administration, backups and restores, plus `host applyforge applyforge 127.0.0.1/32 scram-sha-256` (and `::1/128`) for the app. No `trust` rules and no wide `host` rules.
- **Roles.**
  - `applyforge` is a LOGIN role without SUPERUSER, CREATEDB, CREATEROLE, REPLICATION or BYPASSRLS, and owns the database.
  - `applyforge_restore` is LOGIN only, with no grants and no memberships, and owns nothing in production.
  - No `pg_ident` maps are needed. If maps exist, the restore tool's identity check detects unexpected mappings.
- **Hardening.** `REVOKE CONNECT, TEMPORARY ON DATABASE applyforge FROM PUBLIC`, after checking that no monitoring role relies on it. Keep CONNECT on `postgres` for the restore role, and leave `template0`/`template1` unchanged. PostgreSQL 15+ already denies CREATE on schema `public` to PUBLIC.
- **Resources.** ApplyForge uses up to about 8 connections (one per Gunicorn thread, `conn_max_age=600`) plus 1–2 during management commands and backups, so the default `max_connections = 100` is ample. Optionally set `ALTER ROLE applyforge CONNECTION LIMIT 20` as a guard. Keep `data_checksums` on (the Ubuntu 26.04 default).
- **Monitoring.** Watch the journal for authentication failures and connection-limit errors, and watch disk usage of `/var/lib/postgresql`.
- **Recovery.** Use logical dumps (`pg_dump` custom format) through the backup tool, restored into a new database by the isolated role. The RPO is 24 hours. Add WAL archiving (PITR) only if a smaller RPO is required later.

## 7. Backup storage and DR assessment

| Option | Fits the tool's model? | Evidence | Assessment |
|---|---|---|---|
| **NFSv4.1+, `sync` export, mounted `hard`** | yes: O_EXCL, fsync, hard-link publication, stable inodes | probe passed (root_squash and not) | **Recommended.** Restrict the export to the server's address. Prefer storage-side snapshots. Use `sec=krb5p` if the path is not private. |
| SMB 3.1.1 (`seal`, `hard`, `serverino`) | yes on Samba with a POSIX backend | probe passed; the cifs client defaults to `soft`, and ownership/modes are forced by mount options | Acceptable if NFS is unavailable. The probe must pass on the actual server (Windows and NAS servers vary). |
| sshfs | depends on the SFTP server's extensions; FUSE | not tested | Not recommended. |
| Local disk / second disk | yes | ext4 probe passed | **Not a backup**: it shares the server's fate. Acceptable only as a staging copy. |
| Object storage (S3 with object lock) | not with this tool (needs a POSIX mount) | — | A good second copy. A future option: copy the published archives off-host with a separate, write-only credential. |

**Failure behaviour.**

- A `hard` NFS mount blocks I/O while the server is away. The backup then waits up to `TimeoutStartSec` (6 h); the service stays stopped only during the capture, which comes before any I/O to the share. After the capture, ApplyForge is already running again.
- A `soft` mount can turn a server hiccup into a short write. The tool would then detect it (read-back hash, identity) and publish nothing, but the run fails, so `hard` is preferred.

**Monitoring and retention.**

- The unit exit codes cover failures (runbook 13.2). Add a "no successful backup in 36 hours" alert.
- Retention: 30 days, with the 7 newest always kept. Storage-side snapshots add protection against a compromised server deleting archives.

**DR objectives.** RPO 24 h. RTO a few hours (rebuild per runbook 12.8), still to be measured in a drill (U11).

## 8. Remaining blockers (prioritized)

1. **B1 – No production host.** Provision Ubuntu 26.04 per runbook sections 2–3. Then run `host_check.py` until there are no `FAIL` lines.
2. **B2 – Production network access controls.** Apply the rehearsed configuration (runbook section 14) on the real host: WireGuard, `deploy/nftables/applyforge.nft` with the rollback timer, the nginx drop-in, and private DNS. Verify the provider console and console password **before** the first firewall change. Then run an authorized scan from outside the VPN (U2, U3).
3. **B3 – TLS and DNS.** Planned, and rehearsed in VMs with Pebble (3c): a domain-owned hostname, split DNS (no public A record), Let's Encrypt DNS-01 with a zone-scoped token and automated renewal (U4, U12, U13). Alternative, already verified: a private CA.
4. **B4 – Backup storage.** Planned, and rehearsed in VMs (3c): a separate backup server in another country, NFSv4.2 over a dedicated WireGuard link (`wg1`), `sync`/`hard`/`root_squash`, Hetzner Backups as storage-side history (U14). The final disk size waits for the first real archive. Run `probe-destination`. Then do the first backup, offline verify and restore drill (U7, U8, U11). Also test the shutdown/reboot behaviour of the `hard` mount with the real NAS (the rehearsal saw a 60 s shutdown delay when the NFS server went away first).
5. **B5 – Key custody.** Generate the age key pair offline with a second (escrow) recipient. Store the private keys offline, and keep the offline hash record (U10).
6. **B6 – Production PostgreSQL hardening and drill.** Create the roles and `pg_hba`, revoke PUBLIC, and run a restore drill on the host (U5).
7. **B7 – First real maintenance window.** Run the first backup with ApplyForge running and review the journal (U6).
8. **B8 – Monitoring and alerting.** Planned, and rehearsed against a mock receiver (3c): Healthchecks.io dead-man's-switch pings with email alerts, from both servers (U9, U16).
9. **B9 – Source control.** The 8.4e work, the dress-rehearsal fixes (`222ccd3`) and the network-rehearsal fixes are committed locally. Pushing them needs separate authorization. **Use only the corrected 12.7 procedure** for any switch to restored data.

## 9. Authorizations needed

Each item is needed **at the time it is executed**; none is needed to keep working locally.

| Action | Target | Impact / risk | Rollback |
|---|---|---|---|
| Commit the 8.4e changes (listed in the final report) to `main` | local repository | none outside the repository | `git reset --soft HEAD~1` before pushing |
| Push the local commits | the remote repository | publishes code (no secrets included) | revert commits |
| Provision the production host and install packages | new server | new system; no existing data | rebuild |
| Create the production PostgreSQL roles and database, revoke PUBLIC | production cluster | new, empty database | drop the database and roles |
| Configure the firewall, VPN and DNS | production network | may cut access if wrong | console access; revert rules |
| Mount the backup share and write the marker | production storage | writes one marker file | unmount; delete the marker |
| Run `probe-destination` on the real share | production storage | writes and deletes its own probe files | none needed |
| Start and enable `applyforge.service`; reload nginx | production host | goes live on the VPN | `systemctl disable --now applyforge` |
| First backup, then enable `applyforge-backup.timer` | production host + storage | stops ApplyForge for seconds to minutes | `systemctl disable --now applyforge-backup.timer` |
| Restore drill into a new database | production host | creates a new database and directory only | drop them |

## 10. Go/no-go checklist for the first deployment

- [ ] `sudo python3 -I $REL/deploy/host_check.py --release $REL` shows no `FAIL` lines, and every `WARN` has been reviewed.
- [ ] `sudo applyforge-manage $REL check --deploy` shows only W005 and W021.
- [ ] `systemd-analyze security applyforge.service` reports about 1.2.
- [ ] The runbook 8.1 client checks pass from a VPN client; nothing is reachable from outside the VPN.
- [ ] `applyforge-backup probe-destination` passes on the real share.
- [ ] The first backup is created, its hash is copied offline, it is verified offline with `--expected-sha256`, and restored into a new database and directory, then validated.
- [ ] The timer is enabled only after that, and alerts are configured.
- [ ] The age private keys and the hash record are stored offline; the escrow recipient is tested.
- [ ] The rollback plan is ready: the previous release is kept (none on the first deployment), with `systemctl disable --now applyforge` as the stop.

## 11. Security review summary (8.4e)

- **Fixed:** a timed-out `pg_dump` could keep running after a failed backup, holding locks and load (V9).
- **Hardened:** the backup unit now applies `CapabilityBoundingSet` and `SystemCallFilter=@system-service`.
- **New tooling is read-only or self-contained.**
  - `host_check.py` never opens secrets, and runs SQL in read-only sessions under validated identifiers.
  - `probe-destination` writes only its own random-named directory, under the backup lock, and removes it.
- **No secrets were used.** All tests used fictional values, throwaway keys and disposable resources. Protected files and retained rehearsal resources were not touched.
- **Accepted, documented risks:**
  - a single static API token;
  - `/admin` blocked at nginx only;
  - local users can reach Gunicorn on loopback (the host has no untrusted local users);
  - archive integrity relies on the trusted hash records.
