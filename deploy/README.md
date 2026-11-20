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

- **Server.** A current Ubuntu LTS (or Debian stable) host with systemd ≥ 247 and nginx ≥ 1.19.4 (needed for `ssl_reject_handshake`).
- **Python 3.12.** This matches the version the test suite ran on. Install `python3-venv`.
- **PostgreSQL 14 or later** on the same host, listening on `localhost` only. The Milestone 8.4b rehearsal used PostgreSQL 18.6.
- **A working VPN.** The server needs a fixed VPN address (example: `10.99.0.1`), and clients need a known subnet (example: `10.99.0.0/24`). The host firewall must not expose ports 80 or 443 on any public interface. nginx binds to the VPN address only, but the firewall is the primary control.
- **Name resolution.** A private host name (example: `applyforge.vpn.example`) must resolve to the VPN address for VPN clients, through internal DNS or a hosts file.
- **TLS certificate.** You need a certificate and key for that host name, typically from a private CA that the client devices trust. Install them as `/etc/ssl/applyforge/applyforge.crt` (`0644`) and `/etc/ssl/applyforge/applyforge.key` (`0600`, `root`).
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
sudo python3.12 -m venv "$REL/.venv"
sudo "$REL/.venv/bin/pip" install --no-cache-dir --upgrade pip
sudo "$REL/.venv/bin/pip" install --no-cache-dir -r "$REL/backend/requirements.txt"
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

The tool is `deploy/backup/afbackup.py`, installed as `/usr/local/sbin/applyforge-backup`, with four commands: `create`, `verify`, `restore` and `recover-service`.

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
| No-clobber publication: hard link, or `RENAME_NOREPLACE` | hard links: yes | hard links only with Unix extensions or an NTFS backend; **`RENAME_NOREPLACE` is usually unsupported**, so without hard links the backup fails closed | hard links need the server's `hardlink@openssh.com` extension (OpenSSH has it) |
| Stable inode identity (`O_NOFOLLOW` plus inode checks) | yes | mount with **`serverino`** (with `noserverino`, inode numbers are synthesized and checks may fail) | yes |
| Durability of `fsync`/`close` | depends on the export: use **`sync`** | depends on the server (strict sync) | depends on the SFTP server |
| Root writes | with `root_squash`, files are written as the squashed user, so make the export directory writable by it | as the mount's credentials | as the remote user |

**Recommended:** NFSv4 with a `sync` export.

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
```

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

**Sandbox.** The unit uses `ProtectSystem=strict`, with write access only to the destination, state and work directories, plus `RestrictAddressFamilies`. The capability limit is listed in the unit but **not applied** until host testing (section 12.10).

### 12.4 Backup procedure, maintenance window and service recovery

```bash
sudo systemctl start applyforge-backup.service       # preferred, also for manual backups (see below)
journalctl -u applyforge-backup -n 50 --no-pager     # "archive ... sha256=<64 hex> ... recorded", "backup complete: ..."
systemctl is-active applyforge                       # active again (if it was before)
sudo cat /var/lib/applyforge-backup/index.tsv        # created, name, sha256, size, recipients fingerprint, host, status
```

1. **Keep an independent copy of the hashes.** The index is the primary trusted record but is lost with the server. Regularly copy `index.tsv`, or the `sha256=` journal lines, to the offline machine or another trusted place that **isn't** the backup share. Only `recorded` lines whose name has no later `failed` line are valid.
2. **Run manual backups through the unit.** Use `systemctl start applyforge-backup.service` rather than `sudo applyforge-backup create` in a terminal. The unit runs detached from your SSH session and has `ExecStopPost` recovery. A terminal run still handles SIGHUP, SIGINT and SIGTERM, and a closed terminal, by restarting ApplyForge before exiting. But a **SIGKILL** of a terminal run can only be recovered manually.
3. **First backup.** Run it, copy the archive and its hash to the offline machine, and **verify it there with `--expected-sha256`** (section 12.6). Only then enable the timer: `sudo systemctl enable --now applyforge-backup.timer`.
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

**Switching production** to restored data, including **transferring ownership**, is a future operational gate that is **separately authorized** and **never automated**:

1. Take a fresh backup of the current state.
2. Stop ApplyForge.
3. Transfer ownership deliberately, as `postgres`, for the one database being switched:
   ```bash
   sudo -u postgres psql -d applyforge_restore_20261118 -c 'REASSIGN OWNED BY applyforge_restore TO applyforge'
   sudo -u postgres psql -c 'ALTER DATABASE applyforge_restore_20261118 OWNER TO applyforge'
   ```
   `REASSIGN OWNED` also reassigns **every other database** the restore role owns, so drop or separately review other restore databases first.
4. Change the media ownership: `sudo chown -R applyforge:applyforge /var/lib/applyforge/media-restore-20261118`.
5. Point `DATABASE_URL` and `MEDIA_ROOT` in `/etc/applyforge/applyforge.env` at the restored database and directory, leaving the old ones in place.
6. Run the preflight and `migrate --plan` (section 7), then start the service and run the health checks (section 8.1).

Keep the previous data until the switch has proven itself.

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
- **systemd sandbox.**
  - `ProtectSystem=strict` with `ReadWritePaths`, `PrivateDevices` and `RestrictAddressFamilies` together with `runuser`/PAM (`pam_systemd`, `pam_limits`), `systemctl` over D-Bus and the mounted share.
  - Whether `RequiresMountsFor`/automount behaves as expected.
  - Which capabilities are really needed, before applying the commented `CapabilityBoundingSet`.
- **`ExecStopPost` recovery.**
  - `$INVOCATION_ID` matching.
  - The timestamps of a real `applyforge.service`, including `Type=notify` and a failed start.
  - Behaviour during shutdown.
- **The share.** Not yet validated. The rehearsal below used a local **tmpfs**, which proves nothing about NFS/SMB/sshfs.
  - `O_EXCL`, hard links or `RENAME_NOREPLACE`, inode stability and `fsync` semantics on the chosen mount (section 12.3).
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
