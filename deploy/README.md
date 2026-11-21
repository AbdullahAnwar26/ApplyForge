# ApplyForge deployment runbook: private/VPN Linux server

Milestones 8.4c and 8.4d. This runbook covers deploying ApplyForge to **one Linux server that is reachable only over a VPN**, and backing it up. It is a first configuration and has not been used on a real server yet.

| In scope | Not in scope |
|---|---|
| Gunicorn, systemd, nginx and environment configuration; release layout; migrations; static files; health checks; upgrades; **code** rollback; **encrypted backups and restore into new destinations** (section 12) | Server provisioning, VPN setup, certificate issuance, off-host storage provisioning, monitoring and alerting, public exposure |

Decisions in this configuration:

- **Access.** The server is private and VPN-only. nginx listens on the VPN address and allows only the VPN subnet. Gunicorn listens on `127.0.0.1` only.
- **Blocked features.** `/admin` is blocked at nginx. `/media` is never served.
- **Collector and draft generation are disabled.**
  - nginx refuses their write requests.
  - No LinkedIn session, Chromium/Playwright browser or Ollama is configured on the server.
  - The service may only open network connections to localhost.
- **Database.** PostgreSQL runs on the same host. The first deployment starts from an **empty** database.
- **Frontend.** The frontend is built on the admin workstation, and `frontend/dist` is copied to the server.
- **HSTS** is set to one year, **without** `includeSubDomains` and **without** `preload`.

---

## 1. Architecture

```
VPN client ──HTTPS──▶ nginx (10.99.0.1:443, VPN subnet only, TLS terminated)
                        │  /             → /srv/applyforge/current/frontend/dist (SPA, index.html fallback)
                        │  /assets/      → same dist, immutable cache
                        │  /static/      → /var/lib/applyforge/static (collectstatic)
                        │  /admin, /media → 404
                        │  /api/         → proxy, X-Forwarded-Proto overwritten with "https"
                        ▼
                      Gunicorn (127.0.0.1:8000, 1 worker × 8 threads, timeout 900s)
                        │  systemd unit "applyforge", user applyforge, sandboxed
                        ▼
                      PostgreSQL (127.0.0.1:5432, database "applyforge")
```

Files in this directory:

| File | Installed as | Purpose |
|---|---|---|
| `gunicorn.conf.py` | used in place from the release | localhost bind, one worker, threads, timeouts, logging to the journal, control socket off |
| `systemd/applyforge.service` | `/etc/systemd/system/applyforge.service` | service user, environment file, preflight, sandboxing |
| `nginx/applyforge.conf` | `/etc/nginx/sites-available/applyforge.conf` | TLS, SPA, static, API proxy, blocked paths, timeouts |
| `nginx/snippets/applyforge-*.conf` | `/etc/nginx/snippets/` | security headers and proxy headers, shared by locations |
| `env.production.example` | `/etc/applyforge/applyforge.env` (filled in) | production settings, **placeholders only** in the repository |
| `preflight.py` | used in place from the release | fail-closed configuration check before start and before every management command |
| `bin/applyforge-manage` | `/usr/local/sbin/applyforge-manage` | runs `manage.py` as `applyforge` with the service's environment |
| `backup/afbackup.py` | `/usr/local/sbin/applyforge-backup` | encrypted backup (`create`), offline check (`verify`), restore into new destinations (`restore`) and service recovery (`recover-service`); section 12 |
| `backup/backup.conf.example` | `/etc/applyforge/backup.conf` | backup settings (no secrets; root-owned) |
| `backup/tests/` | not installed | automated tests with fictional data and stand-in tools |
| `systemd/applyforge-backup.{service,timer}` | `/etc/systemd/system/` | daily backup at 03:30, strict sandbox, `ExecStopPost` service recovery |
| `systemd/applyforge-backup.tmpfiles` | `/etc/tmpfiles.d/applyforge-backup.conf` | root-only state and work directories, shared lock file |
| `host_check.py` | used in place from the release | read-only host readiness check (section 13.1) |
| `systemd/nginx-applyforge-vpn.conf` | `/etc/systemd/system/nginx.service.d/applyforge-vpn.conf` | starts nginx after the VPN interface it listens on (section 14) |
| `nftables/applyforge.nft` | `/etc/nftables.conf` | default-deny inbound firewall: WireGuard from outside, SSH/HTTP/HTTPS only through the VPN (section 14) |
| `tests/` | not installed | `test_host_check.py`; `test_firewall_ruleset.py`; `test_monitoring_acme.py`; `stack_test.sh`, the nginx + Gunicorn end-to-end test (section 13.1) |
| `PRODUCTION_READINESS.md` | not installed | readiness assessment, verified/unverified requirements, blockers and go/no-go checklist |
| `INFRASTRUCTURE_DECISIONS.md` | not installed | planned production infrastructure: decision register, region and cost estimates, owner inputs, authorizations (section 15) |
| `nftables/applyforge-backup-server.nft` | backup server `/etc/nftables.conf` | backup server firewall (section 15.3) |
| `unbound/applyforge.conf` | `/etc/unbound/unbound.conf.d/applyforge.conf` | private resolver for VPN clients (section 15.1) |
| `acme/` | `/usr/local/sbin/`, `/etc/systemd/system/` | DNS-01 issuance/renewal wrapper, deploy hook, renewal timer (section 15.2) |
| `monitoring/` | `/usr/local/sbin/`, `/etc/systemd/system/` | Healthchecks pings and checks, timers (section 15.6) |
| `systemd/applyforge-seal.{service,timer}` | backup server `/etc/systemd/system/` | seal archives into the root-only vault (section 15.3) |

### Why one Gunicorn worker

API throttling uses Django's per-process `LocMemCache`. The collector's "one run at a time" lock is also per process. Several worker processes would multiply the throttle limits and allow parallel collector runs. The configuration therefore uses **one worker with 8 threads**. Don't raise `workers` unless throttling and locking have moved to a shared store such as Redis.

### Filesystem layout and ownership

| Path | Owner:group | Mode | Notes |
|---|---|---|---|
| `/srv/applyforge/releases/<commit>/` | `root:root` | `0755` | One directory per release: `backend/`, `deploy/`, `frontend/dist/`, `.venv/`. Read-only for the service. |
| `/srv/applyforge/current` | `root:root` | symlink | Points at the active release. nginx and systemd use only this path. |
| `/etc/applyforge/applyforge.env` | `root:applyforge` | `0640` | Secrets. Readable by root and the service only. |
| `/var/lib/applyforge/` | `root:root` | `0755` | |
| `/var/lib/applyforge/media/` | `applyforge:applyforge` | `0750` | Uploaded resumes. **The only path the service may write.** nginx cannot read it. |
| `/var/lib/applyforge/static/` | `applyforge:applyforge` | `0755` | Written by `collectstatic` (through `applyforge-manage`) and read by nginx. |

Media and static files live outside the release tree, so upgrades and code rollbacks never touch them.

---

## 2. Prerequisites

These are prepared outside this milestone. This runbook does not install or configure them.

- **Server.** **Ubuntu 26.04 LTS** is the validated platform (Milestone 8.4e: systemd 259, nginx 1.28.3, PostgreSQL 18.6, age 1.2.1). Another current Ubuntu LTS or Debian stable host needs systemd ≥ 247 and nginx ≥ 1.19.4 (for `ssl_reject_handshake`), and the 8.4e checks repeated (`deploy/PRODUCTION_READINESS.md`).
- **Python: the distribution's `python3`** (3.14.4 on Ubuntu 26.04; 3.12 or later is required) plus `python3-venv`. Ubuntu 26.04 has no `python3.12` package; don't install another interpreter. The full backend suite passes on 3.14.4 (Linux) and 3.12 (Windows development).
- **Readiness check.** After provisioning, and before every deployment, run the read-only host check: `sudo python3 -I "$REL/deploy/host_check.py" --release "$REL"` (section 13.1).
- **PostgreSQL 14 or later** on the same host, listening on `localhost` only, on the **default port 5432**. The Milestone 8.4b rehearsal used PostgreSQL 18.6. The backup tool runs `pg_dump`, `createdb`, `psql` and `pg_restore` with a scrubbed environment (no `PG*` variables) through the default Unix socket, so it reaches only a cluster on port 5432.
- **A working VPN.** The server needs a fixed VPN address (example: `10.99.0.1`), and clients need a known subnet (example: `10.99.0.0/24`). The host firewall must not expose ports 80 or 443 on any public interface. nginx binds to the VPN address only, but the firewall is the primary control. Section 14 has the rehearsed WireGuard setup, the default-deny ruleset (`deploy/nftables/applyforge.nft`) and the safe procedure for changing it.
- **Name resolution.** A private host name (example: `applyforge.vpn.example`) must resolve to the VPN address for VPN clients, through internal DNS or a hosts file. **Never publish the VPN address in public DNS.** The planned production setup (section 15.1; rehearsed in VMs, not yet on real infrastructure) uses a resolver on the server's VPN address.
- **TLS certificate.** You need a certificate and key for that host name, installed as `/etc/ssl/applyforge/applyforge.crt` (`0644`) and `/etc/ssl/applyforge/applyforge.key` (`0600`, `root`). Two ways to get them:
  - **Planned for production (rehearsed in VMs with Let's Encrypt's test server; not yet against the real service): a publicly trusted Let's Encrypt certificate via DNS-01** for a hostname under a domain you own, renewed and deployed automatically (section 15.2). No CA has to be installed on devices.
  - **Alternative (verified in the rehearsals): a private CA** that the client devices trust. Use it when no domain is available.
  - **The private CA must issue RFC 5280-conformant certificates.** The CA certificate needs `basicConstraints = critical, CA:TRUE` and `keyUsage = critical, keyCertSign, cRLSign`. The server certificate needs `subjectAltName = DNS:<host name>`, `extendedKeyUsage = serverAuth`, and subject/authority key identifiers. Strict clients reject a CA without `keyUsage`, even though curl and browsers may accept it: in the 8.4e rehearsal, Python 3.13+ failed with "CA cert does not include key usage extension". Check with `openssl verify -x509_strict -CAfile ca.crt applyforge.crt`.
- **Admin workstation.** Git, Node.js LTS (to build the frontend) and `scp`/`ssh` access to the server over the VPN.

Do **not** install Chromium or Playwright browsers, and do not run `playwright install`. Do not install Ollama, configure LinkedIn credentials or copy a browser profile to the server.

---

## 3. Initial server setup (first deployment only)

```bash
# Service user: no login shell, no home directory.
sudo useradd --system --user-group --home-dir /nonexistent --no-create-home \
     --shell /usr/sbin/nologin applyforge

# Directories.
sudo install -d -o root -g root -m 0755 /srv/applyforge /srv/applyforge/releases /var/lib/applyforge
sudo install -d -o applyforge -g applyforge -m 0750 /var/lib/applyforge/media
sudo install -d -o applyforge -g applyforge -m 0755 /var/lib/applyforge/static
sudo install -d -o root -g applyforge -m 0750 /etc/applyforge
```

### 3.1 Create an empty PostgreSQL database

The first deployment starts from an **empty** database. Don't copy data from a development SQLite database or from the 8.4b rehearsal database.

```bash
sudo -u postgres createuser --pwprompt applyforge            # login role, no superuser, no CREATEDB
sudo -u postgres createdb --owner applyforge applyforge
```

Keep PostgreSQL on localhost: `listen_addresses = 'localhost'`, with `scram-sha-256` for host connections in `pg_hba.conf`. Each Gunicorn thread can hold one connection (`conn_max_age=600`), so expect up to about 8 connections from ApplyForge, plus 1 or 2 during management commands.

### 3.2 Production environment file

Do this **after the first release has been extracted** (section 5), so that `$REL` exists. Copy the example from that release, then fill in every `<...>` value **on the server**:

```bash
sudo test -f "$REL/deploy/env.production.example"
sudo install -o root -g applyforge -m 0640 "$REL/deploy/env.production.example" /etc/applyforge/applyforge.env
sudoedit /etc/applyforge/applyforge.env
```

On upgrades the existing `/etc/applyforge/applyforge.env` is kept. Compare it with the new release's example and add any new settings by hand.

- Generate `SECRET_KEY` and `API_TOKEN` separately on the server: `python3 -c "import secrets; print(secrets.token_urlsafe(64))"`.
- The API token is what users enter in the web UI. Share it only through a trusted channel.
- `DATABASE_URL` uses the password from section 3.1. URL-encode any special characters in it.
- `ALLOWED_HOSTS` must be the private host name. `CSRF_TRUSTED_ORIGINS` must be `https://<that host name>`.
- Keep `DEBUG=False`, `APPLYFORGE_DOTENV=/nonexistent/applyforge-no-dotenv`, an empty `CORS_ALLOWED_ORIGINS=`, `SECURE_HSTS_INCLUDE_SUBDOMAINS=False` and `SECURE_HSTS_PRELOAD=False`.

Never copy a development `.env` to the server, and never put real values into `deploy/env.production.example` or anywhere else in the repository.

#### How a development `.env` is kept out

- `settings.py` loads only the file named by `APPLYFORGE_DOTENV`. The unit and the environment file both point it at a path that must not exist.
- `preflight.py` refuses to start the service or run a management command in any of these cases:
  - that path exists;
  - a `.env` exists anywhere in the release;
  - `DEBUG` is anything other than an explicit false value;
  - the resolved Django settings still have `DEBUG` on.
- The release itself is built with `git archive`, which includes only tracked files. `.env` is git-ignored.

---

## 4. Build a release (admin workstation)

Releases are identified by their full commit hash. Build from a **clean, committed** tree; on Windows, use Git Bash.

```bash
SHA=$(git rev-parse HEAD)
git status --porcelain          # must print nothing

# Backend, deploy files and docs: tracked files only (no .env, no db.sqlite3, no media).
git archive --format=tar.gz --prefix="$SHA/" -o "applyforge-$SHA.tar.gz" "$SHA"

# Frontend: build from the same commit.
cd frontend
npm ci
npm run lint && npm run typecheck && npm test
npm run build                   # writes frontend/dist
tar -czf "../applyforge-$SHA-dist.tar.gz" dist
cd ..

sha256sum "applyforge-$SHA.tar.gz" "applyforge-$SHA-dist.tar.gz" > "applyforge-$SHA.sha256"
```

Frontend build notes:

- Leave `VITE_API_BASE_URL` **unset**. The default `/api/v1` is same-origin, which is what nginx serves.
- Make sure no `frontend/.env*` file sets other `VITE_*` values. Vite inlines them into the public bundle.
- The API token is never part of the build. Users enter it in the browser.

Copy the three files to the server over the VPN, for example: `scp applyforge-$SHA* admin@applyforge.vpn.example:`.

---

## 5. Install a release (server)

```bash
SHA=<full commit hash>
REL=/srv/applyforge/releases/$SHA
sha256sum -c "applyforge-$SHA.sha256"

sudo test ! -e "$REL"                                   # never overwrite an existing release
sudo tar -xzf "applyforge-$SHA.tar.gz" -C /srv/applyforge/releases      # creates $REL/
sudo install -d -o root -g root -m 0755 "$REL/frontend"
sudo rm -rf "$REL/frontend/dist"
sudo tar -xzf "applyforge-$SHA-dist.tar.gz" -C "$REL/frontend"          # creates $REL/frontend/dist
sudo test -f "$REL/frontend/dist/index.html"
sudo find "$REL" -name '.env' -print                    # must print nothing

# Per-release virtual environment. Old releases keep their own dependencies for rollback.
sudo python3 -m venv "$REL/.venv"
# Wheels only, exact versions for every package (backend/constraints.txt was resolved and
# tested on Ubuntu 26.04 / Python 3.14.4). pip itself is left at the distribution's version.
sudo "$REL/.venv/bin/pip" install --no-cache-dir --only-binary=:all: \
     -r "$REL/backend/requirements.txt" -c "$REL/backend/constraints.txt"
sudo "$REL/.venv/bin/pip" check
sudo "$REL/.venv/bin/python" -m compileall -q "$REL/backend" "$REL/deploy"   # the service cannot write bytecode

sudo chown -R root:root "$REL"
sudo chmod -R go-w "$REL"
sudo chmod 0755 "$REL/deploy/bin/applyforge-manage"
```

`requirements.txt` includes the `playwright` Python package because the collector code imports it. That's harmless without browsers. **Don't** run `playwright install`.

**First deployment:** now create the production environment file from this release (section 3.2), then continue with section 6.

---

## 6. Install the service and site configuration

On the first deployment, repeat these steps whenever a release changes a file under `deploy/systemd`, `deploy/nginx` or `deploy/bin`.

```bash
sudo install -m 0755 "$REL/deploy/bin/applyforge-manage" /usr/local/sbin/applyforge-manage
sudo install -m 0644 "$REL/deploy/systemd/applyforge.service" /etc/systemd/system/applyforge.service
sudo install -m 0644 "$REL/deploy/nginx/snippets/applyforge-security-headers.conf" /etc/nginx/snippets/
sudo install -m 0644 "$REL/deploy/nginx/snippets/applyforge-proxy.conf" /etc/nginx/snippets/
sudo install -m 0644 "$REL/deploy/nginx/applyforge.conf" /etc/nginx/sites-available/applyforge.conf
sudoedit /etc/nginx/sites-available/applyforge.conf     # replace 10.99.0.1, 10.99.0.0/24, applyforge.vpn.example
sudo ln -sfn /etc/nginx/sites-available/applyforge.conf /etc/nginx/sites-enabled/applyforge.conf
sudo rm -f /etc/nginx/sites-enabled/default             # the distribution's default site listens on all addresses
# nginx listens on the VPN address: start it only after the VPN interface exists (section 14).
sudo install -d /etc/systemd/system/nginx.service.d
sudo install -m 0644 "$REL/deploy/systemd/nginx-applyforge-vpn.conf" /etc/systemd/system/nginx.service.d/applyforge-vpn.conf

sudo systemctl daemon-reload
sudo nginx -t
```

Don't reload nginx or start ApplyForge yet. The unit is verified in section 8, after the switch. It names paths under `/srv/applyforge/current`, which don't exist until then on the first deployment.

---

## 7. Preflight, migrations and static files

Run all of these against the **new release directory**, before switching `current`:

```bash
sudo applyforge-manage "$REL" check --deploy
sudo applyforge-manage "$REL" migrate --plan
sudo applyforge-manage "$REL" migrate --noinput
sudo applyforge-manage "$REL" collectstatic --noinput
```

- **Preflight.** `applyforge-manage` runs `preflight.py` first and stops if it fails. Fix the reported variable in `/etc/applyforge/applyforge.env`. Values are never printed.
- **`check --deploy`.** It is expected to report two warnings: `security.W005` (HSTS without `includeSubDomains`) and `security.W021` (no HSTS preload). Both are deliberate here. Any other warning must be investigated.
- **`migrate --plan`.** Read the plan before applying it. On the first deployment, it lists every migration against the empty database.
- **Backups.** On later upgrades, if the plan lists any migration, take a database backup first (see sections 9 and 10).
- **`collectstatic`** fills `/var/lib/applyforge/static`, which holds the static files used by the Django admin and the browsable API. The SPA itself is served from the release's `frontend/dist`.

---

## 8. Switch traffic and start

```bash
sudo ln -sfn "$REL" /srv/applyforge/current.new
sudo mv -T /srv/applyforge/current.new /srv/applyforge/current      # atomic switch

# Verify the unit now that the paths it names exist - before enabling/starting it.
sudo systemd-analyze verify /etc/systemd/system/applyforge.service
sudo systemd-analyze security applyforge.service        # review the exposure score

sudo systemctl enable --now applyforge      # first deployment; later: sudo systemctl restart applyforge
sudo systemctl reload nginx
```

- **If `verify` reports a problem, fix it before enabling the service.** On the first deployment nothing is running yet, so `current` pointing at the new release is harmless.
- **Always use `restart`, never a HUP reload.** A reload would keep the previous release's working directory.
- **After repeated failed starts, reset the service.** systemd stops retrying after 5 failed starts within 5 minutes, for example when the preflight keeps failing. Once the cause is fixed, run `sudo systemctl reset-failed applyforge` and then `sudo systemctl start applyforge`.

### 8.1 Health checks

On the server:

```bash
systemctl status applyforge --no-pager
journalctl -u applyforge -n 50 --no-pager              # "preflight: OK", then Gunicorn "Listening at: http://127.0.0.1:8000"
sudo ss -ltnp | grep -E ':(80|443|8000|5432)\b'         # 8000 and 5432 on 127.0.0.1 only; 80/443 on the VPN address only

# Gunicorn directly. Host and X-Forwarded-Proto are what nginx would send;
# without them Django answers 400 (Host) or 301 (HTTPS redirect).
curl -fsS -H 'Host: applyforge.vpn.example' -H 'X-Forwarded-Proto: https' \
     http://127.0.0.1:8000/api/v1/health/
# -> {"status": "ok", "database": "ok"}

# Through nginx and TLS (add --cacert <private CA> if the CA is not in the system store).
# This request comes from the server's own VPN address (10.99.0.1). nginx permits it
# with a separate `allow 10.99.0.1;`, because with single-address VPN setups (e.g.
# WireGuard /32 peers) that address may not be inside the client subnet. If this
# returns 403, check that line matches the server's actual VPN address.
curl -fsS --resolve applyforge.vpn.example:443:10.99.0.1 https://applyforge.vpn.example/api/v1/health/
```

From a VPN client, check each of these:

| Check | Expected |
|---|---|
| `https://applyforge.vpn.example/` | App loads and asks for the API token. After entering it, the dashboard shows an empty database. |
| `curl -sI https://applyforge.vpn.example/` | `Strict-Transport-Security: max-age=31536000`, with no `includeSubDomains` and no `preload`. `Content-Security-Policy` and `X-Frame-Options: DENY` are present. |
| `curl -s -o /dev/null -w '%{http_code}' https://applyforge.vpn.example/admin/` | `404` |
| `curl -s -o /dev/null -w '%{http_code}' https://applyforge.vpn.example/media/x` | `404` |
| `curl -s https://applyforge.vpn.example/api/v1/profiles/` | `401` (token required) |
| `curl -s -X POST https://applyforge.vpn.example/api/v1/collector/runs/` | `403` `{"detail":"Collection is disabled on this server."}` |
| `curl -s -X POST https://applyforge.vpn.example/api/v1/applications/1/generate/` | `403` `{"detail":"Draft generation is disabled on this server."}` |
| `curl -s -H 'X-Forwarded-Proto: http' https://applyforge.vpn.example/api/v1/health/` | `200`. nginx overwrites the header, so there is no redirect loop. |
| `http://applyforge.vpn.example/` | `301` to the `https://` URL |
| Same URL from outside the VPN | No connection. |

The health endpoint is unauthenticated and returns only `{"status":"ok","database":"ok"}` (200) or `{"status":"unavailable","database":"unavailable"}` (503).

---

## 9. Upgrades

1. **Build** the new release on the workstation (section 4).
2. **Install** it on the server next to the current one (section 5). The running release is not touched.
3. If `deploy/systemd`, `deploy/nginx` or `deploy/bin` changed, compare and reinstall those files (section 6) and run `daemon-reload` and `nginx -t`.
4. **Plan the migrations.** Run `sudo applyforge-manage "$REL" check --deploy` and `sudo applyforge-manage "$REL" migrate --plan`.
   - If the plan lists migrations, take a backup before continuing: `sudo applyforge-backup create` (section 12.4). It stops ApplyForge briefly and starts it again.
   - Check that the migrations are compatible with the **old** code, which keeps running until step 6. Additive changes, such as new tables or nullable columns, are compatible.
   - If they are not compatible, stop ApplyForge before migrating and accept a short outage.
5. **Apply.** Run `sudo applyforge-manage "$REL" migrate --noinput` and `sudo applyforge-manage "$REL" collectstatic --noinput`.
6. **Switch** `current`, restart the service and reload nginx (section 8). Then run the health checks (section 8.1).
7. **Keep releases.** Keep the previous two or three releases for rollback, and delete older ones with `sudo rm -rf /srv/applyforge/releases/<old commit>`. Never delete the release that `current` points to.

### What a restart does to requests

`systemctl restart applyforge` stops Gunicorn and starts it again.

- **Graceful shutdown (120 seconds).**
  - On stop, Gunicorn closes its listening socket immediately.
  - In-flight requests then get up to 120 seconds (`graceful_timeout`) to finish.
  - Requests still running after that are killed, and their clients get an error from nginx.
  - systemd waits up to 150 seconds (`TimeoutStopSec`) before it force-kills anything.
- **Temporary 502 window.**
  - From the moment the socket closes until the new Gunicorn is ready, new API requests get **502 Bad Gateway** from nginx.
  - With nothing long in flight, that's a few seconds (preflight plus application start). With a long request still running, it can last up to 120 seconds plus startup.
  - The SPA and static files are served by nginx directly and are not affected.
  - The frontend shows the error, and the user can retry.
- **When to restart.** Restart when no collector run or generation is in progress. While both are disabled, API requests are short, so in practice only the brief 502 window applies.
- **Request time limits.** Gunicorn's `timeout = 900` does not limit how long a request may run with threaded workers; it only detects a frozen worker. The client-side limit is nginx's `proxy_read_timeout`: 60 seconds, or 900 seconds for the collector-run and generate paths. When it expires, the client gets 504, but Django may still finish the request.
- **Before enabling generation later.** Generation can take up to `OLLAMA_TIMEOUT_SECONDS` (180 seconds by default), longer than the 120-second grace period. When it is enabled, raise `graceful_timeout` above that (for example 200 seconds) and `TimeoutStopSec` above `graceful_timeout` (for example 230 seconds). A collector run can outlast any reasonable grace period, so only restart while no run is in progress.

---

## 10. Code rollback

A code rollback points `current` back at the previous release directory and restarts the service. **It never touches the database:**

- it does not restore a backup;
- it does not reverse migrations;
- it does not bring back deleted data.

Assess migration compatibility **first**, while the newer release is still active. Only then switch.

### 10.1 Assess migrations (before switching anything)

```bash
CUR=$(readlink -f /srv/applyforge/current)
PREV=/srv/applyforge/releases/<previous commit>
sudo test -x "$PREV/.venv/bin/gunicorn"

# Migration files present only in the current (newer) release appear as "> " lines:
diff <(cd "$PREV/backend" && find . -path '*/migrations/0*.py' | sort) \
     <(cd "$CUR/backend" && find . -path '*/migrations/0*.py' | sort)

# Which migrations are applied ([X]). Reads the migration table only; changes nothing.
sudo applyforge-manage "$CUR" showmigrations
```

Decide based on the result:

- **No `>` lines, or none of them applied.** The schema is what the older code expects. Continue with 10.2.
- **Applied migrations that the older release doesn't have, all additive** (new tables, or nullable or defaulted columns). The older code normally tolerates them. Continue with 10.2; the extra schema stays in place.
- **Applied migrations that the older release doesn't have, anything else.** **Don't switch yet.** Choose deliberately between:
  - **Fix forward** with a new release (usually best).
  - **Reverse the specific migrations**, using the **current** release, which contains the migration files:
    1. Review `sudo applyforge-manage "$CUR" migrate <app> <migration> --plan`.
    2. Run the same command without `--plan`.
    3. Continue with 10.2.
    Reverse migrations can drop columns or tables along with their data.
  - **Restore the database from a backup.** This is a separate operation that must be explicitly authorized, and it discards everything written since the backup. It is never part of a code rollback: section 12 restores only into a **new** database and media directory, and switching the application to them is a further manual decision (section 12.7).

### 10.2 Switch to the previous release

```bash
sudo ln -sfn "$PREV" /srv/applyforge/current.new
sudo mv -T /srv/applyforge/current.new /srv/applyforge/current
sudo applyforge-manage "$PREV" collectstatic --noinput   # restore the older release's static files (files only)
sudo systemctl restart applyforge
sudo systemctl reload nginx
# health checks: section 8.1
```

The release's `frontend/dist` and `.venv` switch together with `current`, so the frontend and Python dependencies roll back with the code.

---

## 11. Operations

- **Logs.**
  - Application and access logs: `journalctl -u applyforge`.
  - nginx logs: `/var/log/nginx/applyforge.{access,error}.log`.
  - Request URLs are logged. The API token travels in the `Authorization` header and is never logged.
- **Rotating the API token or `SECRET_KEY`.** Edit `/etc/applyforge/applyforge.env`, then run `sudo systemctl restart applyforge`. Users must re-enter the new token in the browser.
- **Stopping.** `sudo systemctl stop applyforge`. nginx then answers `502` for `/api/`.
- **Status of the sandbox.** `systemd-analyze security applyforge.service`.
  - The service can write only `/var/lib/applyforge/media` (and its private `/tmp`).
  - It can connect only to localhost.
  - It has no capabilities and cannot gain privileges.

### Security assumptions

- The server has no untrusted local users. Any local user can reach Gunicorn on `127.0.0.1:8000` and set `X-Forwarded-Proto` themselves. That cannot bypass the API token, but it is why Gunicorn must never listen on another interface.
- The API is protected by a single static bearer token over HTTPS on a private network. Treat the token like a password.
- `/admin` is blocked at nginx only; the Django route still exists. Don't create Django superusers on this server.

### Enabling the collector or draft generation later

This is out of scope for this configuration and needs a separate, approved change. At minimum, it involves:

- removing the corresponding `if (...) { return 403 ...; }` block in `nginx/applyforge.conf` (the 900-second timeouts are already in place);
- loosening `IPAddressDeny` in the unit only as far as needed;
- for the collector:
  - Chromium/Playwright, a LinkedIn session and a writable browser-profile directory (add it to `ReadWritePaths`);
  - a review of headless operation;
- for generation:
  - Ollama on the server's loopback interface. The app refuses non-loopback Ollama URLs.
  - A longer graceful shutdown: raise Gunicorn's `graceful_timeout` above `OLLAMA_TIMEOUT_SECONDS` (for example 200 seconds), and the unit's `TimeoutStopSec` above that (for example 230 seconds). See section 9.

---

## 12. Backups and restore (Milestone 8.4d)

> **Restoring data is not a code rollback.** A code rollback (section 10) only switches releases and never touches the database or media. A restore (section 12.6) only ever creates a **new** database, owned by an isolated restore role, and a **new** media directory. Pointing the application at restored data, including transferring ownership, is a separate decision that needs explicit approval (section 12.7). No tool in this repository drops, overwrites or switches production data.

The tool is `deploy/backup/afbackup.py`, installed as `/usr/local/sbin/applyforge-backup`, with five commands: `create`, `verify`, `restore`, `recover-service` and `probe-destination` (Milestone 8.4e).

- It uses only the Python standard library.
- It calls `age`, the PostgreSQL client tools, `systemctl` and `runuser`, found only on a fixed search path (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/homebrew/bin`).
- Every subprocess gets a **minimal, explicit environment**: that `PATH`, `LANG`/`LC_ALL=C.UTF-8`, `TZ=UTC` and `HOME=/`. Inherited variables such as `PGHOST`, `PGOPTIONS`, `PGSERVICE`, `AGE_*` or `LD_*` never reach them.
- `create`, `restore` and `recover-service` must run as root.
- Its tests (`deploy/backup/tests/`) use fictional data and stand-in tools.

### 12.1 Design and trust model

Each backup is **one** file, `applyforge-backup-<UTC timestamp>.tar.age`: a tar archive encrypted with [age](https://age-encryption.org).

> **age does not prove who created an archive,** and the off-host share controls what it returns when a file is read back. Anyone with the **public** recipient can create an archive that decrypts and validates, and a malicious storage server can return different bytes on every read. The tool therefore keeps three safeguards:
>
> 1. **Hash taken from age's own output.** age's output streams through the tool, which hashes each chunk **as age emits it** while writing it to the share. That hash is the only trusted one. A read-back from the share is used solely to *detect* tampering or corruption; any difference fails the backup.
> 2. **Hash recorded before the archive exists.** It goes durably into a root-only local index (`/var/lib/applyforge-backup/index.tsv`) and the journal **before** the archive gets its final name.
> 3. **Restore depends on the trusted hash and an isolated role.** `restore` requires the hash (`--expected-sha256`) and checks it against a private local copy before anything is decrypted. When a local index exists, the hash must be in it. The dump is then loaded by a **dedicated restore role** that can affect nothing in production (section 12.6).

| Inside the archive | Never inside |
|---|---|
| `manifest.json`: format version, UTC timestamp, host name, database name, deployed release (commit), recipients fingerprint, `pg_dump` version, service state, and the size and SHA-256 of every file | `/etc/applyforge/applyforge.env` (`SECRET_KEY`, `API_TOKEN`, `DATABASE_URL`) and any PostgreSQL credentials. The PostgreSQL tools use peer authentication. |
| `database.dump`: `pg_dump --format=custom` of `DB_NAME` | Symbolic links and special files under `MEDIA_ROOT`. These are reported as a **WARNING** and listed in the manifest, never followed. |
| `media/...`: every regular file under `MEDIA_ROOT` | Browser profiles, LinkedIn sessions, TLS keys, age identities and anything else outside `MEDIA_ROOT` |

**Media capture is race-resistant.**

- `MEDIA_ROOT` is walked **only through directory file descriptors**. Every directory and file is opened relative to its parent with `O_NOFOLLOW` and checked to be the exact object (device and inode) that was just listed.
- Crossing into another filesystem is refused.
- Every file is re-opened the same way when it is archived, and its inode, size and SHA-256 must still match the scan.
- If the application account is compromised and swaps a directory for a symlink (for example to `/etc/applyforge`) at any point, the backup fails instead of archiving the target. Any unreadable file or directory also fails the backup.

**How `applyforge-backup create` runs:**

1. **Checks** happen before anything is touched:
   - it runs as root;
   - `backup.conf` and the recipients file are opened without following symlinks and validated through the open file: root-owned, regular, not group- or world-writable, in parent directories that only root can modify;
   - the configuration has no unknown or duplicate keys, and its paths are separate;
   - the tools exist on the fixed path;
   - the destination is a mount point with the marker file and, if configured, an allowed filesystem type;
   - the recipients file holds public X25519 keys only;
   - the state directory, index and marker are root-only.
2. **Under the shared lock** `/run/lock/applyforge-backup.lock`, which timer and manual `create`, `verify` and `restore` runs all use:
   - the index is read and must be well-formed;
   - an existing service marker is assessed. It **never** leads to ApplyForge being started (section 12.4);
   - stale work directories from crashed runs are removed.
3. **Maintenance window.** If `applyforge.service` is active:
   1. A durable marker is written (`stopping`: boot ID, invocation ID, stop-request time).
   2. The service is stopped, and the marker is updated (`stopped`, plus the service's `InactiveEnterTimestampMonotonic`).
   3. The database is dumped (`pg_dump --lock-wait-timeout`) and the media is captured, within `BACKUP_CAPTURE_TIMEOUT_SECONDS`.
   4. The service is **started again on every path**: success, failure, timeout, SIGTERM, SIGHUP and SIGINT. Signals arriving during the restart are deferred until it finishes, and a closed terminal cannot interrupt it. There are up to three attempts.
   5. The marker is removed. If the restart fails, the marker is kept (`restart-failed`) and the run exits with code **3**.

   A service that was not running is left alone and gets no marker.
4. **Encryption and publication:**
   1. The tool creates `.<name>.partial` itself (`O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW`, `0600`). A planted file or symlink is refused, never followed and never deleted.
   2. age's output is streamed into it and hashed.
   3. `fchmod`, `fsync` and `close`.
   4. A read-back comparison, then a destination re-check.
   5. **The index record (`recorded`) and the journal line are written.**
   6. Publication **without overwriting**: a hard link, or `renameat2(RENAME_NOREPLACE)` where hard links are unsupported. If neither is available, the backup fails closed; there is no check-then-rename fallback.
   7. The published file must be this run's inode.

   Any failure after recording appends a `failed` record for that name. Only the run's own partial file is ever removed.
5. **Retention** (section 12.5) runs last.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Failure, or terminated. The run's own temporary and partial files are removed. |
| `2` | Refused before any change. Also: an ambiguous service marker that needs investigation. |
| **`3`** | **CRITICAL:** ApplyForge was running before the backup and could not be started again. |
| `4` | The archive was created **and recorded**, but retention failed. (A termination during retention is reported as termination, code 1.) |

**Downtime.** ApplyForge is unavailable (nginx answers 502) only while the database and media are captured, at most `BACKUP_CAPTURE_TIMEOUT_SECONDS`, at normal priority. Encryption and the upload happen after the restart.

### 12.2 Prerequisites, age keys and key loss

**Utilities.** These packages are not installed by this milestone; install them deliberately:

- on the **server**: `age`, `postgresql-client` (present with PostgreSQL), Python 3 and systemd (`systemctl`, `runuser`, `systemd-tmpfiles`);
- on the **offline machine**: `age` and Python 3.

**Supported platforms for `verify`:**

| Platform | Status |
|---|---|
| Linux | Supported and tested; the tests ran on Ubuntu under WSL. |
| macOS | Expected to work (POSIX, Homebrew `age` in `/opt/homebrew/bin`), but **untested**. |
| Windows | **Not supported.** The tool refuses to run there. Use a Linux machine or VM offline, or the manual fallback (section 12.9). |

**Keys.** The server holds only public recipients. The private identity never touches the server's disk.

```bash
# On a trusted OFFLINE machine - never on the server:
age-keygen -o applyforge-backup-identity-A.txt      # prints "Public key: age1..."
age-keygen -o applyforge-backup-identity-B.txt      # recommended: a second, independent identity

# On the server: public recipients only, one per line; root-owned, not group/world writable,
# in a root-only parent directory (/etc/applyforge).
sudoedit /etc/applyforge/backup-recipients.txt
sudo chown root:root /etc/applyforge/backup-recipients.txt && sudo chmod 0644 /etc/applyforge/backup-recipients.txt
```

**Rules for the identities:**

- **Recipient type.** Only **X25519 recipients (`age1…`)** are supported. SSH, plugin (for example YubiKey) and post-quantum recipients are refused.
- **Either identity works.** Each archive is encrypted to every listed recipient, so either identity can decrypt it.
- **Storage.** Keep each identity on separate offline media, in **separate places**. Never copy one into `/etc/applyforge`, the backup destination or the repository. The tool refuses a recipients file that contains `AGE-SECRET-KEY-`.
- **Recipient changes.** Each archive's manifest and index entry record a **recipients fingerprint**. After **any** change to the recipients file, run a backup and **verify it offline** (section 12.6) before relying on new backups. A well-formed but wrong key would produce unreadable archives while retention removes old ones; `BACKUP_MIN_KEEP` limits but does not remove this risk.

**What losing or exposing a key means:**

| Event | Consequence and response |
|---|---|
| **All private identities lost** | **Every backup is permanently unreadable.** No recovery, no back door. |
| **One identity lost** | Replace its recipient and verify a fresh backup. Older archives remain readable with the surviving identity. |
| **An identity exposed** | Archives encrypted to it are disclosed to whoever also has the archives. Generate a new identity offline, replace the recipient, verify a new backup, and delete old archives once enough new ones exist. |
| **Server compromised** | The attacker gains only public recipients. Treat the local index as possibly modified, and rely on your offline hash records. |

### 12.3 Off-host destination, filesystem requirements and installation

The destination must be **storage on another machine**, mounted at `BACKUP_DESTINATION` (default `/mnt/applyforge-backups`).

- **Mount and marker.** It must be a mount point **and** contain the regular marker file `.applyforge-backup-destination`.
- **Filesystem type.** Optionally pin it with `BACKUP_ALLOWED_FS_TYPES` (for example `nfs4`).
- **Treat the share as untrusted.** Its operator can delete, replace or plant files, and can return different bytes on reads. The design assumes this (section 12.1). Prefer storage with snapshots or object lock.

**Filesystem requirements and what cannot be guaranteed:**

| Requirement | NFS | SMB/CIFS | sshfs |
|---|---|---|---|
| Exclusive create (`O_EXCL`) | **NFSv3 or later** (NFSv2 is unsafe) | yes | yes |
| No-clobber publication: hard link, or `RENAME_NOREPLACE` | hard links: yes | Samba with a POSIX backend: hard links work (validated, see below); other servers (Windows, NAS appliances) **must be probed**. Without hard links and `RENAME_NOREPLACE` the backup fails closed | hard links need the server's `hardlink@openssh.com` extension (OpenSSH has it) |
| Stable inode identity (`O_NOFOLLOW` plus inode checks) | yes | mount with **`serverino`** (the default; with `noserverino`, inode numbers are synthesized and checks fail) | yes |
| Durability of `fsync`/`close` | depends on the export: use **`sync`** | depends on the server (strict sync) | depends on the SFTP server |
| Root writes | with `root_squash`, files are written as the squashed user, so make the export directory writable by it | as the mount's credentials; owner and modes shown on the client are **forced by mount options** | as the remote user |
| Mount behaviour on server outage | `hard` (default): I/O blocks until the server returns; the unit's `TimeoutStartSec` bounds the run | the cifs default is **`soft`**: mount with **`hard`** | blocks or fails per sshfs options |

**Validated on 2026-11-20 (Milestone 8.4e) with `applyforge-backup probe-destination`**, all disposable and on loopback (Ubuntu 26.04, kernel 6.18):

| Destination | Result | Publication | Notes |
|---|---|---|---|
| NFSv4.2, export `sync,no_root_squash`, mount `hard` | pass | hard link | |
| NFSv4.2, export `sync,root_squash` | pass | hard link | files owned by `nobody` (warning: anyone with that uid on the NFS server can delete archives) |
| NFSv4.2 mounted `soft` | pass, with warning | hard link | the probe flags `soft` |
| SMB 3.1.1, Samba 4.23, `seal`, cifs client | pass | hard link | cifs mounted `soft` by default; ownership/modes forced by `uid=`/`file_mode=` |
| ext4, tmpfs | pass | hard link | local only - not off-host |
| vfat | pass | `renameat2(RENAME_NOREPLACE)` | proves the fallback path; no POSIX modes |

The probe proves the **client-visible semantics on that test setup**, not the durability promises of a particular NAS. Run it on the real share (below).

**Recommended: NFSv4.1+ with a `sync` export, restricted to this server's address, mounted `hard`, with snapshots on the storage side.** Use `sec=krb5p` if the network path between server and storage is not private. Choose SMB 3.1.1 only with `seal`, `hard`, `serverino` and a passing probe. sshfs is not recommended (FUSE, depends on the SFTP server's extensions).

What the tool **cannot** guarantee on any share:
- that the server persisted the data after a successful `fsync`;
- that it keeps the archive unchanged later.

Integrity is only protected by the trusted hash checked at restore.

```bash
# Example only - adapt to your storage. Add an fstab entry or mount unit for the share, then:
sudo install -d -m 0700 /mnt/applyforge-backups
sudo mount /mnt/applyforge-backups
findmnt /mnt/applyforge-backups                      # must show the remote filesystem (note its FSTYPE)
sudo touch /mnt/applyforge-backups/.applyforge-backup-destination   # once, ON the mounted share
```

**Install the tool, configuration, tmpfiles rule and units from a release:**

```bash
sudo install -m 0755 "$REL/deploy/backup/afbackup.py" /usr/local/sbin/applyforge-backup
sudo install -o root -g root -m 0644 "$REL/deploy/backup/backup.conf.example" /etc/applyforge/backup.conf
sudoedit /etc/applyforge/backup.conf
sudo install -m 0644 "$REL/deploy/systemd/applyforge-backup.tmpfiles" /etc/tmpfiles.d/applyforge-backup.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/applyforge-backup.conf
sudo install -m 0644 "$REL/deploy/systemd/applyforge-backup.service" "$REL/deploy/systemd/applyforge-backup.timer" /etc/systemd/system/
sudoedit /etc/systemd/system/applyforge-backup.service   # RequiresMountsFor=/ReadWritePaths= must match backup.conf
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/applyforge-backup.service

# Qualify the mounted share BEFORE the first backup and after any storage change. Writes only
# a private .applyforge-probe-<random> directory on the share and removes it; holds the lock.
sudo applyforge-backup probe-destination        # every line PASS (WARN lines: read and decide); exit 0
```

`probe-destination` checks: the mount point and marker, the filesystem type against `BACKUP_ALLOWED_FS_TYPES`, mount options (NFS `soft`/version/`sec=sys`; SMB dialect, `seal`, `soft`, `noserverino`, forced ownership), exclusive creation, write + `fsync`, read-back hash, ownership (root squashing) and mode, `(device, inode)` stability, no-clobber publication by hard link or `RENAME_NOREPLACE`, `O_NOFOLLOW` and deletion. A `FAIL` means `create` would refuse or could not be trusted on that storage.

**Path rules.**

| Path | Purpose |
|---|---|
| `BACKUP_STATE_DIR` (default `/var/lib/applyforge-backup`, `0700`) | The trusted index and the service marker. |
| `BACKUP_WORK_DIR` (default `/var/lib/applyforge-backup-work`, `0700`) | Temporary plaintext. Needs room for about three times one backup during a restore. |
| `BACKUP_LOCK_FILE` (default `/run/lock/applyforge-backup.lock`) | The shared lock. Each run also holds its own lock inside its work directory. |

The tool refuses:

- overlapping destination, work, state and media directories;
- the lock file inside the destination or media;
- `MEDIA_ROOT`, or a restore target, under `/etc`, `/root`, `/home` or `/srv/applyforge`.

**Sandbox.** The unit uses `ProtectSystem=strict`, with write access only to the destination, state and work directories, `RestrictAddressFamilies`, `SystemCallFilter=@system-service` and `CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_DAC_OVERRIDE CAP_DAC_READ_SEARCH CAP_KILL CAP_AUDIT_WRITE` (exposure 2.7). These were validated with the real tool under transient units on Ubuntu 26.04 (section 12.10); a negative control without the DAC and KILL capabilities failed as expected. If `runuser` fails on the production host with a PAM error, check `journalctl -u applyforge-backup` before widening anything.

### 12.4 Backup procedure, maintenance window and service recovery

```bash
sudo systemctl start applyforge-backup.service       # preferred, also for manual backups (see below)
journalctl -u applyforge-backup -n 50 --no-pager     # "archive ... sha256=<64 hex> ... recorded", "backup complete: ..."
systemctl is-active applyforge                       # active again (if it was before)
sudo cat /var/lib/applyforge-backup/index.tsv        # created, name, sha256, size, recipients fingerprint, host, status
```

1. **Keep an independent copy of the hashes.** The index is the primary trusted record but is lost with the server. Regularly copy `index.tsv`, or the `sha256=` journal lines, to the offline machine or another trusted place that **isn't** the backup share. Only `recorded` lines whose name has no later `failed` line are valid.
2. **Run manual backups through the unit.** Use `systemctl start applyforge-backup.service` rather than `sudo applyforge-backup create` in a terminal. The unit runs detached from your SSH session and has `ExecStopPost` recovery. A terminal run still handles SIGHUP, SIGINT and SIGTERM, and a closed terminal, by restarting ApplyForge before exiting. But a **SIGKILL** of a terminal run can only be recovered manually.
3. **First backup.** Run `sudo applyforge-backup probe-destination` first (section 12.3). Then run the backup, copy the archive and its hash to the offline machine, and **verify it there with `--expected-sha256`** (section 12.6). Only then enable the timer: `sudo systemctl enable --now applyforge-backup.timer`.
4. **Service recovery rules.** A marker means "this tool stopped ApplyForge":
   - **Automatic recovery** happens only in the unit's `ExecStopPost`, only for the marker of **the same invocation**, on the **same boot**, and only if ApplyForge has been **neither started nor stopped since** the backup stopped it (its `Active/InactiveEnterTimestampMonotonic` are unchanged).
   - **A later backup never starts ApplyForge because an old marker exists.** If the service is active, the marker is obsolete and is removed. If the service was started or stopped since (a deliberate action), the marker is removed and the service is left as it is. If the marker is ambiguous (a reboot, an unreadable marker, a transitional state), the backup refuses with exit code 2 until you investigate.
   - **Manually:** `sudo applyforge-backup recover-service` starts ApplyForge only under the same conditions. After investigating an ambiguous case, start the service yourself if appropriate, then run `sudo applyforge-backup recover-service --discard-marker`.
   - **During system shutdown** the unit's `ExecStopPost` cannot start ApplyForge, because systemd rejects the start job. ApplyForge comes back at boot only if it is enabled, and the marker is then reported as ambiguous (another boot).
5. **Stale plaintext.** Work directories (`create-*`, `restore-*`, `verify-*`) left by crashed runs are removed under the lock, but only if:
   - the name matches exactly;
   - it is a real directory owned by root;
   - its per-run lock is **not held**;
   - it is older than 15 minutes.

   Restore staging directories `.<target>.restoring-*` are handled the same way, but only for the same target. tmpfiles.d removes anything in the work directory older than 2 days.
6. **Alerting.** Monitor the unit result:
   - exit code **3**: ApplyForge is down;
   - exit code **2** from `ExecStopPost`: a marker needs investigation;
   - exit code **4**: retention failed.

### 12.5 Retention

After each **successful** backup, archives older than `BACKUP_RETENTION_DAYS` (default **30**, 1 to 3650) are deleted.

- **Protected archives.** The **`BACKUP_MIN_KEEP` newest valid archives (default 7) are always kept**. Archives in the trusted local index take these slots first, so archives planted on the share cannot push real ones out. Others fill any remaining slots.
- **Future-dated archives.** Archives dated more than a day in the future never take a protected slot and are never deleted by age. They are reported for review.
- **What can be deleted:** only regular files named exactly `applyforge-backup-YYYYMMDDTHHMMSSZ.tar.age` that start with an age header.
- **What is never deleted:** `.partial` files, the marker and anything else.
- **When things fail:** a failed backup deletes nothing. A retention failure exits with code 4; a termination during retention is reported as such.

### 12.6 Verify and restore into new destinations

**Get the expected hash** from a trusted record: the server's `index.tsv` (a `recorded` line) or journal, or your offline copy. Never take it from the backup share.

**Verify** offline. It copies the archive while hashing it, checks `--expected-sha256` before decrypting, then decrypts and validates:
- raw tar headers first, with PAX/GNU long headers limited to 1 MiB, then member count, types and names;
- the manifest, the exact file set, and sizes compared with the manifest before extraction;
- disk space, then every checksum.

Archives over 1 TiB are refused. Verify changes nothing. On the server, pass `--config` so that it uses the same lock and work directory as the other commands.

```bash
applyforge-backup verify --archive applyforge-backup-20261118T033012Z.tar.age \
                         --identity /media/offline-key/applyforge-backup-identity-A.txt \
                         --expected-sha256 <64 hex from your trusted record>
```

**Restore role (one-time setup).** Restores load the dump as a **dedicated** role and OS account, never as `postgres` and never as the production owner `applyforge`:

```bash
sudo useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin applyforge_restore
sudo -u postgres createuser --no-superuser --no-createdb --no-createrole --no-replication applyforge_restore
# No GRANTs to it, no role memberships, nothing owned in production. Peer authentication
# (local all all peer) maps the OS account to the role of the same name - no password.
```

**Recommended production hardening: remove PostgreSQL's default PUBLIC rights on the production database.** This is an operator step, separately reviewed and applied; the tool never runs it.

- **What PostgreSQL grants by default:** every role, including the restore role, gets `CONNECT` and `TEMPORARY` on every new database through `PUBLIC`. The 2026-11-20 rehearsal confirmed this for the source database (`connect=t temp=t create=f`).
- **What the restore role needs:** `CONNECT` on `postgres` (for the tool's identity check) and its own restored databases (which owners always have). It needs **no** rights on the production database.
- **Why a dump can't use them:** `pg_restore` only connects to the new database. There is no cross-database SQL, and `dblink`/`postgres_fdw` require a superuser to install.
- **Why revoke them anyway:** if the restore account were ever misused directly, it couldn't connect to production or create temporary objects there.

```bash
# On the production server, after review (the database owner keeps all rights as owner):
sudo -u postgres psql -c 'REVOKE CONNECT, TEMPORARY ON DATABASE applyforge FROM PUBLIC'
# Keep CONNECT on the "postgres" database for the restore role (the tool's identity check uses it).
# If PUBLIC's CONNECT on "postgres" is ever revoked: GRANT CONNECT ON DATABASE postgres TO applyforge_restore;
# Do not change template0/template1 (restores use template0).
```

Check that nothing else connects to `applyforge` as another role (for example monitoring) before revoking.

**Restore** runs on the server as root:

```bash
sudo applyforge-backup restore \
     --archive /mnt/applyforge-backups/applyforge-backup-20261118T033012Z.tar.age \
     --expected-sha256 <64 hex from your trusted record> \
     --identity /media/offline-key/applyforge-backup-identity-A.txt \
     --new-database applyforge_restore_20261118 \
     --new-media-dir /var/lib/applyforge/media-restore-20261118 \
     --confirm-database applyforge_restore_20261118 \
     --confirm-media-dir /var/lib/applyforge/media-restore-20261118
```

**What the restore guarantees:**

- **Trust checks before any change:**
  - the hash is checked against a private local copy, and symlinked archives are refused;
  - if a local index exists, the hash must be recorded there (`--allow-unindexed` is the documented exception for an archive from **another** server whose hash comes from an independent offline record);
  - the archive is fully validated, including the resource limits.
- **The effective PostgreSQL identity is verified.** The tool connects **exactly as `pg_restore` will** (`runuser -u applyforge_restore`, peer authentication) and requires all of the following:
  - `current_user` and `session_user` are `applyforge_restore` (this detects unexpected peer or `pg_ident` mappings);
  - no `SUPERUSER`, `CREATEROLE`, `CREATEDB`, `BYPASSRLS` or `REPLICATION`;
  - no role memberships at all, and none in `pg_execute_server_program`, `pg_read_server_files`, `pg_write_server_files`, `pg_read_all_data` or `pg_write_all_data`;
  - the role owns nothing in, and holds no **explicit** grants on, the production database (checked through `pg_shdepend` and the database owner). The default PUBLIC rights (`CONNECT`, `TEMPORARY`) are not checked; see the recommended hardening above.

  If any check cannot be completed, the restore refuses.
- **Name checks:**
  - the new database must not exist and must differ from `DB_NAME`, `postgres` and the templates;
  - the media directory must not exist, or must be empty, and must not overlap `MEDIA_ROOT`, the destination, or the work or state directories;
  - both names must be repeated with `--confirm-*`.
- **Order of changes:**
  1. Media goes into a hidden staging directory.
  2. `createdb --template=template0 --owner=applyforge_restore` runs as `postgres`, and the owner is verified.
  3. `pg_restore --single-transaction --exit-on-error --no-owner --no-privileges` runs **as `applyforge_restore`**. A malicious dump can therefore affect only:
     - the new database;
     - the restore role's own settings;
     - **other databases owned by the restore role**, meaning earlier restore copies.

     It cannot run `ALTER DATABASE`/`ALTER ROLE` against production, run server programs or touch server files. Drop restore copies once they have been reviewed, so a later untrusted restore cannot alter them.
  4. The staging directory is renamed onto the target in one atomic step.

  On failure the newly created database is left in place and reported, never dropped automatically: `sudo -u postgres dropdb <name>`.
- **Never touched:** the production database, `MEDIA_ROOT`, the running service and **ownership**.

### 12.7 Verifying restored data and the production-switch gate

Check the restored data next to production:

```bash
sudo -u postgres psql -d applyforge_restore_20261118 -c '\dt'                 # tables present, owner applyforge_restore
sudo -u postgres psql -d applyforge_restore_20261118 -c 'SELECT app, name FROM django_migrations ORDER BY id DESC LIMIT 5'
sudo -u postgres psql -d applyforge_restore_20261118 -c 'SELECT count(*) FROM tracker_profile'
sudo ls -lR /var/lib/applyforge/media-restore-20261118 | head
```

**Things to consider before using restored data:**

- **Release compatibility.** The manifest records the release that wrote the backup; run that release or a newer one.
- **Data loss window.** Everything written after the backup's timestamp is missing.

**Switching production** to restored data, including **transferring ownership**, is an operational gate that is **separately authorized** and **never automated**.

The switch works by **renaming**: the restored database becomes `applyforge` and the restored directory becomes `/var/lib/applyforge/media`, while the current ones are kept under `*-pre-restore-*` names. The environment file, the service sandbox and `backup.conf` therefore stay correct without edits.

> **Don't point `DATABASE_URL`/`MEDIA_ROOT` at the restored names instead.** The 8.4f dress rehearsal tested that approach, which an earlier version of this section described, and it fails on three counts:
> - the service sandbox allows writes only to `/var/lib/applyforge/media`, so the preflight refuses to start (`MEDIA_ROOT is not writable by this user`);
> - `backup.conf` keeps naming the old database and directory, so nightly backups would capture the abandoned data;
> - the restored database has PostgreSQL's default PUBLIC rights.

```bash
STAMP=$(date -u +%Y%m%d_%H%M%S)
NEW=applyforge_restore_20261118                    # the validated restore copy (database)
NEWMEDIA=/var/lib/applyforge/media-restore-20261118 # the validated restore copy (media)

# 1. A fresh backup of the current state (section 12.4), then stop backups and the service.
sudo systemctl start applyforge-backup.service
sudo systemctl stop applyforge-backup.timer applyforge

# 2. REASSIGN OWNED also reassigns EVERY other database the restore role owns: this must list only $NEW.
sudo -u postgres psql -Atc "SELECT datname FROM pg_database WHERE pg_get_userbyid(datdba) = 'applyforge_restore'"

# 3. Swap the databases by name and transfer ownership (as postgres). RENAME fails while any session is
#    connected to either database (e.g. monitoring); this must print nothing:
sudo -u postgres psql -Atc "SELECT datname, usename, application_name FROM pg_stat_activity WHERE datname IN ('applyforge', '$NEW')"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
ALTER DATABASE applyforge RENAME TO applyforge_pre_restore_$STAMP;
ALTER DATABASE $NEW RENAME TO applyforge;
ALTER DATABASE applyforge OWNER TO applyforge;
REVOKE CONNECT, TEMPORARY ON DATABASE applyforge FROM PUBLIC;
SQL
sudo -u postgres psql -d applyforge -c 'REASSIGN OWNED BY applyforge_restore TO applyforge'

# 4. Swap the media directories and transfer ownership.
sudo mv -T /var/lib/applyforge/media "/var/lib/applyforge/media-pre-restore-$STAMP"
sudo mv -T "$NEWMEDIA" /var/lib/applyforge/media
sudo chown -R applyforge:applyforge /var/lib/applyforge/media && sudo chmod 0750 /var/lib/applyforge/media

# 5. Preflight and migration plan (section 7) against the current release, then start.
sudo applyforge-manage /srv/applyforge/current check --deploy
sudo applyforge-manage /srv/applyforge/current migrate --plan    # restored from an older release? migrations may be listed
sudo systemctl reset-failed applyforge                           # a failed start earlier must not block this one
sudo systemctl start applyforge && sudo systemctl start applyforge-backup.timer
# health checks: section 8.1; then a backup, verified offline (section 12.6)
```

**Rollback of the switch:** stop the service and reverse the renames: `applyforge` → a new `applyforge_restore_*` name, `applyforge_pre_restore_$STAMP` → `applyforge`, and the same for the media directories. Then start again. Keep the `*-pre-restore-*` database and directory until the switch has proven itself; then drop them deliberately (`sudo -u postgres dropdb applyforge_pre_restore_$STAMP`).

**Rehearsed on 2026-11-20** (section 12.10): from the start of the restore to healthy on the restored data took about 11 s of tool time for a small fictional dataset. The service was unavailable for about 8 s during the switch, most of it the preflight and `check --deploy`. Real-data timings must come from the first production drill.

### 12.8 Disaster recovery on a new server

1. **Provision the server.** Follow sections 2–3:
   - the OS, PostgreSQL (localhost only), Python 3, `age` and nginx;
   - the `applyforge` OS account and the **non-superuser** production role:
     ```bash
     sudo useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin applyforge
     sudo -u postgres createuser --pwprompt --no-superuser --no-createdb --no-createrole applyforge
     ```
   - the **restore** account and role (section 12.6).
2. **Install the tooling.** Install a release and the backup tooling (sections 5 and 12.3) with the **public** recipients. Mount the share, read-only if you only need to restore.
3. **Retrieve the expected hash** from your **independent offline record**. The old index died with the server.
   - The new server has no index yet, so `--allow-unindexed` isn't needed until its own backups have created one.
   - **If there is no trustworthy record**, a hash computed from the share proves nothing about who made the archive. Prefer an archive whose timestamp, size and manifest (release, host, recipients fingerprint) match what you know, and treat the result as untrusted until checked.
4. **Verify offline**, then restore into a new database and directory (section 12.6). The isolated restore role means even an untrusted dump cannot affect anything else on the server.
5. **Validate** as in section 12.7, and switch **only through the authorized gate** (section 12.7).
6. **Restart backups** and verify the first one offline.

### 12.9 Manual fallback without the tool

Use this only if the tool is unavailable. It skips the tool's safeguards, so follow every step inside a private directory.

```bash
umask 077; WORK=$(mktemp -d)
cp /mnt/applyforge-backups/applyforge-backup-<timestamp>.tar.age "$WORK/archive.age"
sha256sum "$WORK/archive.age"                        # MUST equal the hash from your trusted record
age --decrypt --identity /media/offline-key/identity.txt --output "$WORK/archive.tar" "$WORK/archive.age"
tar -tvf "$WORK/archive.tar"                         # inspect: only manifest.json, database.dump, media/... regular files
mkdir "$WORK/x" && tar -xf "$WORK/archive.tar" -C "$WORK/x" --no-same-owner --no-same-permissions
(cd "$WORK/x" && sha256sum database.dump)            # compare with manifest.json; check every media file too

sudo -u postgres createdb --template=template0 --owner=applyforge_restore applyforge_restore_<date>
# Run pg_restore AS THE ISOLATED RESTORE ROLE - never as postgres, never as applyforge:
sudo runuser -u applyforge_restore -- pg_restore --no-password --exit-on-error --single-transaction \
     --no-owner --no-privileges --dbname=applyforge_restore_<date> < "$WORK/x/database.dump"
sudo cp -a "$WORK/x/media" /var/lib/applyforge/media-restore-<date>   # a NEW directory
rm -rf "$WORK"
```

**Warnings for the manual path:**

- **Don't skip the hash comparison.** age alone does not authenticate an archive's origin.
- **Check the restore role first.** Before running `pg_restore`, confirm it is still a plain, isolated role: `SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolreplication FROM pg_roles WHERE rolname = 'applyforge_restore'` must return only `f`.

### 12.10 Real-host validation still required

These cannot be validated with stand-in tools and must be checked on the actual server before production use:

- **Real `age` — validated on 2026-11-19** (age 1.2.1, Ubuntu 26.04 under WSL, throwaway identity, fictional data):
  - Encryption through the tool's stdout pipe works. The recorded SHA-256 equals the hash of age's output and of the published file.
  - `--decrypt --identity --output` restores every file byte for byte, and `verify --expected-sha256` succeeds.
  - A wrong identity, truncation, corruption (payload, last byte, header) and a missing age header are all refused.
  - age's non-zero exits (an invalid recipient, a SIGKILL mid-encryption) publish nothing, record nothing and leave no partial file, plaintext or open file descriptors.
  - The automated real-age test now runs instead of being skipped. Repeat it on the production server's own `age` build.
- **systemd sandbox — validated on 2026-11-20** (Ubuntu 26.04 under WSL, systemd 259, transient units carrying the unit file's own directives):
  - With `ProtectSystem=strict`, `PrivateDevices`, `RestrictAddressFamilies`, `SystemCallFilter=@system-service` and the capability set now applied: `runuser`/PAM, `psql`/`pg_dump` with peer authentication, `systemctl` over D-Bus, `flock` on the read-only lock, `age`, and reading a `0700` media directory owned by another account all work. Writes outside `ReadWritePaths`, user namespaces and outbound IP are denied.
  - **Negative control:** without `CAP_DAC_*` and `CAP_KILL`, reading the other account's media and killing a `postgres`-owned process fail. `CAP_SYS_RESOURCE` was not needed.
  - **Still on the production host:** the real share under the sandbox, and `RequiresMountsFor`/automount behaviour.
- **`ExecStopPost` recovery — validated on 2026-11-20** with the real tool, a disposable dummy service (a runtime unit), a tmpfs destination, a throwaway age key and a read-only `pg_dump` of the `postgres` maintenance database (26/26 checks):
  - **Normal run:** the dummy service was stopped for the capture and started again (new `ActiveEnterTimestamp`); one archive was recorded and verified with `--expected-sha256`; no plaintext, marker or `pg_dump` was left.
  - **SIGKILL during the capture:** systemd killed `runuser` and `pg_dump`; `ExecStopPost` (`recover-service`, same `$INVOCATION_ID`) started the dummy service and cleared the marker; nothing was published. The run directory left behind (its per-run lock free) was removed by a later run once older than 15 minutes.
  - **SIGTERM during the capture:** handled in-process: the service was started again, temporary files removed, exit 1, `ExecStopPost` found no marker.
  - **Service inactive before the backup:** it stayed inactive.
  - **Still on the production host:** a real `Type=notify` `applyforge.service`, a failed start, and behaviour during shutdown.
- **Timed-out `pg_dump` — fixed on 2026-11-20.** `pg_dump` runs as `runuser -u postgres -- pg_dump`, and `runuser` forks. A capture timeout used to kill only `runuser` and leave `pg_dump` running (reproduced on Ubuntu 26.04). It now runs in its own session and the whole process group is killed; a regression test fails against the old code.
- **The share — client semantics validated on 2026-11-20** with `probe-destination` against loopback NFSv4.2 (`sync`, with and without `root_squash`), SMB 3.1.1 (Samba 4.23), ext4, tmpfs and vfat (section 12.3). **Still on the production host:** the probe against the real storage, a full backup and restore through it, the storage's own snapshot/retention, and the behaviour when the storage is unreachable (a `hard` mount blocks until `TimeoutStartSec`).
- **PostgreSQL end-to-end — rehearsed on 2026-11-20.**
  - **Setup:** PostgreSQL 18.6 and age 1.2.1, Ubuntu 26.04 under WSL. Disposable databases, roles, OS account and fictional data.
  - **Backup:** the real `create` dumped a source database and its media.
  - **Verify:** `verify --expected-sha256` passed before and after the restore. The index record (name, size, SHA-256, `recorded`) matched the archive.
  - **Restore:** `restore` used a dedicated restore role and peer authentication, into a new database and a new media directory.
  - **Comparison:** schema, constraints, indexes, the view, every row (Unicode, timestamps including `infinity`, `bytea`, `jsonb`) and sequence values were **identical**. All 6 media files matched byte for byte.
  - **Restore role:** connected as itself, with no elevated attributes, memberships or dangerous predefined roles. It held no explicit privileges on the source database, owned only the restored database, and owned every relation in it.
  - **Limits of that run:**
    - the destination was a **tmpfs** mount, not NFS/SMB;
    - the service was a non-existent, inactive unit, so the stop/start maintenance window and `ExecStopPost` were **not** exercised;
    - `--allow-unindexed` was not used;
    - it was not the production host.
  - **Fixed afterwards:** the rehearsal found restored media subdirectories at `0700` instead of `0750` (the process umask). Directories are now set to `0750` explicitly, with regression tests.
  - **Still to do on the production host:** peer and `pg_ident` behaviour, the identity and `pg_shdepend` queries on its PostgreSQL version, and the PUBLIC-privilege hardening in section 12.6.
- **tmpfiles.d.** Creation as root, and the cleanup age of the work directory.
- **End-to-end dress rehearsal — 2026-11-20.** A fresh, disposable Ubuntu 26.04 WSL instance ran sections 2–13 in order with the commit `948f58b` release, fictional data and secrets, a stand-in private CA and a loopback NFSv4.2 share (`sync`, `root_squash`, mounted `hard` from fstab).
  - **Passed:**
    - deployment and the full section 8.1 table;
    - the host check (0 failures; only the firewall warning remains, which WSL's shared network namespace cannot exercise);
    - the first backup through the real unit: 1.5 s, with a **client-visible outage of about 1 s** (nginx 502s);
    - offline verification by an unprivileged account with both the primary and the escrow identity;
    - a restore drill into new targets, identical to the backup; a record written after the backup was absent, which proves the recovery point;
    - the corrected switch procedure (section 12.7): about 11 s from restore to healthy, about 8 s unavailable;
    - upgrade and rollback (sections 9–10): about 1 s outage each;
    - recovery from unplanned instance restarts (all services, and the NFS mount through systemd).
  - **Defects found and fixed:**
    - the documented switch procedure could not work: the preflight refused it, and the procedure also left backups pointing at the old data and PUBLIC rights restored;
    - `host_check.py` reported the tracked `.env.example` templates as a `.env`;
    - `host_check.py` did not report a failed `applyforge.service`;
    - the runbook now requires RFC 5280-conformant private-CA certificates, PostgreSQL on port 5432, and a delayed check after certificate reloads.
  - **Deviations:**
    - PostgreSQL ran on port 55432 with a default-socket link, because WSL shares one network namespace;
    - `createuser --pwprompt` and `sudoedit` were replaced by non-interactive equivalents;
    - the VPN address was a loopback alias;
    - the NFS server ran in the same instance.

---

## 13. Readiness checks and operational procedures (Milestone 8.4e)

`deploy/PRODUCTION_READINESS.md` records what has been validated, what has not, and the go/no-go checklist. The procedures below are the routine ones.

### 13.1 Readiness checks

| Check | When | Command | Changes anything? |
|---|---|---|---|
| Host check | after provisioning; before every deployment; after OS or PostgreSQL upgrades | `sudo python3 -I "$REL/deploy/host_check.py" --release "$REL"` (add `--strict` to fail on warnings) | no: secrets are only stat()ed, SQL is read-only, commands are read-only |
| Configuration preflight | automatically before every start and management command | `sudo applyforge-manage "$REL" check --deploy` | no |
| Destination probe | before the first backup; after any storage change | `sudo applyforge-backup probe-destination` | writes and removes its own probe directory on the share |
| Stack test | on a workstation or test VM when `deploy/nginx`, `deploy/gunicorn.conf.py` or settings change | `deploy/tests/stack_test.sh --venv <venv>` (normal user, loopback high ports) | no (temporary directory, removed) |
| Backup tests | when `deploy/backup` changes | `python3 -m unittest discover -s deploy/backup/tests` (Linux) | no |

The host check prints `PASS`, `WARN`, `FAIL`, `INFO` and `SKIP` lines and exits 1 on any `FAIL`. It never opens `/etc/applyforge/applyforge.env` or private keys, runs PostgreSQL queries as `postgres` in `default_transaction_read_only` sessions, and never starts, stops, enables or mounts anything. It covers:

- OS, systemd and clock synchronisation; CPU, memory and free space;
- tool versions; the virtualenv against `backend/constraints.txt`, and `pip check`;
- the service and restore accounts; the layout and modes of section 1; the release tree (root-owned, not writable, no `.env`); installed copies against the release;
- `systemd-analyze verify` and the app unit's exposure score;
- nginx: `nginx -t`, the default site, listen addresses, leftover placeholders, certificate expiry and host name;
- listening sockets and the firewall policy;
- PostgreSQL: version, `listen_addresses`, `password_encryption`, `pg_hba` rules, peer authentication, role attributes and memberships, database owner and PUBLIC rights;
- backup configuration, mount, recipients (no private key) and the age of the last recorded backup.

### 13.2 Logs and what to watch

| Source | Command | Watch for |
|---|---|---|
| ApplyForge (Gunicorn, Django) | `journalctl -u applyforge -f` | `preflight: FAILED`, tracebacks, worker timeouts, repeated restarts (`systemctl show -p NRestarts applyforge`) |
| nginx | `/var/log/nginx/applyforge.{access,error}.log` | 5xx bursts, 403s from VPN addresses (allowlist), TLS handshake errors |
| Backups | `journalctl -u applyforge-backup` | exit 1 (failed), **2** (marker needs investigation), **3** (ApplyForge down), **4** (retention failed); no run in 36 hours |
| PostgreSQL | `journalctl -u postgresql@18-main` and `/var/log/postgresql/` | authentication failures, connection-limit errors, disk full |
| System | `journalctl -p warning -b`, `df -h`, `systemctl --failed` | full disks (database, work directory, share), failed units, OOM kills |

Set journald's `SystemMaxUse=` so logs cannot fill `/var`. Alert at least on: `applyforge` not active, `applyforge-backup` failed or not run for 36 hours, the health check failing, disk usage above 80 %, and certificate expiry within 30 days.

### 13.3 Routine maintenance

- **Monthly.** Run the host check. Apply OS security updates (`sudo apt update && sudo apt upgrade`), then `sudo systemctl restart applyforge` and the health checks (section 8.1). A PostgreSQL **minor** update restarts PostgreSQL; ApplyForge reconnects on the next request.
- **PostgreSQL major upgrade.** A separate, planned change: back up, verify the archive offline, upgrade with `pg_upgradecluster`, run the host check, take a new backup.
- **Python.** Release virtualenvs use the system interpreter. After a Python patch update, run `sudo "$REL/.venv/bin/pip" check` and the health checks. A new Python minor version needs a new release (section 5).
- **Dependencies.** Change `backend/requirements.txt`, regenerate `backend/constraints.txt` in a fresh venv (`pip freeze`), run the backend tests and `pip-audit`, and ship a new release.
- **Certificate renewal.** Install the new certificate and key with the same paths and modes, run `sudo nginx -t && sudo systemctl reload nginx`, then check the expiry from a VPN client with a **new** connection, a few seconds later. `reload` returns before the old nginx workers have exited, and they keep serving the previous certificate until then. The 8.4f dress rehearsal saw a check fail for that reason right after a reload.
- **Quarterly restore drill.** Restore the latest archive into a new database and directory (section 12.6), validate it (section 12.7), then drop the copy (`sudo -u postgres dropdb <copy>`) and remove the directory.
- **Releases.** Keep two or three; delete older ones (section 9).

### 13.4 Security incidents

Contain first, then investigate. Keep notes with times.

| Incident | Immediate actions |
|---|---|
| **API token disclosed** | Generate a new `API_TOKEN` (section 3.2), restart ApplyForge, give users the new token over a trusted channel. Review the nginx access log for unexpected VPN addresses. |
| **`SECRET_KEY` disclosed** | Replace it and restart. Signed values become invalid; API clients use the token and are not affected. |
| **Database password disclosed** | `sudo -u postgres psql -c "\password applyforge"`, update `DATABASE_URL`, restart. Confirm `listen_addresses` and `pg_hba` still limit access to localhost. |
| **A VPN client's credentials lost** | Revoke that peer in the VPN and rotate the API token. |
| **age private key exposed** | Create a new key pair offline, replace the recipients file, take and verify a new backup. Treat every older archive as readable by whoever holds the key; delete them once the new backups are verified if that data must not be exposed. |
| **age private key lost** | Create a new key pair; older archives cannot be decrypted (section 12.2). Take and verify a new backup now. |
| **Server suspected compromised** | Disconnect it from the VPN (keep it powered for forensics if needed). Don't trust its local index; use offline hash records. Rebuild on a new server (section 12.8) from an archive verified offline. Rotate **all** secrets: token, `SECRET_KEY`, database password, TLS key, VPN keys, and the age key if it was ever on that server. |
| **Backup storage compromised** | Archives stay confidential and tampering is detected by the trusted hashes. Take a new backup to clean storage, verify it, and verify any older archive against offline records before using it. |

### 13.5 Failures and disaster recovery

| Failure | Procedure | Data loss |
|---|---|---|
| ApplyForge does not start | `journalctl -u applyforge -n 100`; fix the reported preflight variable or roll back the release (section 10); then `sudo systemctl reset-failed applyforge` | none |
| PostgreSQL down or damaged | Restart it. If the data directory is damaged, restore the latest verified archive into a new database (section 12.6) and switch through the gate (section 12.7) | since the last backup |
| Disk full | Find the cause (`sudo du -xh /var \| sort -h \| tail`); journald and `BACKUP_WORK_DIR` are the usual suspects. Never delete files under `/var/lib/postgresql` by hand | none if handled before PostgreSQL stops |
| Server lost | Section 12.8 on a new server, with an archive verified offline against the offline hash record | since the last backup |
| Backup storage lost | Provision new storage, mount it, add the marker, run `probe-destination`, take and verify a new backup | none (history only) |

Recovery objectives with the shipped configuration: **RPO 24 hours** (daily backup at 03:30) and an **RTO of a few hours** for a rebuild (provisioning, restore, validation). Back up more often only after measuring the maintenance window, because each backup stops ApplyForge briefly.

---

## 14. VPN, firewall and administrative access (rehearsed 2026-11-21)

This is the configuration exercised in the network rehearsal: three QEMU/KVM VMs (server, VPN client, outsider) on an isolated virtual LAN, running Ubuntu 26.04. Results are in `deploy/PRODUCTION_READINESS.md` section 3b. The VPN here is WireGuard. If you use another VPN, keep the same properties.

**Properties.**

- The server's VPN address is `10.99.0.1`.
- From outside the VPN, nothing but the WireGuard UDP port answers. All other traffic is dropped silently, not refused.
- SSH (keys only, no root), HTTP and HTTPS are accepted **only on the `wg0` interface**.
- nginx and the firewall come back on their own after every reboot.

### 14.1 WireGuard on the server

```bash
sudo apt install wireguard-tools
umask 077; wg genkey | sudo tee /etc/wireguard/server.key | wg pubkey | sudo tee /etc/wireguard/server.pub
sudoedit /etc/wireguard/wg0.conf                 # 0600 root
#   [Interface]
#   Address = 10.99.0.1/24
#   ListenPort = 51820
#   PrivateKey = <contents of /etc/wireguard/server.key>
#   [Peer]                                       # one block per admin/user device
#   PublicKey = <the device's public key>
#   AllowedIPs = 10.99.0.2/32
sudo systemctl enable --now wg-quick@wg0
```

Each client device has its own key pair and `Address = 10.99.0.N/24`. Its peer block for the server has `Endpoint = <server public or LAN address>:51820`, `AllowedIPs = 10.99.0.0/24` and `PersistentKeepalive = 25`. Enable `wg-quick@wg0` on clients that should reconnect after a reboot.

- **To add a device:** add a `[Peer]` block, then `sudo systemctl restart wg-quick@wg0`. The restart briefly interrupts every tunnel, so do it from the console or be ready to reconnect. `wg syncconf` avoids the interruption.
- **To remove a device:** delete its block and restart. A key the server does not list gets no handshake and no access (rehearsal N6).

### 14.2 Firewall

```bash
sudo apt install nftables
sudo nft -c -f "$REL/deploy/nftables/applyforge.nft"      # syntax check only; changes nothing
sudo install -m 0644 "$REL/deploy/nftables/applyforge.nft" /etc/nftables.conf
sudo systemctl enable --now nftables
sudo nft list ruleset | grep 'policy drop'                # input and forward chains
```

`flush ruleset` in that file removes every other nftables table, so this assumes a dedicated server. If anything else manages rules (Docker, ufw, a cloud agent), merge the `applyforge` table instead. The host check reports `PASS nftables has an input chain with policy drop`.

### 14.3 Changing the firewall safely (automatic rollback)

A wrong ruleset can lock you out of SSH. The rehearsal's lock-out ruleset also dropped the WireGuard port, so the tunnel went down too. **Always apply changes with a rollback timer**, from an SSH session through the VPN:

```bash
sudo nft -c -f /tmp/new.nft                                                    # 1. syntax check
sudo systemd-run --unit=fw-rollback --on-active=120 /usr/sbin/nft -f /etc/nftables.conf   # 2. arm the rollback
sudo nft -f /tmp/new.nft                                                       # 3. apply (live only)
# 4. From a SECOND, new SSH session through the VPN, confirm SSH and HTTPS still work. Then:
sudo systemctl stop fw-rollback.timer                                          # 5. confirm (cancel the rollback)
sudo install -m 0644 /tmp/new.nft /etc/nftables.conf                           # 6. persist
```

If you are locked out, do nothing. When the timer fires, the saved `/etc/nftables.conf` is restored. The rehearsal used a 60 s timer, and access returned 64 s and 87 s after applying (the timer plus the WireGuard re-handshake). With the 120 s timer above, expect about 2–2.5 minutes. A change that was never persisted (step 6) is also undone by any reboot.

### 14.4 Console recovery

When SSH is unreachable and no rollback timer was armed:

1. Open the **provider/hypervisor console** (serial or VNC). This is out of band and does not use the network.
2. Log in as the admin account with its **console password**. SSH accepts keys only, so this password is useful only on the console. Store it in the password manager, not on the server.
3. Run `sudo nft -f /etc/nftables.conf` (restores the saved ruleset), or `sudo systemctl restart nftables`.
4. Alternatively, **reboot from the console or hypervisor**. The bad ruleset was live only, and the saved file is loaded at boot.

Both paths were rehearsed (F2 and F4). Before go-live, confirm that the provider console works and that the console password is known.

### 14.5 Boot order

- **nginx after the VPN.** nginx listens on `10.99.0.1`, which exists only once `wg-quick@wg0` has run. Without the drop-in from section 6 (`deploy/systemd/nginx-applyforge-vpn.conf`), nginx can start first, fail with `bind() to 10.99.0.1:443 failed (99: Cannot assign requested address)` and **stay down** until restarted by hand. The rehearsal reproduced this; the host check now fails when the ordering is missing.
- **DNS configuration.** On Ubuntu 26.04, netplan runs `systemd-networkd-wait-online --dns`. If the server's network configuration has **no DNS server**, boot waits the full 120 s timeout before `network-online.target`, and with it the VPN, nginx and ApplyForge. Make sure netplan (or DHCP) provides a resolver.
- **Restarting nginx while `wg0` is down** fails for the same reason as at boot. Bring the VPN up first: `sudo systemctl start wg-quick@wg0`.

---

## 15. Production architecture: two servers, private DNS, ACME, sealed backups, monitoring

> **Status: rehearsed in disposable VMs, NOT verified on production infrastructure.** Every procedure in this section ran in the two-server rehearsal of 2026-11-21 (`deploy/PRODUCTION_READINESS.md` section 3c). That rehearsal used QEMU/KVM VMs on an isolated network, a fictional domain, Let's Encrypt's test server (Pebble) and a mock Healthchecks receiver. Real DNS providers, Let's Encrypt, Healthchecks.io, Hetzner and real devices remain unverified (`deploy/INFRASTRUCTURE_DECISIONS.md`, register D).

Layout:

```
 Internet ──UDP 51820──▶ APP SERVER                                 BACKUP SERVER (other country)
                          wg0 10.99.0.1/24  (devices: SSH, HTTPS, DNS)  wg0 10.97.0.1/24 (admin devices only: SSH)
                          wg1 10.98.0.1  ═══ UDP 51821 (app's IP only) ═══▶ wg1 10.98.0.2 (NFS 2049 from 10.98.0.1 only)
                          nginx, Gunicorn, PostgreSQL, unbound          /srv/applyforge-backups  (export: drop box)
                          NFS client: /mnt/applyforge-backups (hard)    /srv/applyforge-vault    (root-only, NOT exported)
```

Repository files used here:

| Path | Purpose |
|---|---|
| `deploy/nftables/applyforge.nft` | app server ruleset (now including DNS on `wg0`) |
| `deploy/nftables/applyforge-backup-server.nft` | backup server ruleset |
| `deploy/unbound/applyforge.conf` | private resolver configuration |
| `deploy/systemd/unbound-applyforge-vpn.conf`, `nfs-server-applyforge-vpn.conf` | start-order drop-ins |
| `deploy/acme/` | ACME wrapper, deploy hook, renewal units |
| `deploy/monitoring/` | ping and check scripts, units |
| `deploy/systemd/applyforge-seal.{service,timer}` | backup server seal |

### 15.1 Private DNS for VPN clients

The app hostname, for example `applyforge.<domain>`, **has no public A/AAAA record**; only the ACME challenge CNAME is public (15.2). VPN clients resolve the name through unbound on the app server.

```bash
sudo apt install unbound unbound-anchor
# Fresh Ubuntu 26.04: the packaged DNSSEC config needs root.key BEFORE unbound-checkconf works (rehearsal finding).
sudo unbound-anchor -a /var/lib/unbound/root.key; sudo chown unbound: /var/lib/unbound/root.key
sudo install -m 0644 "$REL/deploy/unbound/applyforge.conf" /etc/unbound/unbound.conf.d/applyforge.conf
sudoedit /etc/unbound/unbound.conf.d/applyforge.conf          # replace <applyforge.example.org>
sudo install -d /etc/systemd/system/unbound.service.d
sudo install -m 0644 "$REL/deploy/systemd/unbound-applyforge-vpn.conf" /etc/systemd/system/unbound.service.d/applyforge-vpn.conf
sudo unbound-checkconf && sudo systemctl daemon-reload && sudo systemctl enable --now unbound
dig +short @10.99.0.1 applyforge.<domain>                     # from a VPN client: 10.99.0.1
```

- **Firewall.** `deploy/nftables/applyforge.nft` already accepts DNS (53/udp and 53/tcp) on `wg0` only. Apply changes with the rollback procedure (14.3).
- **Laptops: split DNS.** Only the app name goes to the VPN resolver, so public DNS keeps working when the server is down. Add to the device's `wg0.conf`:

  ```
  PostUp = resolvectl dns %i 10.99.0.1; resolvectl domain %i ~applyforge.<domain>
  ```

  **Do not add `PreDown = resolvectl revert %i`.** On Ubuntu 26.04 the AppArmor `wg-quick` profile denies the `RevertLink` D-Bus call, so the `PreDown` fails and wg-quick aborts **before removing the interface**. The tunnel then cannot be brought down, and the next start fails with "wg0 already exists". This was reproduced in the rehearsal. Without `PreDown`, systemd-resolved drops the link's DNS settings when wg-quick deletes the interface.
- **Phones: route-all.** Use the WireGuard app's `DNS = 10.99.0.1`. While the tunnel is up, all DNS goes through the server: if the server or unbound is down, the phone has no DNS until the tunnel is switched off.
- **Caching.** Devices cache the answer for the record's TTL. The template sets 300 s; unbound's default would be 3600 s. After the tunnel goes down, the name keeps resolving (to an unreachable address) until the cache expires.
- **Recovery, from least to most invasive:**
  1. `sudo systemctl restart unbound` on the server. unbound also restarts itself after a crash (rehearsed).
  2. On an admin laptop, add a hosts entry `10.99.0.1 applyforge.<domain>`. HTTPS works without the resolver; remove the entry afterwards.
  3. Switch the tunnel off to get normal DNS back.

  The `dns` monitoring check (15.6) alerts on resolver failure independently of the application health check.

### 15.2 TLS: Let's Encrypt via DNS-01 (CNAME delegation, scoped credential)

**Public DNS, done once by the owner:**

- `_acme-challenge.applyforge.<domain>  CNAME  applyforge.<validation-zone>.`
- The validation zone is a separate zone (or a separate DNS account) holding nothing but ACME TXT records.
- Create an API credential **limited to TXT records in the validation zone**. In the rehearsal, a TSIG key with `update-policy { grant <key> zonesub TXT; }` could add TXT records there, and was refused for an A record in the owned zone and for non-TXT records in the validation zone.

```bash
sudo apt install lego
sudo install -m 0755 "$REL/deploy/acme/applyforge-acme" "$REL/deploy/acme/applyforge-cert-deploy" /usr/local/sbin/
sudo install -d -m 0700 /etc/applyforge/acme
sudo install -m 0600 "$REL/deploy/acme/acme.env.example" /etc/applyforge/acme/acme.env
sudoedit /etc/applyforge/acme/acme.env              # domain, e-mail, provider + its scoped credential; STAGING server first
sudo install -m 0644 "$REL/deploy/monitoring/monitoring.env.example" /etc/applyforge/monitoring.env   # APP_HOST, VPN_ADDR
sudo applyforge-acme issue                           # issues, then the deploy hook installs it for nginx
# after a successful staging run: set ACME_SERVER to the production URL in acme.env, remove the staging account
# directory (/etc/applyforge/acme/lego/accounts/acme-staging-*), run issue again
sudo install -m 0644 "$REL/deploy/acme/applyforge-acme-renew.service" "$REL/deploy/acme/applyforge-acme-renew.timer" /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now applyforge-acme-renew.timer
```

- **Credential handling.** It is passed to lego only through the environment, never on a command line (tested).
- **The deploy hook (`applyforge-cert-deploy`):**
  1. checks that the certificate and key match, that the certificate covers the host and that it is currently valid;
  2. installs the files atomically;
  3. **rolls back** if `nginx -t` fails;
  4. reloads nginx, waits, and checks the served serial on a **new** connection.

  Any failure exits non-zero, and the unit's `OnFailure=` alerts. Rehearsed:
  - issuance through the CNAME;
  - a forced renewal (the new serial was served);
  - a renewal with a broken nginx config (rolled back; the old certificate kept serving).
- **Forcing a renewal** (for example after a key exposure): `sudo applyforge-acme renew --days 100000`. Setting `ACME_RENEW_DAYS` in the environment does **not** work, because `acme.env` overrides it (rehearsal finding).
- **Private CA alternative:** section 2. It is verified in the earlier rehearsals and needs CA installation on every device.

### 15.3 Backup server, backup link and sealed archives

**Backup server** (smallest amd64 plan, Ubuntu 26.04, Hetzner Backups enabled):

```bash
sudo apt install nfs-kernel-server wireguard-tools nftables age
# wg0.conf: Address 10.97.0.1/24, ListenPort 51820, [Peer] per admin device (AllowedIPs 10.97.0.N/32)
# wg1.conf: Address 10.98.0.2/30, ListenPort 51821, [Peer] app server link key (AllowedIPs 10.98.0.1/32)
sudo systemctl enable --now wg-quick@wg0 wg-quick@wg1
sudo install -d -m 0700 -o nobody -g nogroup /srv/applyforge-backups      # export = drop box (root_squash -> nobody)
sudo install -d -m 0700 -o root -g root /srv/applyforge-vault             # vault: root only, NEVER exported
echo "/srv/applyforge-backups 10.98.0.1(rw,sync,root_squash,no_subtree_check,sec=sys)" | sudo tee /etc/exports.d/applyforge.exports
printf '[nfsd]\nvers3=n\n' | sudo tee /etc/nfs.conf.d/applyforge.conf      # NFSv4 only
sudo install -d /etc/systemd/system/nfs-server.service.d
sudo install -m 0644 "$REL/deploy/systemd/nfs-server-applyforge-vpn.conf" /etc/systemd/system/nfs-server.service.d/applyforge-vpn.conf
sudo systemctl daemon-reload && sudo systemctl restart nfs-server && sudo exportfs -ra
sudo nft -c -f "$REL/deploy/nftables/applyforge-backup-server.nft"         # edit APP_PUBLIC_V4 first; apply with 14.3
sudo install -m 0755 "$REL/deploy/backup/afbackup.py" /usr/local/sbin/applyforge-backup
sudo install -m 0644 "$REL/deploy/systemd/applyforge-seal.service" "$REL/deploy/systemd/applyforge-seal.timer" /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now applyforge-seal.timer
```

**App server (NFS client):**

- `wg1.conf`: `Address 10.98.0.1/30`, `[Peer]` backup link key, `Endpoint <backup public IP>:51821`, `AllowedIPs 10.98.0.2/32`, `PersistentKeepalive 25`.
- fstab:

  ```
  10.98.0.2:/srv/applyforge-backups /mnt/applyforge-backups nfs4 vers=4.2,hard,proto=tcp,_netdev,x-systemd.requires=wg-quick@wg1.service 0 0
  ```

- Then the marker file and `probe-destination` (12.3).

**Why the vault.** With `root_squash`, the app server's root writes the archives as `nobody`, so a compromised app server **can delete or overwrite archives on the export**; the rehearsal confirmed this. `applyforge-backup seal`, running hourly on the backup server:

- copies every finished archive (at least 10 minutes old) into the vault (`0400`, root-only, not exported) and re-hashes the source after copying;
- applies retention there (30 days, 7 newest always kept);
- **exits 1 (alert) if an archive on the export changed after it was sealed**, or if an archive-like file cannot be sealed (a planted symlink, no age header, persistent lock contention).

What the rehearsal showed:
- An archive deleted or altered through the app server stayed intact in the vault.
- The app server could not mount the vault or `/srv`.
- It could not SSH to the backup server, even holding the admin key.
- Of the 20 likely service ports probed, it could reach only port 2049 on the link address.

**NFS delegations.** Just after publication, the NFS server may hold a client delegation on the archive. A local open then fails with `EAGAIN` until the lease is recalled. The seal retries for about 30 s. The first version skipped silently with exit 0 and left the archive unprotected; a regression test now guards this.

**Clearing a seal alert** (`seal` check down, "differs from its sealed copy"):
1. Treat it as a possible compromise of the app server (13.4).
2. Compare the export copy and the vault copy against the trusted hash in the offline record (15.4). The vault copy is the one to keep.
3. On the backup server, replace the export copy with the vault copy:

   ```
   sudo install -o nobody -g nogroup -m 0600 /srv/applyforge-vault/<name> /srv/applyforge-backups/<name>
   ```

4. The next seal run passes, and the check returns to up.

**Backup server unreachable.** Any access to a `hard` mount blocks until the server returns. The backup unit therefore has a fast-fail pre-check: `ExecStartPre=/usr/bin/timeout -s KILL 30 /usr/bin/stat -f /mnt/applyforge-backups`.
- Rehearsed: the unit failed after 31 s, ApplyForge stayed up, and the failure alert fired.
- Without the pre-check, the unit hung until `TimeoutStartSec` (6 h) with no alert.
- When the server returns, the mount resumes by itself; no remount is needed.

**Restoring from the vault** (the export copy is missing or damaged): on the backup server, run `sudo install -o nobody -g nogroup -m 0600 /srv/applyforge-vault/<name> /srv/applyforge-backups/<name>`, then restore on the app server as in 12.6, with the hash from the offline record.

### 15.4 Monthly offline copy and independent verification

From an admin device; each server is reached through its own tunnel:

```bash
D=~/applyforge-offline/$(date +%Y-%m); mkdir -p "$D" && chmod 700 "$D"
ssh admin@10.99.0.1 'sudo cat /var/lib/applyforge-backup/index.tsv' > "$D/index.tsv"            # the trusted hash record
NEWEST=$(ssh admin@10.97.0.1 'sudo ls /srv/applyforge-vault' | grep '^applyforge-backup-' | sort | tail -n 1)
ssh admin@10.97.0.1 "sudo cat /srv/applyforge-vault/$NEWEST" > "$D/$NEWEST"                      # the sealed copy
HASH=$(awk -F'\t' -v n="$NEWEST" '$2==n && $7=="recorded"{h=$3} END{print h}' "$D/index.tsv")
applyforge-backup verify --archive "$D/$NEWEST" --identity <identity file> --expected-sha256 "$HASH" \
     --work-dir "$D" --lock-file "$D/verify.lock"
```

- Rehearsed with both the primary and the escrow identity.
- A vault copy that had been altered before sealing was **refused** by the trusted hash. The seal cannot know the trusted hash, so the offline record is what catches this case.
- Run it on Linux/macOS, or in WSL on Windows. Keep the last three months.

### 15.5 Key custody, access test and recovery

- **Generate both age identities offline, on your own computer:** the primary (A) and the escrow (B). Put **only** the two public recipients on the server (12.2).
  - A goes into the password manager, as a secure note.
  - B is printed on paper (one 74-character line) and stored in a separate physical location.
  - Delete the files.
- **Quarterly access test:** open each identity in a temporary file on an offline device, run `age-keygen -y <file>`, compare the output with a line of `/etc/applyforge/backup-recipients.txt`, then delete the file. Better still, run 15.4 with each identity in turn; both were rehearsed.
- **Yearly:** a restore drill with identity B (12.6).
- **Primary key lost:** use B → create a new pair → replace the recipients → take and verify a new backup → print a new escrow.
- **Exposure:** section 13.4.
- **Server identity, also kept in the password manager** (none of it is in the backups):
  - the app server's `wg0` and `wg1` private keys and the backup server's `wg0`/`wg1` keys. With them, a rebuilt server is reachable by existing devices without re-enrolment (rehearsed, 15.8);
  - `/etc/applyforge/acme/acme.env` (the scoped DNS credential);
  - `/etc/applyforge/healthchecks.env`;
  - optionally the SSH host keys. Without them, admins must re-pin the host key after a rebuild (rehearsed: "REMOTE HOST IDENTIFICATION HAS CHANGED").

  `SECRET_KEY`, `API_TOKEN` and the database password are regenerated on a rebuild; users re-enter the new API token.

### 15.6 Monitoring (Healthchecks.io dead-man's switch)

```bash
sudo install -m 0755 "$REL/deploy/monitoring/applyforge-ping" "$REL/deploy/monitoring/applyforge-check" /usr/local/sbin/
sudo install -m 0600 "$REL/deploy/monitoring/healthchecks.env.example" /etc/applyforge/healthchecks.env   # then sudoedit: real ping URLs
sudo install -m 0644 "$REL/deploy/monitoring/monitoring.env.example" /etc/applyforge/monitoring.env      # then sudoedit
sudo install -m 0644 "$REL"/deploy/monitoring/applyforge-check@.service "$REL"/deploy/monitoring/applyforge-ping-fail@.service \
     "$REL"/deploy/monitoring/applyforge-check-{health,dns,disk,cert}.timer /etc/systemd/system/   # backup server: -heartbeat, -disk
sudo install -d /etc/systemd/system/applyforge-backup.service.d
sudo install -m 0644 "$REL/deploy/monitoring/applyforge-backup-monitoring.conf" /etc/systemd/system/applyforge-backup.service.d/monitoring.conf
sudo systemctl daemon-reload && sudo systemctl enable --now applyforge-check-{health,dns,disk,cert}.timer
```

| Check | Server | Period / grace (Healthchecks) | Fires when |
|---|---|---|---|
| `health` | app | 5 min / 10 min | the health endpoint fails, or the server/timer is dead |
| `dns` | app | 5 min / 10 min | the private resolver fails |
| `disk` | app | 1 h / 2 h | a watched filesystem is at 80 % or more |
| `cert` | app | 1 d / 1 d | the certificate expires within 21 days (renewal also alerts via `OnFailure`) |
| `backup` | app | 1 d / 3 h | a backup fails (`OnFailure`), or no successful backup within 27 h |
| `heartbeat`, `backup-disk` | backup | 15 min / 30 min, 1 h / 2 h | the backup server is down or its disk is filling |
| `seal` | backup | 1 h / 2 h | tampering, an unsealable archive, or seal not running |

Rehearsed against a mock receiver:
- every check up;
- application, resolver, disk and certificate failures, each followed by recovery ("up" notifications);
- seal tampering;
- **missed heartbeats with a whole server powered off**, which shows monitoring is independent of the monitored server;
- backup age.

No real account was created.

### 15.7 Emergency access

1. **Provider account:** 2FA (TOTP; add a security key or passkey if offered). Recovery codes printed and stored with the escrow key.
2. **Console:** before the first firewall change, open the provider console on **both** servers and log in with the console password.
3. **Second admin device:** enrolled on both `wg0` interfaces with its own keys, kept for emergencies.
4. **Firewall mistake:** rollback timer (14.3), then console (14.4).
5. **WireGuard unavailable:**
   1. Console → `systemctl status wg-quick@wg0`, `journalctl -u wg-quick@wg0 -b`, `wg show`, `nft list ruleset | grep 51820`.
   2. **"wg0 already exists"** (an interface left behind, e.g. by a failed `PreDown` on a client) → `sudo ip link delete wg0 && sudo systemctl reset-failed wg-quick@wg0 && sudo systemctl start wg-quick@wg0`.
   3. Broken config → restore `/etc/wireguard/wg0.conf.bak`. Make that copy before every change.
   4. Check the provider firewall still allows UDP 51820/51821 to the right IPs.
   5. **Time-boxed last resort:**
      - allow TCP 22 at the provider firewall from your current IP only;
      - on the console, run `systemd-run --on-active=1800 /usr/sbin/nft -f /etc/nftables.conf`, then `nft insert rule inet applyforge input ip saddr <your-ip> tcp dport 22 accept`;
      - fix the problem;
      - remove the provider rule.
   6. Rescue system, or a Backups/snapshot restore, if the system does not boot.
6. **All admin devices lost:** console → new key on a new device → add its `[Peer]` on both servers → restart the tunnels.

### 15.8 Application server lost: rebuild and restore

The rehearsal killed the app server, **deleted its disk** and rebuilt it. Timings are in `PRODUCTION_READINESS.md` 3c.

1. **Provision** a new server, then follow sections 2–8 with the current release.
2. **Restore the server identity from the password manager:** the WireGuard keys, so devices and the backup link work unchanged; then `acme.env` and `healthchecks.env`.
3. **Check the clock first:** `timedatectl show -p NTPSynchronized --value` must print `yes`. DNS-01 through a TSIG/RFC 2136 credential fails with "bad authentication" when the clock is more than 5 minutes off; the rehearsal's rebuilt VM was 1.8 h off. Then install unbound (15.1), the ACME tooling (15.2) and monitoring (15.6), and run `applyforge-acme issue`.
4. Wait for the NFS mount (fstab, after `wg1`) and run `probe-destination`.
5. **Choose the archive and its hash from the offline record** (15.4); the old index died with the server.
6. **Restore** into a new database and directory (12.6), then **switch** (12.7).
7. **Verify** HTTPS from a device.
   - Re-pin the SSH host key (`ssh-keygen -R 10.99.0.1`) unless it was restored.
   - Give users the new API token.
8. **Take a backup** and check that it is sealed. Re-enable the timers.
