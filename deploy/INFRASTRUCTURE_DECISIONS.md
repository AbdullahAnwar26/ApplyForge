# ApplyForge production infrastructure plan

Status: **design rehearsed in disposable VMs (two-server rehearsal, 2026-11-21, `PRODUCTION_READINESS.md` 3c); NOT verified on real infrastructure.** Nothing has been ordered, registered, provisioned or configured. The rehearsal used fictional zones (`.test`), Let's Encrypt's test server (Pebble) and a mock Healthchecks receiver.

What is already **verified** (rehearsals):

- the deployment, backup, restore and switch procedures (`PRODUCTION_READINESS.md` 3a);
- WireGuard, the nftables default-deny ruleset, SSH through the VPN, reboot persistence and firewall rollback/console recovery (3b).

Runbook section 15 holds the procedures, rehearsed in VMs. Section D lists what only real infrastructure can confirm.

## Decision register

### A. Engineering decisions (recommended; baseline accepted by the owner)

| ID | Decision |
|---|---|
| A1 | **Hetzner Cloud**, x86-64 (amd64: the validated architecture; Arm `CAX` plans are excluded until revalidated), Ubuntu 26.04 LTS |
| A2 | **App server:** 4 vCPU / 8 GB / ≥ 80 GB. **CX33** if orderable, otherwise **CPX32**. Region: **Helsinki (HEL1)** unless the latency test (C1) says otherwise |
| A3 | **Backup server:** the smallest amd64 plan (**CPX02**, or CX23), in a **different country**: **Falkenstein (FSN1)** if the app is in Helsinki (Helsinki if the app is in Germany) |
| A4 | **Provider firewalls.** App server: inbound UDP 51820 from anywhere, nothing else. Backup server: UDP 51821 from the app server's public IPs only, plus UDP 51820 for admin devices; nothing else |
| A5 | **WireGuard on the app server** (`wg0`, 10.99.0.0/24), one key per device, SSH/HTTP/HTTPS only on `wg0` (verified design, runbook 14) |
| A6 | **Backup link** `wg1`, 10.98.0.0/30: app server `.1` = NFS client, backup server `.2` = NFS server. **Admin access to the backup server is separate** (its own `wg0`, admin devices only), so a compromised app server cannot log in to it. *Rehearsed: blocked even with the admin key in hand.* |
| A7 | **NFSv4.2**, export `sync,root_squash,no_subtree_check` to `10.98.0.1` only; client mount `hard`; NFS (2049) accepted only on `wg1` |
| A8 | **Storage-side protection:** the export is only a drop box. `applyforge-backup seal` (hourly) copies finished archives into a **root-only vault that is not exported** and alerts on later changes. *Rehearsed: archives deleted or altered through the app server survived in the vault.* Hetzner automatic Backups of the backup server (7 daily slots) add history. Archives live on the server's **own disk**, because Hetzner Backups do not include Volumes |
| A9 | **Retention baseline:** 30 days + 7 always kept (`BACKUP_MIN_KEEP`), i.e. up to **37 archives** |
| A10 | **TLS:** Let's Encrypt via **DNS-01**, renewed on the app server by a systemd timer, deployed with an `nginx -t` / reload / delayed-verify hook. The private CA remains the documented alternative (runbook 2) |
| A11 | **Split DNS:** **no public A/AAAA record** for the app hostname; public DNS carries only the ACME challenge CNAME. VPN clients resolve the name through unbound on `10.99.0.1`, answering only on `wg0`, with a TTL of 300 s. Laptops route only the app name to it (`PostUp` resolvectl, **no `PreDown`**: Ubuntu 26.04's AppArmor blocks it); phones use `DNS =`. *Rehearsed* |
| A12 | **Narrow DNS credential:** `_acme-challenge.<app-host>` is a **CNAME into a dedicated validation zone**; the API token can edit only that zone. The server never holds a token for the main domain |
| A13 | **Keys:** age primary identity in the owner's password manager; age escrow identity on paper in a separate physical location; neither on the server, in Git or in ordinary backups. Key-access test quarterly; full restore with the escrow key yearly |
| A14 | **Monitoring:** Healthchecks.io free tier, email alerts, dead-man's-switch pings from systemd timers on **both** servers. Being external, it stays independent of the app server |
| A16 | **Backup unit fails fast** (30 s `stat -f` pre-check) when the backup server is unreachable, instead of hanging on the `hard` mount for up to 6 h. *Rehearsed: failed after 31 s, alert sent, app unaffected* |
| A17 | **Keep the app server's public IP across rebuilds** (Hetzner Primary IP, kept and reassigned). Otherwise every device `Endpoint` and the backup server's firewall rule must change |
| A18 | **NTP must be synchronised** before issuing certificates. *Rehearsed: DNS-01 with TSIG failed at 1.8 h skew* |
| A15 | **Emergency access:** provider account 2FA (TOTP or a security key/passkey, where Hetzner supports it), recovery codes offline, console verified before the first firewall change, a second enrolled admin device, a break-glass sheet |

### B. Values the owner must supply

| ID | Needed | Used for |
|---|---|---|
| B1 | Your usual location, or a latency test result (C1) | region choice A2/A3 |
| B2 | A domain you own (or approval to register one) and its DNS provider (must have an API) | A10–A12 |
| B3 | The app hostname (for example `applyforge.<domain>`) and the name of the validation zone (A12) | A11, A12 |
| B4 | The devices that get VPN access (at least **two**: a primary and a spare/emergency device), and other users if any | A5, A15 |
| B5 | Password manager; physical location (and keeper, if any) of the escrow key and recovery codes | A13, A15 |
| B6 | Alert email address | A14 |
| B7 | Expected data volume (resumes, applications), to confirm by measuring the first real archive | backup disk size |
| B8 | Who owns the Hetzner account and its billing (you create it; I never handle payment or credentials) | A1 |

### C. Items requiring your formal authorization (each separately, when reached)

| ID | Action |
|---|---|
| C1 | Latency test from your location to the candidate regions. Optional and read-only (e.g. ping or download Hetzner's public speed-test hosts) |
| C2 | Creating the Hetzner account and projects; ordering the two servers; enabling Backups |
| C3 | Registering a domain or changing DNS: the validation zone, the CNAME, the API token |
| C4 | Generating production keys: age (on your computer), WireGuard (per device), SSH |
| C5 | Applying the provider firewalls and the server nftables rules (with the rollback timer) |
| C6 | First production deployment (runbook 2–8) and certificate issuance |
| C7 | Backup link, NFS export and mount; `probe-destination`; first backup; offline verification; restore drill |
| C8 | Enabling `applyforge-backup.timer`, the monitoring timers and the healthchecks.io account |
| C9 | Pushing the local commits to GitHub |

### D. Verifiable only on real production infrastructure

| ID | Item |
|---|---|
| D1 | Actual plan availability and prices (CX33 currently shows "unavailable"); VAT |
| D2 | Real latency and bandwidth between you, the app region and the backup region |
| D3 | Provider firewall behaviour, including the scan from outside (rehearsed only in VMs) |
| D4 | DNS-01 issuance and renewal with your DNS provider; CNAME delegation; token scope |
| D5 | Split DNS on each real device (Windows, Android, iOS, macOS), and behaviour when the tunnel is down |
| D6 | NFS over `wg1` across regions: latency, `hard`-mount behaviour during an outage, snapshot/Backups restore |
| D7 | Real archive size and backup/restore duration (the RTO), hence the final disk size |
| D8 | Hetzner console, rescue system and 2FA recovery path |
| D9 | Healthchecks.io delivery and alert timing |
| D10 | Keeping and reassigning the Hetzner Primary IP when rebuilding the app server (A17) |
| D11 | NTP synchronisation on both servers; the real ACME/DNS provider tolerating normal skew |
| D12 | The real DNS provider's API credential scope (TXT-only, one zone) behaving like the rehearsed TSIG policy |

---

## 1. Hosting: region comparison and plan

Hetzner prices, read 2026-11-21 from hetzner.com/cloud (regular-performance, cost-optimized, general-purpose pages) and docs.hetzner.com/cloud/general/locations. **Estimates, excluding VAT, prices include a primary IPv4.** Confirm everything in the Hetzner Console configurator before purchase.

| Plan (4 vCPU / 8 GB) | Disk | Helsinki HEL1 | Nuremberg NBG1 | Falkenstein FSN1 | Singapore SIN1 | Included traffic |
|---|---|---|---|---|---|---|
| **CX33** (shared, cost-optimized) | 80 GB | €8.99 | €8.99 | not listed | not offered | 20 TB (EU) |
| **CPX32** (shared AMD, regular perf.) | 160 GB | **€30.49** | €35.99 | not listed | **€49.49** | 20 TB (EU), **2 TB (SIN)** |
| CCX (dedicated) | — | no 4 vCPU / 8 GB plan; the nearest is CCX13 (2 vCPU / 8 GB) at €36.99 HEL1 / €43.49 NBG1 | | | | |

| Small plans (backup server) | Disk | HEL1 | NBG1 | FSN1 | SIN1 |
|---|---|---|---|---|---|
| CPX02 (1 vCPU / 1 GB) | 20 GB | €5.49 | €6.49 | €6.49 | €7.99 (0.5 TB traffic) |
| CX23 (2 vCPU / 4 GB) | 40 GB | €5.99 | €5.99 | — | not offered |

**Other prices.**

- Snapshots: €0.0143 per GB-month.
- Automatic Backups: from €1.098/month. Apparently 20 % of the server price; that is inferred, not stated.
- Traffic overage: €1.00/TB in the EU and US, **€7.40/TB in Singapore**.
- Volume price: not published on the pages read.

**Conflicts to resolve in the Console.**

- Hetzner's locations doc says EU prices are the same in all eu-central locations, but the plan pages show HEL1 cheaper than NBG1 for CPX.
- Hetzner raised cloud prices during 2026; third-party pages still quote older figures.
- CX33 was marked **"currently unavailable"**.

**Region assessment.**

| Region | For | Against |
|---|---|---|
| **Helsinki** | lowest CPX price; EU (GDPR); 20 TB traffic | slightly further for users in Western Europe |
| Nuremberg / Falkenstein | central Europe; FSN1 is the natural backup site for a HEL1 app | CPX dearer in NBG1 |
| **Singapore** | lowest latency if you are in South or Southeast Asia | **+62 %** for CPX32 (€49.49 vs €30.49); CX33 not offered; only 2 TB traffic and **€7.40/TB** overage; data outside the EU. The backup server should still be in the EU, so NFS over `wg1` crosses continents (fine for one nightly archive, slower restores) |

ApplyForge is interactive but light: one user, small API responses, a static SPA. Up to roughly 150–250 ms of latency is acceptable. **Recommendation: Helsinki app + Falkenstein backup**, unless you live in Asia–Pacific and the latency test (C1) shows the EU above about 250 ms; then Singapore app + EU backup.

**Cost estimates (monthly, excluding VAT).**

| Scenario | App server | App Backups (20 %) | Backup server (CPX02) | Its Backups | Domain (€12/yr) | **Total** |
|---|---|---|---|---|---|---|
| S1: CX33 HEL1 + CPX02 FSN1 | €8.99 | €1.80 | €6.49 | €1.30 | €1.00 | **≈ €19.60** |
| S2: CX33 unavailable → CPX32 HEL1 + CPX02 FSN1 | €30.49 | €6.10 | €6.49 | €1.30 | €1.00 | **≈ €45.40** |
| S3: CPX32 SIN1 + CPX02 FSN1 | €49.49 | €9.90 | €6.49 | €1.30 | €1.00 | **≈ €68.20** |

**Assumptions.**

- Prices as read on 2026-11-21, excluding VAT (add your country's rate).
- Backups at 20 % of the server price.
- App-server Backups are optional, because the application data is already backed up. They are included for quick whole-server rollback.
- Traffic stays within the allowance (expected: well under 1 TB/month).
- A typical domain price of €10–15/year.
- Healthchecks.io free tier (€0).
- Occasional snapshots before risky changes: cents per month.

The CX33/CPX32 choice is the largest cost driver. If CX33 stays unavailable, waiting for stock is reasonable; CX23 (2 vCPU / 4 GB, €5.99) is below the requested spec and not recommended.

## 2. Network (A4–A7)

```
   Internet ──UDP 51820──▶ [Hetzner firewall] ──▶ APP SERVER (HEL1)        BACKUP SERVER (FSN1)
                                                  wg0 10.99.0.1/24          wg0 10.97.0.1/24 (admin devices only)
   admin devices ◀══ wg0 (SSH, HTTPS, DNS) ══▶   wg1 10.98.0.1 ══UDP 51821══▶ wg1 10.98.0.2  (NFS 2049 only)
                                                  nginx, Gunicorn, PostgreSQL   /srv/applyforge-backups (own disk)
```

- **App server.** `deploy/nftables/applyforge.nft`, which now includes the two DNS rules below (rehearsed in VMs):
  - `iifname "wg0" udp dport 53` and `tcp dport 53` for the VPN resolver (A11);
  - nothing inbound on `wg1`, because the app server is the NFS client.
- **Backup server:** `deploy/nftables/applyforge-backup-server.nft` (rehearsed in VMs).
  - `policy drop`, established/related accepted;
  - UDP 51820 (admin `wg0`) and UDP 51821 (`wg1`) accepted;
  - TCP 22 accepted only on its own `wg0`;
  - TCP 2049 accepted only on `wg1` from `10.98.0.1`.
- **Same rollback-timer procedure** for both servers (runbook 14.3).

## 3. DNS and TLS (A10–A12)

**Rehearsed in VMs** with fictional zones and Pebble (`PRODUCTION_READINESS.md` 3c); the real DNS provider and Let's Encrypt are untested (D4, D12). Runbook 15.1–15.2.

- **Public DNS:** only `_acme-challenge.applyforge.<domain>  CNAME  _acme-challenge.<validation-zone>`. The validation zone holds only the TXT record that the ACME client writes.
- **Credential:** an API token **restricted to the validation zone**, stored root-only (`0600`) on the app server. If the provider cannot scope tokens per zone, put the validation zone in a separate DNS account.
- **Private DNS:** unbound on the app server, listening on `10.99.0.1` only:
  - `local-data: "applyforge.<domain>. A 10.99.0.1"`;
  - recursion for `10.99.0.0/24` only.

  WireGuard client configs set `DNS = 10.99.0.1`. While the tunnel is up, the device's DNS goes through the server: the app name resolves privately and other names are resolved recursively by the server.
- **Trade-off.** With the tunnel up and the server down, the device has no DNS until you disconnect the VPN. That's acceptable for a single-purpose admin VPN. Laptops can use a hosts-file entry instead.
- **Renewal:** a systemd timer runs the ACME client twice daily. On renewal, a deploy hook:
  1. installs the certificate and key into `/etc/ssl/applyforge/` (`0644` / `0600`);
  2. runs `nginx -t && systemctl reload nginx`;
  3. waits a few seconds, then verifies the served certificate with a new connection (runbook 13.3).

  Expiry is monitored (A14).
- **Alternative (verified in rehearsal):** a private CA (runbook 2, RFC 5280 profile). Use it if no domain is available.

## 4. Backups (A6–A9)

**Rehearsed in VMs** (3c): isolation, the sealed vault, the outage fast-fail and the restore. Real storage is untested (D6, D7). Runbook 15.3–15.4.

- **Capacity: not final.** Planning formula: `disk ≥ 37 × archive size × 1.3 + 2 GB`.

  | Archive size | Needed | Suitable disk |
  |---|---|---|
  | 50 MB | ≈ 4.4 GB | CPX02 (20 GB) |
  | 300 MB | ≈ 16 GB | CPX02 (20 GB) |
  | larger | — | CX23 (40 GB) or larger, still without Volumes, so Hetzner Backups cover it |

  Measure the first real archive (C7) before committing.
- **Monthly offline copy:** from an admin device over the VPN, copy the newest archive and the app server's `index.tsv`. Then run `applyforge-backup verify --expected-sha256` offline (Linux/macOS, or WSL on Windows). Keep the last 3 monthly copies.

## 5. Key custody (A13)

Runbook 15.5.

- The primary and escrow age identities are generated together, offline, on your computer. The server receives only the two public recipients.
- **Quarterly key-access test (non-destructive):** open each identity and run `age-keygen -y <identity>`. The printed public key must equal a line in `/etc/applyforge/backup-recipients.txt`. For the paper key, type it into a temporary file on an offline device, test it, then delete the file.
- **Yearly:** restore drill with the escrow identity.
- **Recovery:**
  - if the primary is lost: use the escrow key, generate a new pair, replace the recipients, take and verify a new backup, then make a new paper escrow;
  - if a key is exposed: runbook 13.4.

## 6. Monitoring (A14)

**Rehearsed against a mock receiver** (3c); no account is created and no real pings are sent. Runbook 15.6.

| Check | From | Period / grace | Fires when |
|---|---|---|---|
| App health | app server timer: `curl https://<app-host>/api/v1/health/` through nginx | 5 min / 10 min | the health check fails, or the server/timer is dead |
| Backup completion | `applyforge-backup.service` `ExecStartPost` → success ping; `OnFailure=` → `/fail` ping | daily / 3 h | a backup fails |
| Backup age | the same check: Healthchecks alerts when no success ping arrives within 24 h + grace | — | the newest good backup is more than about 27 h old |
| Disk | timers on both servers | hourly / 2 h | any watched filesystem is above 80 % |
| Certificate | app server timer | daily / 1 day | the certificate expires in less than 21 days |
| Backup server alive | backup server timer | 15 min / 30 min | the backup server is down |

Independence: the alerts come from Healthchecks.io's own infrastructure when pings **stop**, so a dead app server still triggers them. Ping URLs are stored root-only (`0600`); they are not secrets that grant access, but leaking them allows false "OK" pings.

## 7. Emergency access (A15)

Runbook 15.7.

- **Provider account:** 2FA (TOTP; add a security key or passkey if Hetzner offers it for the account). Recovery codes printed and stored with the escrow key. Console access tested on both servers **before** the first firewall change.
- **Normal emergency SSH:** from the **second enrolled device** through WireGuard.
- **Firewall mistake:** the rollback timer (14.3), then the console (14.4).
- **WireGuard itself unavailable** (no handshake from any device):
  1. Provider console → log in with the console password.
  2. Check `systemctl status wg-quick@wg0`, `journalctl -u wg-quick@wg0 -b`, `wg show`, and `nft list ruleset | grep 51820`.
  3. Restart: `systemctl restart wg-quick@wg0`. If the config is broken, restore it from `/etc/wireguard/wg0.conf.bak`, which is kept after every change.
  4. If the network is the problem, check the provider firewall rule for UDP 51820 and the server's public IP.
  5. **Last resort, time-boxed:**
     - add a provider-firewall rule allowing TCP 22 from your *current* public IP only;
     - on the console, run `nft insert rule inet applyforge input ip saddr <your-ip> tcp dport 22 accept` together with a 30-minute rollback timer;
     - fix WireGuard over SSH;
     - remove both rules.
  6. If the system cannot boot or the disk must be repaired: Hetzner rescue system, or a restore from Backups or a snapshot.
- **All admin devices lost:** console → generate a new peer key on a new device → add its `[Peer]` → restart `wg-quick@wg0`.
