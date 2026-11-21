"""Tests for deploy/monitoring (applyforge-ping, applyforge-check) and deploy/acme (applyforge-acme,
applyforge-cert-deploy). External commands (curl for checks, dig, df, nginx, systemctl, lego, s_client) are
replaced by stubs on PATH; a local HTTP server stands in for Healthchecks. Linux/WSL only (bash, openssl).
"""

import http.server
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest

DEPLOY = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
PING = os.path.join(DEPLOY, "monitoring", "applyforge-ping")
CHECK = os.path.join(DEPLOY, "monitoring", "applyforge-check")
ACME = os.path.join(DEPLOY, "acme", "applyforge-acme")
DEPLOY_HOOK = os.path.join(DEPLOY, "acme", "applyforge-cert-deploy")
POSIX = os.name == "posix" and shutil.which("bash") and shutil.which("openssl") and shutil.which("curl")


class Receiver(http.server.BaseHTTPRequestHandler):
    paths = []

    def do_GET(self):
        Receiver.paths.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    do_HEAD = do_POST = do_GET

    def log_message(self, *args):
        pass


@unittest.skipUnless(POSIX, "needs bash, curl and openssl (run on Linux/WSL)")
class ScriptTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af-mon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        Receiver.paths = []
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/ping/"
        self.hc = self.write("healthchecks.env", "".join(
            f"HC_{n}={self.base}uuid-{n.lower()}\n" for n in ("HEALTH", "DNS", "DISK", "CERT", "BACKUP", "BACKUP_DISK")), 0o600)

    def write(self, name, text, mode=0o644):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(text)
        os.chmod(path, mode)
        return path

    def stub(self, name, script):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/bash\n" + script)
        os.chmod(path, 0o755)

    def run_script(self, args, env=None):
        full = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", APPLYFORGE_HC_CONF=self.hc)
        full.update(env or {})
        return subprocess.run(["bash", *args], capture_output=True, text=True, env=full, timeout=60)


class PingTests(ScriptTestCase):

    def test_success_fail_and_start(self):
        for kind, suffix in (("success", ""), ("fail", "/fail"), ("start", "/start")):
            r = self.run_script([PING, "health", kind])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(Receiver.paths[-1], f"/ping/uuid-health{suffix}")

    def test_dash_in_check_name_maps_to_underscore(self):
        self.assertEqual(self.run_script([PING, "backup-disk"]).returncode, 0)
        self.assertEqual(Receiver.paths[-1], "/ping/uuid-backup_disk")

    def test_unknown_check_or_kind_is_a_usage_error_and_url_never_printed(self):
        for args in (["nosuch"], ["health", "maybe"], ["Bad Name"], []):
            r = self.run_script([PING, *args])
            self.assertEqual(r.returncode, 2, args)
            self.assertNotIn("uuid-", r.stdout + r.stderr)

    def test_unreachable_receiver_exits_1_without_printing_the_url(self):
        self.write("healthchecks.env", "HC_HEALTH=http://127.0.0.1:9/ping/secret-uuid\n", 0o600)
        r = self.run_script([PING, "health"])
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("secret-uuid", r.stdout + r.stderr)

    def test_unreadable_config(self):
        r = self.run_script([PING, "health"], env={"APPLYFORGE_HC_CONF": os.path.join(self.tmp, "missing")})
        self.assertEqual(r.returncode, 2)


class CheckTests(ScriptTestCase):

    def setUp(self):
        super().setUp()
        self.watched = os.path.join(self.tmp, "watched")
        os.mkdir(self.watched)
        self.mon = self.write("monitoring.env", f'APP_HOST=app.test\nVPN_ADDR=10.99.0.1\nDISK_PATHS="{self.watched}"\n'
                                                f"DISK_MAX_PCT=80\nCERT_FILE={self.tmp}/cert.pem\nCERT_MIN_DAYS=21\n")

    def check(self, what, **env):
        return self.run_script([CHECK, what], env=dict({"APPLYFORGE_MON_CONF": self.mon, "APPLYFORGE_PING": PING}, **env))

    def test_health_ok_and_failing(self):
        real_curl = shutil.which("curl")
        self.stub("curl", f'case "$*" in *api/v1/health*) echo \'{{"status": "ok", "database": "ok"}}\';; *) exec {real_curl} "$@";; esac\n')
        self.assertEqual(self.check("health").returncode, 0)
        self.assertEqual(Receiver.paths[-1], "/ping/uuid-health")
        self.stub("curl", f'case "$*" in *api/v1/health*) echo "curl: (7) connection refused" >&2; exit 7;; *) exec {real_curl} "$@";; esac\n')
        r = self.check("health")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(Receiver.paths[-1], "/ping/uuid-health/fail")
        self.assertIn("health endpoint", r.stderr)

    def test_dns_ok_and_wrong_answer(self):
        self.stub("dig", "echo 10.99.0.1\n")
        self.assertEqual(self.check("dns").returncode, 0)
        self.stub("dig", "echo ';; connection timed out'\n")
        self.assertEqual(self.check("dns").returncode, 1)
        self.assertEqual(Receiver.paths[-1], "/ping/uuid-dns/fail")

    def test_disk_threshold_and_prefix(self):
        self.stub("df", 'echo "Use%"; echo " 42%"\n')
        self.assertEqual(self.check("disk").returncode, 0)
        self.stub("df", 'echo "Use%"; echo " 91%"\n')
        r = self.check("disk")
        self.assertEqual(r.returncode, 1)
        self.assertIn("91% used", r.stderr)
        with open(self.mon, "a") as fh:
            fh.write("CHECK_NAME_PREFIX=backup-\n")
        self.check("disk")
        self.assertEqual(Receiver.paths[-1], "/ping/uuid-backup_disk/fail")

    def test_certificate_expiry_threshold(self):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "10", "-subj", "/CN=app.test",
                        "-keyout", f"{self.tmp}/key.pem", "-out", f"{self.tmp}/cert.pem"], capture_output=True, check=True)
        r = self.check("cert")                                   # 10 days left, threshold 21
        self.assertEqual(r.returncode, 1)
        self.assertIn("expires within 21 days", r.stderr)
        with open(self.mon, "a") as fh:
            fh.write("CERT_MIN_DAYS=5\n")
        self.assertEqual(self.check("cert").returncode, 0)

    def test_unknown_check(self):
        self.assertEqual(self.check("everything").returncode, 2)


class CertDeployTests(ScriptTestCase):

    def setUp(self):
        super().setUp()
        self.tls = os.path.join(self.tmp, "tls")
        os.mkdir(self.tls)
        self.old_cert, self.old_key = self.make_pair("old", "app.test")
        shutil.copy(self.old_cert, os.path.join(self.tls, "applyforge.crt"))
        shutil.copy(self.old_key, os.path.join(self.tls, "applyforge.key"))
        self.new_cert, self.new_key = self.make_pair("new", "app.test")
        self.mon = self.write("monitoring.env", "APP_HOST=app.test\nVPN_ADDR=10.99.0.1\n")
        self.calls = os.path.join(self.tmp, "calls.log")
        self.stub("nginx", f'echo "nginx $*" >> {self.calls}; [ "$*" = "-t" ] && [ -n "${{FAIL_NGINX_T:-}}" ] && exit 1; exit 0\n')
        self.stub("systemctl", f'echo "systemctl $*" >> {self.calls}; exit 0\n')
        real = shutil.which("openssl")
        # s_client returns what "nginx serves": the installed certificate unless SERVED_CERT says otherwise
        self.stub("openssl", f'if [ "$1" = s_client ]; then cat "${{SERVED_CERT:-{self.tls}/applyforge.crt}}"; exit 0; fi\nexec {real} "$@"\n')

    def make_pair(self, tag, host):
        cert, key = f"{self.tmp}/{tag}.crt", f"{self.tmp}/{tag}.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30", "-subj", f"/CN={host}",
                        "-addext", f"subjectAltName=DNS:{host}", "-keyout", key, "-out", cert], capture_output=True, check=True)
        return cert, key

    def deploy(self, cert=None, key=None, **env):
        return self.run_script([DEPLOY_HOOK], env=dict({
            "LEGO_CERT_PATH": cert or self.new_cert, "LEGO_CERT_KEY_PATH": key or self.new_key,
            "APPLYFORGE_TLS_DIR": self.tls, "APPLYFORGE_MON_CONF": self.mon, "APPLYFORGE_CERT_VERIFY_WAIT": "0"}, **env))

    def installed(self):
        with open(os.path.join(self.tls, "applyforge.crt")) as fh:
            return fh.read()

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def test_new_certificate_installed_and_reloaded(self):
        r = self.deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.installed(), self.read(self.new_cert))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tls, "applyforge.key")).st_mode), 0o600)
        self.assertIn("systemctl reload nginx", self.read(self.calls))
        self.assertFalse(os.path.exists(os.path.join(self.tls, "applyforge.crt.previous")))

    def test_nginx_rejecting_the_new_certificate_rolls_back(self):
        r = self.deploy(FAIL_NGINX_T="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("restoring the previous certificate", r.stderr)
        self.assertEqual(self.installed(), self.read(self.old_cert))
        self.assertNotIn("systemctl reload nginx", self.read(self.calls))

    def test_served_certificate_mismatch_fails(self):
        r = self.deploy(SERVED_CERT=self.old_cert)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nginx serves", r.stderr)

    def test_mismatched_key_changes_nothing(self):
        r = self.deploy(key=self.old_key)
        self.assertEqual(r.returncode, 1)
        self.assertIn("do not match", r.stderr)
        self.assertEqual(self.installed(), self.read(self.old_cert))

    def test_certificate_for_another_host_changes_nothing(self):
        cert, key = self.make_pair("other", "other.test")
        r = self.deploy(cert=cert, key=key)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not cover app.test", r.stderr)
        self.assertEqual(self.installed(), self.read(self.old_cert))


class AcmeWrapperTests(ScriptTestCase):

    def setUp(self):
        super().setUp()
        self.argv_log = os.path.join(self.tmp, "lego-argv")
        self.env_log = os.path.join(self.tmp, "lego-env")
        self.stub("lego", f'printf "%s\\n" "$@" > {self.argv_log}; env > {self.env_log}\n')
        self.conf = self.write("acme.env", "ACME_DOMAIN=app.test\nACME_EMAIL=owner@example.test\nACME_DNS_PROVIDER=rfc2136\n"
                                           "ACME_RENEW_DAYS=30\nRFC2136_TSIG_SECRET=very-secret-token-value\n", 0o600)

    def acme(self, *args):
        return self.run_script([ACME, *args], env={"APPLYFORGE_ACME_CONF": self.conf})

    def test_credential_only_in_the_environment_never_in_argv(self):
        self.assertEqual(self.acme("issue").returncode, 0)
        self.assertNotIn("very-secret-token-value", self.read(self.argv_log))
        self.assertIn("RFC2136_TSIG_SECRET=very-secret-token-value", self.read(self.env_log))
        self.assertIn("--run-hook", self.read(self.argv_log))

    def test_renew_days_from_config_and_override(self):
        self.acme("renew")
        self.assertIn("--days\n30\n", self.read(self.argv_log))
        self.acme("renew", "--days", "100000")
        self.assertIn("--days\n100000\n", self.read(self.argv_log))

    def test_bad_days_and_usage(self):
        self.assertEqual(self.acme("renew", "--days", "soon").returncode, 2)
        self.assertEqual(self.acme("revoke").returncode, 2)

    def test_missing_settings(self):
        self.write("acme.env", "ACME_DOMAIN=app.test\n", 0o600)
        r = self.acme("issue")
        self.assertEqual(r.returncode, 2)
        self.assertIn("ACME_EMAIL is not set", r.stderr)

    def read(self, path):
        with open(path) as fh:
            return fh.read()


if __name__ == "__main__":
    unittest.main()
