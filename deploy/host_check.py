#!/usr/bin/python3 -I
"""
Read-only production host readiness check for ApplyForge (Milestone 8.4e).

    sudo python3 -I /srv/applyforge/current/deploy/host_check.py [--release DIR] [--strict]

It inspects the server against deploy/README.md and deploy/PRODUCTION_READINESS.md and
prints one line per check: PASS, WARN, FAIL, INFO or SKIP. It changes nothing:

- files are only stat()ed or read when they hold no secrets (unit files, nginx
  configuration, backup.conf, the PUBLIC age recipients, public certificates); the
  environment file and private keys are only stat()ed, never opened;
- PostgreSQL is queried as the postgres OS user (peer authentication) inside
  read-only transactions (default_transaction_read_only=on);
- external commands are read-only (nginx -t, systemd-analyze verify, ss, nft list,
  findmnt, openssl x509, pip check, ...). Nothing is started, stopped, enabled,
  mounted or written. probe-destination (which writes probe files) is NOT run.

Exit status: 0 no failures (with --strict: no failures and no warnings), 1 otherwise.
Python standard library only; Linux.
"""

import argparse
import datetime as dt
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
GIB = 1024 ** 3
RELEASE_NAME_RE = re.compile(r"^[0-9a-f]{40}$")
NOLOGIN_SHELLS = {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false"}
LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "[::1]", "localhost"}
WILDCARD_ADDRESSES = {"0.0.0.0", "*", "[::]", "::"}
PRIVILEGED_GROUPS = {"sudo", "wheel", "adm", "admin", "root", "docker", "lxd", "disk", "shadow", "systemd-journal"}
MIN_PYTHON = (3, 12)
MIN_POSTGRES = 140000
MIN_NGINX = (1, 19, 4)
MIN_SYSTEMD = 247

# (path, kind, owner, group, mode) from deploy/README.md "Filesystem layout and ownership"
LAYOUT = [
    ("/srv/applyforge", "dir", "root", "root", 0o755),
    ("/srv/applyforge/releases", "dir", "root", "root", 0o755),
    ("/var/lib/applyforge", "dir", "root", "root", 0o755),
    ("/var/lib/applyforge/media", "dir", "{app}", "{app}", 0o750),
    ("/var/lib/applyforge/static", "dir", "{app}", "{app}", 0o755),
    ("/etc/applyforge", "dir", "root", "{app}", 0o750),
    ("/etc/applyforge/applyforge.env", "file", "root", "{app}", 0o640),
    ("/etc/ssl/applyforge/applyforge.key", "file", "root", None, 0o600),
    ("/etc/ssl/applyforge/applyforge.crt", "file", "root", None, 0o644),
    ("/usr/local/sbin/applyforge-manage", "file", "root", "root", 0o755),
]
# installed copy -> path inside the release; must be byte-identical
INSTALLED_COPIES = [
    ("/usr/local/sbin/applyforge-manage", "deploy/bin/applyforge-manage"),
    ("/usr/local/sbin/applyforge-backup", "deploy/backup/afbackup.py"),
    ("/etc/systemd/system/applyforge.service", "deploy/systemd/applyforge.service"),
    ("/etc/systemd/system/applyforge-backup.service", "deploy/systemd/applyforge-backup.service"),
    ("/etc/systemd/system/applyforge-backup.timer", "deploy/systemd/applyforge-backup.timer"),
    ("/etc/tmpfiles.d/applyforge-backup.conf", "deploy/systemd/applyforge-backup.tmpfiles"),
    ("/etc/nginx/snippets/applyforge-security-headers.conf", "deploy/nginx/snippets/applyforge-security-headers.conf"),
    ("/etc/nginx/snippets/applyforge-proxy.conf", "deploy/nginx/snippets/applyforge-proxy.conf"),
]
# Tracked, placeholder-only templates that every release built with git archive contains
# (.env.example at the repository root and in frontend/). Only a real `.env` is loaded on the
# server (settings.py via APPLYFORGE_DOTENV; preflight.py refuses one); found in the 8.4f dress rehearsal.
TRACKED_ENV_TEMPLATES = {".env.example"}
NGINX_SITE = "/etc/nginx/sites-available/applyforge.conf"
NGINX_ENABLED = "/etc/nginx/sites-enabled/applyforge.conf"
NGINX_DEFAULT_SITE = "/etc/nginx/sites-enabled/default"
BACKUP_CONFIG = "/etc/applyforge/backup.conf"


class Host:
    """Everything the checks read from the machine. Tests substitute a fake."""

    def __init__(self, root="/"):
        self.root = root

    def p(self, path):
        return os.path.join(self.root, path.lstrip("/")) if self.root != "/" else path

    def run(self, args, timeout=60, env=None):
        full_env = {"PATH": SAFE_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": "/"}
        full_env.update(env or {})
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout, env=full_env,
                                    stdin=subprocess.DEVNULL, check=False)
        except FileNotFoundError:
            return 127, "", f"{args[0]}: not found"
        except subprocess.TimeoutExpired:
            return 124, "", f"{args[0]}: timed out"
        return (result.returncode, result.stdout.decode("utf-8", "replace"),
                result.stderr.decode("utf-8", "replace"))

    def which(self, name):
        return shutil.which(name, path=SAFE_PATH)

    def lstat(self, path):
        return os.lstat(self.p(path))

    def stat(self, path):
        return os.stat(self.p(path))

    def exists(self, path):
        return os.path.lexists(self.p(path))

    def readlink(self, path):
        return os.readlink(self.p(path))

    def realpath(self, path):
        real = os.path.realpath(self.p(path))
        if self.root != "/":
            real = "/" + os.path.relpath(real, self.root).replace(os.sep, "/")
        return real

    def read_text(self, path, limit=4 * 1024 * 1024):
        with open(self.p(path), "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")

    def sha256(self, path):
        digest = hashlib.sha256()
        with open(self.p(path), "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def walk(self, path):
        for dirpath, dirnames, filenames in os.walk(self.p(path)):
            rel = dirpath if self.root == "/" else "/" + os.path.relpath(dirpath, self.root).replace(os.sep, "/")
            yield rel, dirnames, filenames

    def disk_free(self, path):
        return shutil.disk_usage(self.p(path)).free

    def user(self, name):
        import pwd
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            return None
        return {"uid": entry.pw_uid, "gid": entry.pw_gid, "home": entry.pw_dir, "shell": entry.pw_shell}

    def user_name(self, uid):
        import pwd
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return str(uid)

    def group_name(self, gid):
        import grp
        try:
            return grp.getgrgid(gid).gr_name
        except KeyError:
            return str(gid)

    def groups_of(self, name):
        import grp
        return {g.gr_name for g in grp.getgrall() if name in g.gr_mem}

    def euid(self):
        return os.geteuid()

    def now(self):
        return dt.datetime.now(dt.timezone.utc)


class Report:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "INFO": 0, "SKIP": 0}

    def emit(self, tag, message):
        self.counts[tag] += 1
        print(f"{tag}  {message}", file=self.out, flush=True)

    def section(self, title):
        print(f"\n== {title}", file=self.out, flush=True)

    def ok(self, m):
        self.emit("PASS", m)

    def warn(self, m):
        self.emit("WARN", m)

    def fail(self, m):
        self.emit("FAIL", m)

    def info(self, m):
        self.emit("INFO", m)

    def skip(self, m):
        self.emit("SKIP", m)


def version_tuple(text):
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x) for x in match.groups(default="0")) if match else None


def parse_kv(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


# --- checks ----------------------------------------------------------------------------------------

def check_os(host, r):
    r.section("Operating system and runtime")
    try:
        osr = parse_kv(host.read_text("/etc/os-release"))
    except OSError:
        osr = {}
    name = osr.get("PRETTY_NAME", "unknown").strip('"')
    distro, version = osr.get("ID", "").strip('"'), osr.get("VERSION_ID", "").strip('"')
    if distro == "ubuntu" and version == "26.04":
        r.ok(f"{name} (the validated platform)")
    elif distro in ("ubuntu", "debian"):
        r.warn(f"{name}: supported family, but only Ubuntu 26.04 LTS was validated (re-run the 8.4e checks)")
    else:
        r.warn(f"{name}: not a validated distribution")
    rc, out, _ = host.run(["systemctl", "--version"])
    sd = re.search(r"systemd (\d+)", out) if rc == 0 else None
    if not sd:
        r.fail("systemd not found or not running")
    elif int(sd.group(1)) < MIN_SYSTEMD:
        r.fail(f"systemd {sd.group(1)} is older than {MIN_SYSTEMD}")
    else:
        r.ok(f"systemd {sd.group(1)}")
    rc, out, _ = host.run(["systemd-detect-virt"])
    r.info(f"virtualization: {out.strip() or 'none'}")
    rc, out, _ = host.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if rc == 0 and out.strip() == "yes":
        r.ok("system clock is NTP-synchronised")
    else:
        r.warn("system clock is not NTP-synchronised (TLS, logs and backup timestamps depend on it)")


def check_resources(host, r):
    r.section("CPU, memory and storage")
    try:
        cpus = len(re.findall(r"^processor\s*:", host.read_text("/proc/cpuinfo"), re.M))
    except OSError:
        cpus = 0
    (r.ok if cpus >= 2 else r.warn)(f"{cpus} CPU(s) (2 or more recommended)")
    try:
        mem = int(re.search(r"^MemTotal:\s+(\d+) kB", host.read_text("/proc/meminfo"), re.M).group(1)) * 1024
    except (OSError, AttributeError):
        mem = 0
    if mem >= 2 * GIB:
        r.ok(f"{mem / GIB:.1f} GiB RAM")
    elif mem >= 1 * GIB:
        r.warn(f"{mem / GIB:.1f} GiB RAM (2 GiB or more recommended: PostgreSQL + Gunicorn + backup tar)")
    else:
        r.fail(f"{mem / GIB:.1f} GiB RAM is below the 1 GiB minimum")
    for path, need in (("/", 5), ("/srv", 5), ("/var/lib/postgresql", 5), ("/var/lib/applyforge", 2)):
        probe = path
        while probe != "/" and not host.exists(probe):
            probe = os.path.dirname(probe)
        try:
            free = host.disk_free(probe)
        except OSError:
            r.skip(f"free space on {path}: cannot be determined")
            continue
        (r.ok if free >= need * GIB else r.warn)(
            f"{free / GIB:.1f} GiB free for {path} ({need} GiB or more recommended)")
    r.info("backup work space: BACKUP_WORK_DIR needs about 2x (database dump + media) free during a backup")


def check_tools(host, r, release):
    r.section("Executables and versions")
    rc, out, _ = host.run(["python3", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"])
    ver = version_tuple(out) if rc == 0 else None
    if not ver:
        r.fail("python3 not found")
    elif ver[:2] < MIN_PYTHON:
        r.fail(f"python3 {out.strip()} is older than {MIN_PYTHON[0]}.{MIN_PYTHON[1]}")
    else:
        r.ok(f"python3 {out.strip()}")
    rc, _, err = host.run(["nginx", "-v"])
    nv = version_tuple(err) if rc == 0 else None
    if not nv:
        r.fail("nginx not found")
    elif nv < MIN_NGINX:
        r.fail(f"nginx {'.'.join(map(str, nv))} is older than 1.19.4 (ssl_reject_handshake)")
    else:
        r.ok(f"nginx {'.'.join(map(str, nv))}")
    for tool in ("psql", "pg_dump", "pg_restore", "createdb", "age", "runuser", "systemd-analyze", "openssl", "ss"):
        if host.which(tool):
            r.ok(f"{tool} found")
        else:
            (r.warn if tool in ("openssl", "ss") else r.fail)(f"{tool} not found on {SAFE_PATH}")
    for tool in ("chromium", "chromium-browser", "google-chrome", "ollama"):
        if host.which(tool):
            r.warn(f"{tool} is installed; the collector and generation are disabled and need no browser or model")
    venv_python = f"{release}/.venv/bin/python"
    if not host.exists(venv_python):
        r.skip(f"release virtualenv {venv_python} not present yet")
        return
    rc, out, err = host.run([venv_python, "-m", "pip", "check"], timeout=120)
    (r.ok if rc == 0 else r.fail)("pip check: " + (out.strip().splitlines() or ["ok"])[-1][:200])
    constraints = f"{release}/backend/constraints.txt"
    rc, out, _ = host.run([venv_python, "-m", "pip", "freeze", "--all"], timeout=120)
    if rc != 0 or not host.exists(constraints):
        r.skip("installed packages could not be compared with backend/constraints.txt")
        return
    installed = {k.lower(): v for k, _, v in (line.partition("==") for line in out.splitlines()) if v}
    names = {k.lower(): k for k, _, v in (line.partition("==") for line in host.read_text(constraints).splitlines()
                                           if line.strip() and not line.startswith("#")) if v}
    wanted = {k.lower(): v for k, _, v in (line.partition("==") for line in host.read_text(constraints).splitlines()
                                            if line.strip() and not line.startswith("#")) if v}
    drift = sorted(f"{names[k]} {installed.get(k, 'missing')} (pinned {v})" for k, v in wanted.items()
                   if installed.get(k) != v.strip())
    if drift:
        r.fail("virtualenv differs from backend/constraints.txt: " + "; ".join(drift[:8]))
    else:
        r.ok(f"virtualenv matches backend/constraints.txt ({len(wanted)} pinned packages)")
    if installed.get("playwright"):
        r.info("the playwright Python package is installed (imported by the collector code); no browsers needed")


def check_accounts(host, r, app_user, restore_user):
    r.section("Service accounts")
    seen = {}
    for name, role in ((app_user, "application service"), (restore_user, "isolated restore")):
        entry = host.user(name)
        if entry is None:
            r.fail(f"OS account {name} ({role}) does not exist (deploy/README.md 3 and 12.6)")
            continue
        seen[name] = entry
        problems = []
        if entry["uid"] == 0 or entry["uid"] >= 1000:
            problems.append(f"uid {entry['uid']} is not a system uid")
        if entry["shell"] not in NOLOGIN_SHELLS:
            problems.append(f"login shell {entry['shell']}")
        if entry["home"] not in ("/nonexistent", "/") and host.exists(entry["home"]):
            problems.append(f"home directory {entry['home']} exists")
        groups = host.groups_of(name) & PRIVILEGED_GROUPS
        if groups:
            problems.append("member of privileged group(s) " + ", ".join(sorted(groups)))
        if problems:
            r.fail(f"{name} ({role}): " + "; ".join(problems))
        else:
            r.ok(f"{name} ({role}): system account, no login shell, no home, no privileged groups")
    if len(seen) == 2 and seen[app_user]["uid"] == seen[restore_user]["uid"]:
        r.fail(f"{app_user} and {restore_user} share a uid")


def check_layout(host, r, app_user):
    r.section("Filesystem layout, ownership and permissions (secrets are only stat()ed)")
    for path, kind, owner, group, mode in LAYOUT:
        owner, group = (x.format(app=app_user) if x else x for x in (owner, group))
        try:
            st = host.lstat(path)
        except FileNotFoundError:
            (r.warn if path.startswith("/etc/ssl") else r.fail)(f"{path} is missing")
            continue
        problems = []
        if stat.S_ISLNK(st.st_mode):
            problems.append("is a symbolic link")
        elif kind == "dir" and not stat.S_ISDIR(st.st_mode):
            problems.append("is not a directory")
        elif kind == "file" and not stat.S_ISREG(st.st_mode):
            problems.append("is not a regular file")
        if host.user_name(st.st_uid) != owner:
            problems.append(f"owner {host.user_name(st.st_uid)} (expected {owner})")
        if group and host.group_name(st.st_gid) != group:
            problems.append(f"group {host.group_name(st.st_gid)} (expected {group})")
        actual = stat.S_IMODE(st.st_mode)
        if actual & ~mode:
            problems.append(f"mode {actual:04o} is more permissive than {mode:04o}")
        if problems:
            r.fail(f"{path}: " + "; ".join(problems))
        else:
            r.ok(f"{path} {owner}:{group or '*'} {actual:04o}")


def check_release(host, r, release):
    r.section("Active release")
    if not host.exists(release):
        r.fail(f"{release} does not exist (no release installed or switched)")
        return None
    try:
        is_link = stat.S_ISLNK(host.lstat(release).st_mode)
    except OSError:
        is_link = False
    real = host.realpath(release)
    name = os.path.basename(real)
    if release == "/srv/applyforge/current" and not is_link:
        r.fail(f"{release} is not a symbolic link")
    if os.path.dirname(real) == "/srv/applyforge/releases" and RELEASE_NAME_RE.match(name):
        r.ok(f"{release} -> {real}")
    else:
        r.warn(f"{release} resolves to {real}, not /srv/applyforge/releases/<full commit hash>")
    writable, not_root, dotenv, env_variants = [], [], [], []
    for dirpath, dirnames, filenames in host.walk(real):
        for entry in dirnames + filenames:
            path = f"{dirpath}/{entry}"
            try:
                st = host.lstat(path)
            except OSError:
                continue
            if entry == ".env":
                dotenv.append(path)
            elif entry.startswith(".env.") and entry not in TRACKED_ENV_TEMPLATES:
                env_variants.append(path)
            if st.st_uid != 0:
                not_root.append(path)
            if not stat.S_ISLNK(st.st_mode) and stat.S_IMODE(st.st_mode) & 0o022:
                writable.append(path)
    for found, message in ((dotenv, "a .env file inside the release (preflight refuses to start)"),
                           (not_root, "release files not owned by root"),
                           (writable, "group/world-writable release files")):
        if found:
            r.fail(f"{len(found)} {message}, e.g. {found[0]}")
    if env_variants:
        r.warn(f"{len(env_variants)} .env.* file(s) in the release, e.g. {env_variants[0]}: not loaded on the "
               "server, but they may hold secrets - releases are built with git archive and should not contain them")
    if not (dotenv or not_root or writable):
        r.ok("release tree is root-owned, not group/world-writable, and has no .env")
    for required in ("frontend/dist/index.html", "deploy/preflight.py", ".venv/bin/gunicorn"):
        if not host.exists(f"{real}/{required}"):
            r.fail(f"release is missing {required}")
    return real


def check_installed_copies(host, r, release_real):
    r.section("Installed files match the active release")
    if not release_real:
        r.skip("no release to compare with")
        return
    for installed, source in INSTALLED_COPIES:
        src = f"{release_real}/{source}"
        if not host.exists(installed):
            (r.fail if "applyforge-backup" not in installed else r.warn)(f"{installed} is not installed")
            continue
        try:
            same = host.sha256(installed) == host.sha256(src)
        except OSError as exc:
            r.skip(f"{installed}: cannot compare ({exc.strerror})")
            continue
        (r.ok if same else r.warn)(f"{installed} {'matches' if same else 'DIFFERS from'} {source}")


def check_nginx(host, r):
    r.section("nginx and TLS")
    if not host.exists(NGINX_SITE):
        r.fail(f"{NGINX_SITE} is not installed")
        return
    if host.exists(NGINX_DEFAULT_SITE):
        r.fail(f"{NGINX_DEFAULT_SITE} is enabled; it listens on all addresses - remove it")
    else:
        r.ok("the distribution's default site is not enabled")
    if host.exists(NGINX_ENABLED) and host.realpath(NGINX_ENABLED) == NGINX_SITE:
        r.ok("applyforge site is enabled")
    else:
        r.fail(f"{NGINX_ENABLED} does not link to {NGINX_SITE}")
    conf = host.read_text(NGINX_SITE)
    listens = re.findall(r"^\s*listen\s+([^;\s]+)", conf, re.M)
    wildcard = [x for x in listens if listen_address(x) in WILDCARD_ADDRESSES | {""}]
    if wildcard:
        r.fail("nginx site listens on all addresses: " + ", ".join(wildcard))
    else:
        r.ok("nginx site listens only on specific addresses: " + ", ".join(sorted(set(listens))))
    check_nginx_vpn_ordering(host, r, listens)
    names = sorted(set(re.findall(r"^\s*server_name\s+([^;]+);", conf, re.M)) - {"_"})
    server_names = sorted({n for line in names for n in line.split() if n != "_"})
    if any(n.endswith(".example") for n in server_names) or "10.99.0." in conf:
        r.warn("nginx site still contains the example host name or 10.99.0.x addresses: replace the placeholders "
               "(unless they really are your VPN values)")
    rc, out, err = host.run(["nginx", "-t"])
    (r.ok if rc == 0 else r.fail)("nginx -t: " + ((err or out).strip().splitlines() or ["no output"])[-1][:200])
    cert = re.search(r"^\s*ssl_certificate\s+([^;]+);", conf, re.M)
    cert_path = cert.group(1).strip() if cert else "/etc/ssl/applyforge/applyforge.crt"
    if not host.exists(cert_path):
        r.fail(f"certificate {cert_path} is missing")
        return
    rc, _, _ = host.run(["openssl", "x509", "-checkend", str(30 * 86400), "-noout", "-in", host.p(cert_path)])
    rc_now, _, _ = host.run(["openssl", "x509", "-checkend", "0", "-noout", "-in", host.p(cert_path)])
    if rc_now != 0:
        r.fail(f"certificate {cert_path} has expired (or cannot be read)")
    elif rc != 0:
        r.warn(f"certificate {cert_path} expires within 30 days")
    else:
        r.ok(f"certificate {cert_path} is valid for more than 30 days")
    for server_name in server_names:
        rc, out, _ = host.run(["openssl", "x509", "-noout", "-checkhost", server_name, "-in", host.p(cert_path)])
        if rc == 0 and "does match" in out and "does NOT match" not in out:
            r.ok(f"certificate covers {server_name}")
        else:
            r.fail(f"certificate does not cover {server_name}")


def check_nginx_vpn_ordering(host, r, listens):
    """nginx listens on the VPN address; if a WireGuard (wg-quick) unit is enabled, nginx must be ordered
    after it, or nginx can fail to bind at boot and stay down (found in the 2026-11-21 network rehearsal)."""
    specific = [x for x in listens if listen_address(x) not in WILDCARD_ADDRESSES | LOOPBACK_ADDRESSES | {"", "unix"}]
    if not specific:
        return
    # Enabled template INSTANCES (wg-quick@wg0) are not listed by list-unit-files, only the template is;
    # list the loaded instances and keep the enabled ones.
    rc, out, _ = host.run(["systemctl", "list-units", "--all", "--type=service", "--no-legend", "--plain", "wg-quick@*"])
    loaded = sorted({line.split()[0] for line in out.splitlines() if line.strip()}) if rc == 0 else []
    vpn_units = [u for u in loaded if host.run(["systemctl", "is-enabled", u])[1].strip() == "enabled"]
    if not vpn_units:
        r.info("no enabled wg-quick unit found; check that nginx starts after whatever brings up the VPN address")
        return
    rc, out, _ = host.run(["systemctl", "show", "-p", "After", "--value", "nginx.service"])
    after = set(out.split()) if rc == 0 else set()
    missing = [u for u in vpn_units if u not in after]
    if missing:
        r.fail(f"nginx is not ordered after {', '.join(missing)}: at boot it can fail to bind the VPN address and "
               "stay down - install deploy/systemd/nginx-applyforge-vpn.conf (deploy/README.md section 6)")
    else:
        r.ok(f"nginx starts after {', '.join(vpn_units)} (it binds the VPN address)")


def listen_address(spec):
    """Address part of an nginx `listen` value: "10.0.0.1:443" -> "10.0.0.1", "443" -> ""."""
    if spec.startswith("unix:"):
        return "unix"
    return spec.rpartition(":")[0] if ":" in spec else ""


def parse_listeners(text):
    """[(address, port, process)] from `ss -Htlnp`."""
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        address, _, port = local.rpartition(":")
        address = address.split("%")[0]
        process = re.search(r'users:\(\("([^"]+)"', line)
        found.append((address, port, process.group(1) if process else "?"))
    return found


def check_network(host, r, vpn_addresses):
    r.section("Listening sockets and firewall")
    rc, out, _ = host.run(["ss", "-Htlnp"])
    if rc != 0:
        r.skip("ss is not available; listening sockets not checked")
    else:
        listeners = parse_listeners(out)
        for address, port, process in listeners:
            loop = address in LOOPBACK_ADDRESSES or address.startswith("127.")
            label = f"{address}:{port} ({process})"
            if port in ("8000", "5432") and not loop:
                r.fail(f"{label}: Gunicorn/PostgreSQL must listen on loopback only")
            elif port in ("80", "443") and address in WILDCARD_ADDRESSES:
                r.fail(f"{label}: nginx must listen on the VPN address only, not all addresses")
            elif port in ("80", "443") and vpn_addresses and address not in vpn_addresses and not loop:
                r.warn(f"{label}: not one of the VPN addresses in the nginx site")
            elif not loop and port not in ("80", "443"):
                r.info(f"{label}: non-loopback listener - confirm the firewall restricts it (e.g. SSH via VPN)")
        if any(p == "8000" for _, p, _ in listeners):
            r.ok("Gunicorn listener present on loopback")
        else:
            r.info("nothing listens on port 8000 (ApplyForge not running)")
    rc, out, _ = host.run(["nft", "list", "ruleset"])
    if rc == 0 and re.search(r"hook input\b[^;]*;\s*policy drop", out):
        r.ok("nftables has an input chain with policy drop")
    else:
        rc2, out2, _ = host.run(["ufw", "status"])
        if rc2 == 0 and "Status: active" in out2:
            r.ok("ufw is active (review: deny incoming by default, 80/443 only on the VPN interface)")
        else:
            r.warn("no default-deny inbound firewall found (nftables policy drop or active ufw); the "
                   "firewall is the primary control for VPN-only access")


PSQL_READ_ONLY = ["-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1", "-c", "SET default_transaction_read_only = on"]


def psql(host, sql, db="postgres"):
    rc, out, err = host.run(["runuser", "-u", "postgres", "--", "psql", *PSQL_READ_ONLY, "-d", db, "-c", sql])
    if rc != 0:
        raise RuntimeError((err.strip().splitlines() or ["psql failed"])[-1][:200])
    return [line.split("|") for line in out.splitlines() if line.strip()]


def check_postgres(host, r, db_name, db_owner, restore_role):
    r.section("PostgreSQL (read-only queries as postgres via peer authentication)")
    if host.euid() != 0:
        r.skip("not root: PostgreSQL checks need runuser -u postgres")
        return
    try:
        rows = psql(host, "SELECT current_setting('server_version_num'), current_setting('server_version')")
    except RuntimeError as exc:
        r.fail(f"cannot query PostgreSQL as postgres: {exc}")
        return
    num, text = int(rows[0][0]), rows[0][1]
    (r.ok if num >= MIN_POSTGRES else r.fail)(f"PostgreSQL {text} (14 or later required)")
    rc, out, _ = host.run(["pg_dump", "--version"])
    dump_major = version_tuple(out)[0] if rc == 0 and version_tuple(out) else 0
    (r.ok if dump_major >= num // 10000 else r.fail)(
        f"pg_dump major {dump_major} vs server {num // 10000} (pg_dump must not be older)")

    settings = dict(psql(host, "SELECT name, setting FROM pg_settings WHERE name IN ('listen_addresses', "
                               "'password_encryption', 'max_connections', 'ssl', 'shared_buffers', "
                               "'log_connections', 'data_checksums')"))
    listen = {a.strip() for a in settings.get("listen_addresses", "").split(",") if a.strip()}
    if listen and listen <= LOOPBACK_ADDRESSES:
        r.ok(f"listen_addresses = {settings['listen_addresses']}")
    elif not listen:
        r.ok("listen_addresses is empty (Unix socket only)")
    else:
        r.fail(f"listen_addresses = {settings.get('listen_addresses')}: keep PostgreSQL on localhost")
    (r.ok if settings.get("password_encryption") == "scram-sha-256" else r.warn)(
        f"password_encryption = {settings.get('password_encryption')}")
    r.info(f"max_connections = {settings.get('max_connections')} (ApplyForge needs about 10); "
           f"shared_buffers = {settings.get('shared_buffers')} x 8 kB; data_checksums = "
           f"{settings.get('data_checksums')}")

    try:
        hba = psql(host, "SELECT type, array_to_string(database, ','), array_to_string(user_name, ','), "
                         "coalesce(address, ''), coalesce(netmask, ''), auth_method, coalesce(error, '') "
                         "FROM pg_hba_file_rules")
    except RuntimeError as exc:
        r.fail(f"cannot read pg_hba_file_rules: {exc}")
        hba = []
    errors = [row for row in hba if row[6]]
    trust = [row for row in hba if row[5] == "trust"]
    wide = [row for row in hba if row[0].startswith("host") and (row[3] in ("all", "0.0.0.0", "::", "samenet",
                                                                           "samehost") or row[4] in ("0.0.0.0", "::"))]
    md5 = [row for row in hba if row[5] in ("md5", "password")]
    peer_local = [row for row in hba if row[0] == "local" and row[5] == "peer"]
    for found, level, message in ((errors, r.fail, "pg_hba.conf has lines with errors"),
                                  (trust, r.fail, "pg_hba.conf has 'trust' rules"),
                                  (wide, r.fail, "pg_hba.conf allows host connections from any address"),
                                  (md5, r.warn, "pg_hba.conf uses md5/password (use scram-sha-256)")):
        if found:
            level(f"{message}: {len(found)} rule(s)")
    if not (errors or trust or wide):
        r.ok(f"pg_hba.conf: {len(hba)} rule(s), no trust, no wide host rules")
    (r.ok if peer_local else r.fail)("local peer authentication " + ("is configured" if peer_local else
                                     "is missing: the backup and restore tools rely on it"))

    roles = {row[0]: row[1:] for row in psql(
        host, "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolcanlogin "
              f"FROM pg_roles WHERE rolname IN ('{db_owner}', '{restore_role}')")}
    for role, purpose in ((db_owner, "application/database owner"), (restore_role, "isolated restore")):
        attrs = roles.get(role)
        if attrs is None:
            r.fail(f"role {role} ({purpose}) does not exist")
            continue
        bad = [n for n, v in zip(("SUPERUSER", "CREATEROLE", "CREATEDB", "REPLICATION", "BYPASSRLS"), attrs) if v == "t"]
        if bad or attrs[5] != "t":
            r.fail(f"role {role}: " + ", ".join(bad + ([] if attrs[5] == "t" else ["cannot log in"])))
        else:
            r.ok(f"role {role} ({purpose}): login, no dangerous attributes")
    if restore_role in roles:
        members = psql(host, "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                             f"WHERE r.rolname = '{restore_role}'")
        (r.ok if members[0][0] == "0" else r.fail)(
            f"restore role {restore_role} is a member of {members[0][0]} role(s) (must be 0)")

    dbs = psql(host, "SELECT d.datname, pg_get_userbyid(d.datdba), pg_encoding_to_char(d.encoding), d.datconnlimit, "
                     "coalesce((SELECT string_agg(a.privilege_type, ',') FROM aclexplode(d.datacl) a "
                     "WHERE a.grantee = 0), CASE WHEN d.datacl IS NULL THEN 'CONNECT,TEMPORARY' ELSE '' END) "
                     f"FROM pg_database d WHERE d.datname = '{db_name}'")
    if not dbs:
        r.fail(f"database {db_name} does not exist (deploy/README.md 3.1)")
        return
    _, owner, encoding, connlimit, public = dbs[0]
    (r.ok if owner == db_owner else r.fail)(f"database {db_name} owner {owner} (expected {db_owner})")
    (r.ok if encoding == "UTF8" else r.warn)(f"database encoding {encoding}")
    if public:
        r.warn(f"PUBLIC has {public} on database {db_name}: REVOKE CONNECT, TEMPORARY ON DATABASE {db_name} "
               "FROM PUBLIC (deploy/README.md 12.6) - after confirming no other role relies on it")
    else:
        r.ok(f"PUBLIC has no privileges on database {db_name}")
    r.info(f"database connection limit: {connlimit} (-1 = unlimited)")
    try:
        create = psql(host, "SELECT has_schema_privilege('public', 'public', 'CREATE')", db=db_name)
        (r.ok if create[0][0] == "f" else r.warn)(
            "PUBLIC " + ("cannot" if create[0][0] == "f" else "CAN") + " create objects in schema public")
        owned = psql(host, "SELECT count(*) FROM pg_class c JOIN pg_roles o ON o.oid = c.relowner "
                           f"WHERE o.rolname = '{restore_role}'", db=db_name)
        (r.ok if owned[0][0] == "0" else r.fail)(
            f"restore role owns {owned[0][0]} relation(s) in {db_name} (must be 0)")
    except RuntimeError as exc:
        r.skip(f"checks inside {db_name}: {exc}")


def check_units(host, r):
    r.section("systemd units (verified, not started)")
    for unit in ("applyforge.service", "applyforge-backup.service", "applyforge-backup.timer"):
        path = f"/etc/systemd/system/{unit}"
        if not host.exists(path):
            (r.fail if unit == "applyforge.service" else r.warn)(f"{unit} is not installed")
            continue
        rc, out, err = host.run(["systemd-analyze", "verify", path], timeout=60)
        # verify also prints notes about unrelated distribution units it loads (e.g. Ubuntu 26.04:
        # "system-xfs_scrub.slice:15: Support for option CPUAccounting= has been removed"); keep only
        # lines about this unit.
        text = "\n".join(line for line in (err + out).splitlines()
                         if unit in line or path in line or unit.split(".")[0] + "." in line).strip()
        (r.ok if rc == 0 and not text else r.warn if rc == 0 else r.fail)(
            f"systemd-analyze verify {unit}: " + (text.splitlines()[-1][:200] if text else "clean"))
        rc, enabled, _ = host.run(["systemctl", "is-enabled", unit])
        rc, active, _ = host.run(["systemctl", "is-active", unit])
        r.info(f"{unit}: {enabled.strip() or 'unknown'}, {active.strip() or 'unknown'}")
        # Found in the 8.4f dress rehearsal: a unit stuck in "failed" (e.g. its start limit was hit
        # after preflight refusals) went unreported. Inactive is fine before go-live; failed is not.
        if active.strip() == "failed":
            if unit == "applyforge.service":
                r.fail("applyforge.service is in the failed state: read journalctl -u applyforge, fix the cause, "
                       "then systemctl reset-failed applyforge && systemctl start applyforge")
            else:
                r.warn(f"{unit} is in the failed state (the last run failed): journalctl -u {unit.split('.')[0]}")
    rc, out, _ = host.run(["systemd-analyze", "security", "--no-pager", "applyforge.service"])
    exposure = re.search(r"Overall exposure level for \S+: ([\d.]+) (\w+)", out)
    if exposure:
        (r.ok if float(exposure.group(1)) <= 2.0 else r.warn)(
            f"applyforge.service exposure {exposure.group(1)} {exposure.group(2)} (validated: 1.2)")


def check_backup(host, r):
    r.section("Backups (configuration and state; probe-destination is NOT run here)")
    if not host.exists(BACKUP_CONFIG):
        r.warn(f"{BACKUP_CONFIG} is not installed: no backups are configured (deploy/README.md 12)")
        return
    try:
        cfg = parse_kv(host.read_text(BACKUP_CONFIG))
        st = host.lstat(BACKUP_CONFIG)
    except OSError as exc:
        r.fail(f"cannot read {BACKUP_CONFIG} ({exc.strerror})")
        return
    if st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o022:
        r.fail(f"{BACKUP_CONFIG} must be root-owned and not group/world-writable")
    dest = cfg.get("BACKUP_DESTINATION", "/mnt/applyforge-backups")
    rc, out, _ = host.run(["findmnt", "--fstab", "-n", "-o", "FSTYPE,OPTIONS", "--target", dest])
    unit = host_mount_unit(host, dest)
    rc_unit = host.run(["systemctl", "cat", unit])[0] if unit else 1
    if rc == 0 or rc_unit == 0:
        r.ok(f"{dest} has an fstab entry or mount unit" + (f": {out.strip()}" if out.strip() else ""))
    else:
        r.fail(f"{dest} has no fstab entry or mount unit")
    rc, out, _ = host.run(["findmnt", "-n", "-o", "FSTYPE,SOURCE,OPTIONS", "--mountpoint", dest])
    if rc != 0:
        r.warn(f"{dest} is not mounted now (create refuses to run until it is)")
    else:
        fs_type = out.split()[0]
        r.ok(f"{dest} is mounted: {out.strip()[:200]}")
        allowed = [t.strip() for t in cfg.get("BACKUP_ALLOWED_FS_TYPES", "").split(",") if t.strip()]
        if not allowed:
            r.warn(f"BACKUP_ALLOWED_FS_TYPES is empty: set it to {fs_type}")
        elif fs_type not in allowed:
            r.fail(f"mounted type {fs_type} is not in BACKUP_ALLOWED_FS_TYPES")
        (r.ok if host.exists(f"{dest}/.applyforge-backup-destination") else r.fail)(
            "destination marker file " + ("present" if host.exists(f"{dest}/.applyforge-backup-destination")
                                          else "missing"))
    recipients = cfg.get("BACKUP_RECIPIENTS_FILE", "/etc/applyforge/backup-recipients.txt")
    try:
        text = host.read_text(recipients)
        keys = [line.strip() for line in text.splitlines() if line.strip().startswith("age1")]
        if "AGE-SECRET-KEY-" in text.upper():
            r.fail(f"{recipients} contains an age PRIVATE key - remove it from the server now")
        elif keys:
            r.ok(f"{recipients}: {len(keys)} public recipient(s), no private key")
        else:
            r.fail(f"{recipients} lists no age recipients")
    except OSError:
        r.fail(f"recipients file {recipients} is missing")
    index = os.path.join(cfg.get("BACKUP_STATE_DIR", "/var/lib/applyforge-backup"), "index.tsv")
    try:
        records = [line.split("\t") for line in host.read_text(index).splitlines() if line.count("\t") == 6]
    except OSError:
        records = []
    recorded = [rec for rec in records if rec[6] == "recorded"]
    if not recorded:
        r.warn("no backup has been recorded yet (first backup requires authorization)")
    else:
        try:
            last = dt.datetime.strptime(recorded[-1][0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
            age_h = (host.now() - last).total_seconds() / 3600
            (r.ok if age_h <= 36 else r.warn)(f"last recorded backup {recorded[-1][1]} is {age_h:.0f} h old")
        except ValueError:
            r.warn("the last index record has an unexpected timestamp")
    r.info("before the first backup on new storage run: sudo applyforge-backup probe-destination "
           "(writes and removes its own probe files only)")


def host_mount_unit(host, path):
    rc, out, _ = host.run(["systemd-escape", "--path", "--suffix=mount", path])
    return out.strip() if rc == 0 and out.strip() else None


def vpn_addresses_from_nginx(host):
    try:
        conf = host.read_text(NGINX_SITE)
    except OSError:
        return set()
    return {listen_address(x) for x in re.findall(r"^\s*listen\s+([^;\s]+)", conf, re.M)} - {"", "unix"}


def main(argv=None, host=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description="Read-only ApplyForge production host readiness check.")
    parser.add_argument("--release", default="/srv/applyforge/current")
    parser.add_argument("--app-user", default="applyforge")
    parser.add_argument("--restore-user", default="applyforge_restore")
    parser.add_argument("--db-name", default="applyforge")
    parser.add_argument("--db-owner", default="applyforge")
    parser.add_argument("--restore-role", default="applyforge_restore")
    parser.add_argument("--skip-postgres", action="store_true")
    parser.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    args = parser.parse_args(argv)
    for value in (args.db_name, args.db_owner, args.restore_role):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
            parser.error(f"invalid PostgreSQL identifier: {value}")
    host = host or Host()
    report = Report(out)
    print("ApplyForge host readiness check (read-only)", file=out)
    if host.euid() != 0:
        report.warn("not running as root: several checks are skipped or incomplete (run with sudo)")
    check_os(host, report)
    check_resources(host, report)
    check_tools(host, report, args.release)
    check_accounts(host, report, args.app_user, args.restore_user)
    check_layout(host, report, args.app_user)
    release_real = check_release(host, report, args.release)
    check_installed_copies(host, report, release_real)
    check_units(host, report)
    check_nginx(host, report)
    check_network(host, report, vpn_addresses_from_nginx(host))
    if args.skip_postgres:
        report.section("PostgreSQL")
        report.skip("skipped (--skip-postgres)")
    else:
        check_postgres(host, report, args.db_name, args.db_owner, args.restore_role)
    check_backup(host, report)
    c = report.counts
    print(f"\nRESULT: {c['FAIL']} fail, {c['WARN']} warn, {c['PASS']} pass, {c['INFO']} info, {c['SKIP']} skip",
          file=out)
    return 1 if c["FAIL"] or (args.strict and c["WARN"]) else 0


if __name__ == "__main__":
    sys.exit(main())
