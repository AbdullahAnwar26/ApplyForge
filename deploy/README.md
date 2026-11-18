# ApplyForge deployment runbook: private/VPN Linux server

Milestone 8.4c. This runbook covers deploying ApplyForge to **one Linux server that is reachable only over a VPN**. It is a first configuration and has not been used on a real server yet.

| In scope | Not in scope |
|---|---|
| Gunicorn, systemd, nginx and environment configuration; release layout; migrations; static files; health checks; upgrades; **code** rollback | Server provisioning, VPN setup, certificate issuance, monitoring, **backups and database restore (Milestone 8.4d)**, public exposure |

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
   - If the plan lists migrations, take a database backup before continuing. The backup and restore procedure belongs to Milestone 8.4d; until it exists, create the backup deliberately by hand.
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
  - **Restore the database from a backup.** This is a separate operation (Milestone 8.4d) that must be explicitly authorized, and it discards everything written since the backup. It is never part of a code rollback, and nothing in this runbook performs it.

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
