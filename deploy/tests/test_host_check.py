"""Tests for deploy/host_check.py (Milestone 8.4e).

A fake host: a temporary directory stands in for "/", file ownership is mapped per path,
and every external command is answered from a table. The tests also prove the check is
read-only: it never opens the environment file or private keys, and never runs a command
that could change the system.
"""

import io
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
import datetime as dt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
import host_check  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
APP_UID, RESTORE_UID = 990, 991
NOW = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)
CONSTRAINTS = "# pins\nDjango==5.2.19\ngunicorn==27.0.1\npsycopg2-binary==2.9.13\n"
SECRET_ENV_TEXT = "SECRET_KEY=fictional-must-never-be-read\n"
SECRET_KEY_TEXT = "-----BEGIN PRIVATE KEY-----\nfictional-must-never-be-read\n"
MUTATING = {"start", "stop", "restart", "reload", "enable", "disable", "mask", "kill", "mount", "umount",
            "create", "restore", "probe-destination", "recover-service", "daemon-reload", "reset-failed"}

NGINX_SITE = """server {
    listen 10.99.1.1:443 ssl;
    server_name applyforge.vpn.internal;
    ssl_certificate /etc/ssl/applyforge/applyforge.crt;
    ssl_certificate_key /etc/ssl/applyforge/applyforge.key;
}
server {
    listen 10.99.1.1:80;
    server_name applyforge.vpn.internal;
}
"""


class FakeHost(host_check.Host):
    def __init__(self, root):
        super().__init__(root)
        self.uids, self.gids = {}, {}
        self.commands, self.read_paths, self.hashed_paths = [], [], []
        self.overrides = {}
        self.is_root = True

    # identity and accounts
    def euid(self):
        return 0 if self.is_root else 1000

    def now(self):
        return NOW

    def user(self, name):
        return {"applyforge": {"uid": APP_UID, "gid": APP_UID, "home": "/nonexistent", "shell": "/usr/sbin/nologin"},
                "applyforge_restore": {"uid": RESTORE_UID, "gid": RESTORE_UID, "home": "/nonexistent",
                                       "shell": "/usr/sbin/nologin"}}.get(name)

    def user_name(self, uid):
        return {0: "root", APP_UID: "applyforge", RESTORE_UID: "applyforge_restore"}.get(uid, str(uid))

    group_name = user_name

    def groups_of(self, name):
        return set()

    def disk_free(self, path):
        return 100 * host_check.GIB

    def which(self, name):
        return None if name in ("chromium", "chromium-browser", "google-chrome", "ollama", "ufw") else f"/usr/bin/{name}"

    def lstat(self, path):
        real = os.lstat(self.p(path))
        return types.SimpleNamespace(st_mode=real.st_mode, st_uid=self.uids.get(path, 0),
                                     st_gid=self.gids.get(path, 0))

    def read_text(self, path, limit=4 * 1024 * 1024):
        self.read_paths.append(path)
        return super().read_text(path, limit)

    def sha256(self, path):
        self.hashed_paths.append(path)
        return super().sha256(path)

    def run(self, args, timeout=60, env=None):
        self.commands.append(list(args))
        for prefix, result in self.overrides.items():
            if " ".join(args).startswith(prefix):
                return result
        return answer(args)


def answer(args):
    cmd = " ".join(args)
    venv = f"/srv/applyforge/current/.venv/bin/python"
    table = [
        ("systemctl --version", (0, "systemd 259 (259.1-1ubuntu1)\n+PAM +AUDIT\n", "")),
        ("systemd-detect-virt", (0, "kvm\n", "")),
        ("timedatectl show", (0, "yes\n", "")),
        ("python3 -c", (0, "3.14.4\n", "")),
        ("nginx -v", (0, "", "nginx version: nginx/1.28.3\n")),
        ("nginx -t", (0, "", "nginx: configuration file /etc/nginx/nginx.conf test is successful\n")),
        (f"{venv} -m pip check", (0, "No broken requirements found.\n", "")),
        (f"{venv} -m pip freeze", (0, "Django==5.2.19\ngunicorn==27.0.1\npsycopg2-binary==2.9.13\npip==25.3\n", "")),
        ("systemd-analyze verify", (0, "", "")),
        ("systemd-analyze security", (0, "→ Overall exposure level for applyforge.service: 1.2 OK 🙂\n", "")),
        ("systemctl is-enabled", (1, "disabled\n", "")),
        ("systemctl is-active", (3, "inactive\n", "")),
        ("systemctl cat", (1, "", "No files found")),
        ("systemd-escape", (0, "mnt-applyforge\\x2dbackups.mount\n", "")),
        ("openssl x509 -checkend", (0, "Certificate will not expire\n", "")),
        ("openssl x509 -noout -checkhost", (0, "Hostname applyforge.vpn.internal does match certificate\n", "")),
        ("ss -Htlnp", (0, "\n".join([
            'LISTEN 0 2048 127.0.0.1:8000 0.0.0.0:* users:(("gunicorn",pid=10,fd=5))',
            'LISTEN 0 200 127.0.0.1:5432 0.0.0.0:* users:(("postgres",pid=11,fd=6))',
            'LISTEN 0 511 10.99.1.1:443 0.0.0.0:* users:(("nginx",pid=12,fd=7))',
            'LISTEN 0 511 10.99.1.1:80 0.0.0.0:* users:(("nginx",pid=12,fd=8))',
            'LISTEN 0 128 10.99.1.1:22 0.0.0.0:* users:(("sshd",pid=13,fd=3))']) + "\n", "")),
        ("nft list ruleset", (0, "table inet filter {\n\tchain input {\n\t\ttype filter hook input priority filter; "
                                 "policy drop;\n\t}\n}\n", "")),
        ("pg_dump --version", (0, "pg_dump (PostgreSQL) 18.6\n", "")),
        ("findmnt --fstab", (0, "nfs4 rw,hard,vers=4.2\n", "")),
        ("findmnt -n -o FSTYPE,SOURCE,OPTIONS --mountpoint", (0, "nfs4 nas:/export/af rw,vers=4.2,hard\n", "")),
    ]
    for prefix, result in table:
        if cmd.startswith(prefix):
            return result
    if args[:4] == ["runuser", "-u", "postgres", "--"] and args[4] == "psql":
        return (0, psql_answer(args[-1]), "")
    return (127, "", f"fake host: unexpected command {cmd}")


def psql_answer(sql):
    if "server_version_num" in sql:
        return "180006|18.6\n"
    if "pg_settings" in sql:
        return ("listen_addresses|localhost\npassword_encryption|scram-sha-256\nmax_connections|100\n"
                "ssl|off\nshared_buffers|16384\nlog_connections|off\ndata_checksums|on\n")
    if "pg_hba_file_rules" in sql:
        return ("local|all|postgres|||peer|\nlocal|all|all|||peer|\n"
                "host|all|all|127.0.0.1|255.255.255.255|scram-sha-256|\n")
    if "FROM pg_roles" in sql:
        return "applyforge|f|f|f|f|f|t\napplyforge_restore|f|f|f|f|f|t\n"
    if "pg_auth_members" in sql:
        return "0\n"
    if "pg_database" in sql:
        return "applyforge|applyforge|UTF8|-1|\n"
    if "has_schema_privilege" in sql:
        return "f\n"
    if "pg_class" in sql:
        return "0\n"
    raise AssertionError(f"unexpected SQL: {sql}")


class HostCheckTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="hostcheck-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.host = FakeHost(self.root)
        self.build_good_host()

    # --- fixture ---------------------------------------------------------------------------

    def path(self, p):
        return os.path.join(self.root, p.lstrip("/"))

    def mkdir(self, p, mode=0o755, uid=0, gid=0):
        os.makedirs(self.path(p), exist_ok=True)
        os.chmod(self.path(p), mode)
        self.host.uids[p], self.host.gids[p] = uid, gid

    def write(self, p, text="", mode=0o644, uid=0, gid=0):
        os.makedirs(os.path.dirname(self.path(p)), exist_ok=True)
        with open(self.path(p), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.chmod(self.path(p), mode)
        self.host.uids[p], self.host.gids[p] = uid, gid

    def build_good_host(self):
        self.write("/etc/os-release", 'PRETTY_NAME="Ubuntu 26.04.1 LTS"\nID=ubuntu\nVERSION_ID="26.04"\n')
        self.write("/proc/cpuinfo", "processor\t: 0\nprocessor\t: 1\nprocessor\t: 2\n")
        self.write("/proc/meminfo", "MemTotal:        8000000 kB\n")
        for p in ("/srv/applyforge", "/srv/applyforge/releases", "/var/lib/applyforge"):
            self.mkdir(p)
        self.mkdir("/var/lib/applyforge/media", 0o750, APP_UID, APP_UID)
        self.mkdir("/var/lib/applyforge/static", 0o755, APP_UID, APP_UID)
        self.mkdir("/etc/applyforge", 0o750, 0, APP_UID)
        self.write("/etc/applyforge/applyforge.env", SECRET_ENV_TEXT, 0o640, 0, APP_UID)
        self.write("/etc/ssl/applyforge/applyforge.key", SECRET_KEY_TEXT, 0o600)
        self.write("/etc/ssl/applyforge/applyforge.crt", "-----BEGIN CERTIFICATE-----\nfictional\n")
        rel = f"/srv/applyforge/releases/{SHA}"
        self.rel = rel
        for p, text, mode in (("frontend/dist/index.html", "<!doctype html>", 0o644),
                              ("deploy/preflight.py", "# preflight", 0o644),
                              (".venv/bin/gunicorn", "#!python", 0o755),
                              (".venv/bin/python", "#!python", 0o755),
                              ("backend/constraints.txt", CONSTRAINTS, 0o644)):
            self.write(f"{rel}/{p}", text, mode)
        for installed, source in host_check.INSTALLED_COPIES:
            self.write(f"{rel}/{source}", f"fictional {source}\n", 0o755 if "bin" in source or "afbackup" in source
                       else 0o644)
            self.write(installed, f"fictional {source}\n", 0o755 if installed.startswith("/usr/local") else 0o644)
        for dirpath, dirnames, _ in os.walk(self.path(rel)):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o755)
        os.symlink(f"releases/{SHA}", self.path("/srv/applyforge/current"))
        self.write(host_check.NGINX_SITE, NGINX_SITE)
        os.makedirs(self.path("/etc/nginx/sites-enabled"), exist_ok=True)
        os.symlink("../sites-available/applyforge.conf", self.path(host_check.NGINX_ENABLED))
        self.write(host_check.BACKUP_CONFIG, "BACKUP_DESTINATION=/mnt/applyforge-backups\n"
                                             "BACKUP_ALLOWED_FS_TYPES=nfs4\n"
                                             "BACKUP_RECIPIENTS_FILE=/etc/applyforge/backup-recipients.txt\n")
        self.write("/etc/applyforge/backup-recipients.txt",
                   "# public\nage1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq\n")
        self.write("/mnt/applyforge-backups/.applyforge-backup-destination")
        self.write("/var/lib/applyforge-backup/index.tsv",
                   "2026-11-20T03:30:00Z\tapplyforge-backup-20261120T033000Z.tar.age\t" + "a" * 64 +
                   "\t1000\t" + "b" * 64 + "\tserver\trecorded\n", 0o600)

    def check(self, *argv):
        out = io.StringIO()
        code = host_check.main(list(argv), host=self.host, out=out)
        self.out = out.getvalue()
        return code

    def lines(self, tag):
        return [line for line in self.out.splitlines() if line.startswith(tag + " ")]

    # --- tests -----------------------------------------------------------------------------

    def test_good_host_passes(self):
        self.assertEqual(self.check(), 0, self.out)
        self.assertEqual(self.lines("FAIL"), [], self.out)
        unexpected = [c for c in self.host.commands if answer(c)[0] == 127 and "fake host" in answer(c)[2]]
        self.assertEqual(unexpected, [])
        for expected in ("Ubuntu 26.04.1 LTS (the validated platform)", "virtualenv matches backend/constraints.txt",
                         "release tree is root-owned", "nftables has an input chain with policy drop",
                         "PUBLIC has no privileges on database applyforge", "certificate covers applyforge.vpn.internal",
                         "last recorded backup", "listen_addresses = localhost"):
            self.assertIn(expected, self.out)

    def test_never_reads_secrets(self):
        self.check()
        secrets = {"/etc/applyforge/applyforge.env", "/etc/ssl/applyforge/applyforge.key"}
        self.assertFalse(secrets & set(self.host.read_paths), self.host.read_paths)
        self.assertFalse(secrets & set(self.host.hashed_paths))
        self.assertNotIn("must-never-be-read", self.out)
        for command in self.host.commands:
            self.assertFalse(any(part in ("/etc/applyforge/applyforge.env", self.path("/etc/applyforge/applyforge.env"),
                                          self.path("/etc/ssl/applyforge/applyforge.key")) for part in command))

    def test_runs_only_read_only_commands(self):
        self.check()
        for command in self.host.commands:
            self.assertFalse(MUTATING & set(command), command)
            if "psql" in command:
                self.assertEqual(command[:4], ["runuser", "-u", "postgres", "--"])
                self.assertIn("SET default_transaction_read_only = on", command)
                self.assertLess(command.index("SET default_transaction_read_only = on"), len(command) - 1)
            if command[:1] == ["nft"]:
                self.assertEqual(command[1], "list")

    def test_default_nginx_site_fails(self):
        self.write(host_check.NGINX_DEFAULT_SITE, "server { listen 80 default_server; }")
        self.assertEqual(self.check(), 1)
        self.assertTrue(any("default site" in l or "sites-enabled/default" in l for l in self.lines("FAIL")))

    def test_wildcard_listen_fails(self):
        self.write(host_check.NGINX_SITE, NGINX_SITE.replace("10.99.1.1:443", "443"))
        self.assertEqual(self.check(), 1)
        self.assertTrue(any("listens on all addresses" in l for l in self.lines("FAIL")))

    def test_env_file_too_permissive_fails(self):
        os.chmod(self.path("/etc/applyforge/applyforge.env"), 0o644)
        self.assertEqual(self.check(), 1)
        self.assertTrue(any("applyforge.env" in l and "more permissive" in l for l in self.lines("FAIL")))

    def test_media_wrong_owner_fails(self):
        self.host.uids["/var/lib/applyforge/media"] = 0
        self.assertEqual(self.check(), 1)
        self.assertTrue(any("media" in l and "owner root" in l for l in self.lines("FAIL")))

    def test_dotenv_in_release_fails(self):
        self.write(f"{self.rel}/backend/.env", "fictional")
        self.assertEqual(self.check(), 1)
        self.assertTrue(any(".env file inside the release" in l for l in self.lines("FAIL")))

    def test_writable_release_file_fails(self):
        os.chmod(self.path(f"{self.rel}/deploy/preflight.py"), 0o666)
        self.assertEqual(self.check(), 1)
        self.assertTrue(any("group/world-writable" in l for l in self.lines("FAIL")))

    def test_gunicorn_on_all_addresses_fails(self):
        self.host.overrides["ss -Htlnp"] = (0, 'LISTEN 0 2048 0.0.0.0:8000 0.0.0.0:* users:(("gunicorn",pid=1,fd=5))\n', "")
        self.assertEqual(self.check(), 1)
        self.assertTrue(any("0.0.0.0:8000" in l for l in self.lines("FAIL")))

    def test_missing_firewall_warns(self):
        self.host.overrides["nft list ruleset"] = (0, "", "")
        self.assertEqual(self.check(), 0)
        self.assertTrue(any("firewall" in l for l in self.lines("WARN")))
        self.assertEqual(self.check("--strict"), 1)

    def test_constraints_drift_fails(self):
        venv = "/srv/applyforge/current/.venv/bin/python -m pip freeze"
        self.host.overrides[venv] = (0, "Django==5.2.18\ngunicorn==27.0.1\n", "")
        self.assertEqual(self.check(), 1)
        self.assertTrue(any("Django 5.2.18 (pinned 5.2.19)" in l and "psycopg2-binary missing" in l
                            for l in self.lines("FAIL")), self.out)

    def test_postgres_findings(self):
        real = psql_answer

        def bad(sql):
            if "pg_hba_file_rules" in sql:
                return "local|all|all|||trust|\nhost|all|all|0.0.0.0|0.0.0.0|md5|\n"
            if "FROM pg_roles" in sql:
                return "applyforge|f|f|t|f|f|t\napplyforge_restore|t|f|f|f|f|t\n"
            if "pg_database" in sql:
                return "applyforge|applyforge|UTF8|-1|CONNECT,TEMPORARY\n"
            if "pg_settings" in sql:
                return "listen_addresses|*\npassword_encryption|md5\n"
            return real(sql)
        globals()["psql_answer"] = bad
        try:
            self.assertEqual(self.check(), 1)
        finally:
            globals()["psql_answer"] = real
        fails, warns = "\n".join(self.lines("FAIL")), "\n".join(self.lines("WARN"))
        for expected in ("'trust' rules", "from any address", "role applyforge: CREATEDB",
                         "role applyforge_restore: SUPERUSER", "listen_addresses = *", "local peer authentication is missing"):
            self.assertIn(expected, fails)
        self.assertIn("REVOKE CONNECT, TEMPORARY ON DATABASE applyforge FROM PUBLIC", warns)
        self.assertIn("md5", warns)

    def test_private_age_key_on_server_fails_without_printing_it(self):
        self.write("/etc/applyforge/backup-recipients.txt", "AGE-SECRET-KEY-1FICTIONALNOTAREALKEY\n")
        self.assertEqual(self.check(), 1)
        self.assertTrue(any("PRIVATE key" in l for l in self.lines("FAIL")))
        self.assertNotIn("FICTIONALNOTAREALKEY", self.out)

    def test_stale_backup_warns_and_unmounted_destination_warns(self):
        self.write("/var/lib/applyforge-backup/index.tsv",
                   "2026-11-10T03:30:00Z\tapplyforge-backup-20261110T033000Z.tar.age\t" + "a" * 64 +
                   "\t1000\t" + "b" * 64 + "\tserver\trecorded\n", 0o600)
        self.host.overrides["findmnt -n -o FSTYPE,SOURCE,OPTIONS --mountpoint"] = (1, "", "")
        self.check()
        warns = "\n".join(self.lines("WARN"))
        self.assertIn("h old", warns)
        self.assertIn("is not mounted now", warns)

    def test_not_root_skips_postgres(self):
        self.host.is_root = False
        self.check()
        self.assertIn("not running as root", self.out)
        self.assertFalse(any("psql" in c for c in self.host.commands))

    def test_expired_certificate_fails(self):
        self.host.overrides["openssl x509 -checkend 0"] = (1, "Certificate will expire\n", "")
        self.host.overrides["openssl x509 -checkend"] = (1, "Certificate will expire\n", "")
        self.assertEqual(self.check(), 1)
        self.assertTrue(any("has expired" in l for l in self.lines("FAIL")))

    def test_rejects_unsafe_identifiers(self):
        with self.assertRaises(SystemExit):
            host_check.main(["--db-name", "x'; DROP DATABASE applyforge; --"], host=self.host, out=io.StringIO())

    def test_parsers(self):
        self.assertEqual(host_check.listen_address("10.0.0.1:443"), "10.0.0.1")
        self.assertEqual(host_check.listen_address("443"), "")
        self.assertEqual(host_check.listen_address("[::]:443"), "[::]")
        self.assertEqual(host_check.parse_listeners(
            'LISTEN 0 511 [::1]:5432 [::]:* users:(("postgres",pid=1,fd=2))\n'), [("[::1]", "5432", "postgres")])
        self.assertEqual(host_check.version_tuple("nginx/1.28.3"), (1, 28, 3))


@unittest.skipUnless(sys.platform.startswith("linux") and os.environ.get("HOST_CHECK_REAL") == "1",
                     "set HOST_CHECK_REAL=1 on Linux to run the check against this real machine")
class RealHostSmokeTest(unittest.TestCase):
    def test_runs_to_completion(self):
        out = io.StringIO()
        code = host_check.main(["--skip-postgres"], out=out)
        self.assertIn(code, (0, 1))
        self.assertIn("RESULT:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
