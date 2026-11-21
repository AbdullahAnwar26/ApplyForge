"""Tests for the staging/production tooling that can run without servers:
deploy/hetzner/provision.sh (plan mode with a stub hcloud: no API call, valid firewall JSON, refusals) and
deploy/install/applyforge-install (site-file validation, WireGuard config rendering, client config without a
private key). The installer itself is exercised end to end on fresh VMs (deploy/PRODUCTION_READINESS.md).
Linux/WSL only (bash). The site-file tests need root (the file must be root-owned) and skip otherwise.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

DEPLOY = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
PROVISION = os.path.join(DEPLOY, "hetzner", "provision.sh")
INSTALLER = os.path.join(DEPLOY, "install", "applyforge-install")
POSIX = os.name == "posix" and shutil.which("bash")
ROOT = POSIX and os.geteuid() == 0
WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")
FAKE_PUB = "x" * 42 + "A="   # syntactically valid WireGuard public key, not a real one


@unittest.skipUnless(POSIX, "needs bash (run on Linux/WSL)")
class ProvisionPlanTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af-prov-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(bindir)
        self.calls = os.path.join(self.tmp, "calls")
        with open(os.path.join(bindir, "hcloud"), "w") as fh:
            fh.write(f'#!/bin/sh\necho "$*" >> {self.calls}\nexit 1\n')
        os.chmod(os.path.join(bindir, "hcloud"), 0o755)
        self.env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        self.env.pop("HCLOUD_TOKEN", None)
        self.pub = self.write("k.pub", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample t\n")

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def config(self, **over):
        values = {"PREFIX": "af-staging", "IMAGE": "ubuntu-26.04", "APP_TYPE": "cx33", "APP_LOCATION": "hel1",
                  "BACKUP_TYPE": "cx23", "BACKUP_LOCATION": "fsn1", "SSH_PUBKEY_FILE": self.pub,
                  "ADMIN_CIDR": "198.51.100.7/32"}
        values.update(over)
        return self.write("h.env", "".join(f"{k}={v}\n" for k, v in values.items()))

    def run_prov(self, *args, cfg=None):
        return subprocess.run(["bash", PROVISION, *args, "--config", cfg or self.config()], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def rules(self, out):
        return [json.loads(m) for m in re.findall(r"--rules-file <<(.*)>>", out)]

    def test_plan_makes_no_api_call_and_prints_the_whole_sequence(self):
        res = self.run_prov("plan")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.exists(self.calls), "plan must not call hcloud")
        for needle in ("hcloud ssh-key create", "hcloud firewall create --name af-staging-app-fw",
                       "hcloud server create --name af-staging-app --type cx33",
                       "hcloud server create --name af-staging-backup --type cx23", "enable-backup af-staging-backup"):
            self.assertIn(needle, res.stdout)

    def test_firewall_rules_are_valid_json_and_minimal(self):
        res = self.run_prov("plan")
        app, backup = self.rules(res.stdout)
        self.assertEqual({(r["protocol"], r["port"]) for r in app}, {("udp", "51820"), ("tcp", "22")})
        self.assertEqual({(r["protocol"], r["port"]) for r in backup}, {("udp", "51820"), ("udp", "51821"), ("tcp", "22")})
        ssh = [r for r in app + backup if r["port"] == "22"]
        self.assertTrue(all(r["source_ips"] == ["198.51.100.7/32"] for r in ssh), "SSH only from ADMIN_CIDR")
        link = [r for r in backup if r["port"] == "51821"][0]
        self.assertEqual(len(link["source_ips"]), 1, "backup link only from the app server")
        self.assertTrue(all(r["direction"] == "in" for r in app + backup))

    def test_close_ssh_rules_drop_only_ssh(self):
        res = self.run_prov("close-ssh")
        # close-ssh is not a dry run: with the failing stub it stops at preflight, before any rule change.
        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn("replace-rules", res.stdout)

    def test_refusals(self):
        self.assertNotEqual(self.run_prov("plan", cfg=self.config(ADMIN_CIDR="0.0.0.0/0")).returncode, 0)
        self.assertNotEqual(self.run_prov("plan", cfg=self.config(PREFIX="X")).returncode, 0)
        priv = self.write("id", "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n")
        res = self.run_prov("plan", cfg=self.config(SSH_PUBKEY_FILE=priv))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("PRIVATE", res.stderr)
        res = self.run_prov("destroy")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--confirm af-staging", res.stderr)
        self.assertFalse(os.path.exists(self.calls), "destroy without confirmation must not call hcloud")

    def test_config_is_not_evaluated_as_shell(self):
        marker = os.path.join(self.tmp, "pwned")
        res = self.run_prov("plan", cfg=self.config(IMAGE=f"$(touch {marker})"))
        self.assertFalse(os.path.exists(marker))
        self.assertIn(f"--image $(touch {marker})", res.stdout)


def bash_lib(snippet, env=None):
    """Source the installer (main does not run) and execute a snippet with its functions."""
    script = f'source "{INSTALLER}"\nset +e\n{snippet}\n'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, **(env or {})))


@unittest.skipUnless(POSIX, "needs bash (run on Linux/WSL)")
class InstallerLibraryTests(unittest.TestCase):

    def test_sourcing_does_not_run_main(self):
        res = bash_lib("echo sourced-ok")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "sourced-ok")

    def test_help_lists_every_command(self):
        res = subprocess.run(["bash", INSTALLER, "--help"], capture_output=True, text=True, timeout=30)
        for cmd in ("keys", "host", "release", "rollback", "link", "acme", "peer add", "client-config",
                    "firewall apply", "validate"):
            self.assertIn(cmd, res.stdout)

    def test_unknown_command_fails(self):
        res = subprocess.run(["bash", INSTALLER, "nonsense"], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(res.returncode, 0)

    def test_firewall_table_names_match_the_rulesets(self):
        # Regression (installer VM test): confirm/validate looked for table "applyforge" on the backup server.
        for role, name in (("app", "applyforge.nft"), ("backup", "applyforge-backup-server.nft")):
            with open(os.path.join(DEPLOY, "nftables", name)) as fh:
                table = re.search(r"^table inet (\S+) \{", fh.read(), re.M).group(1)
            res = bash_lib(f"fw_table {role}")
            self.assertEqual(res.stdout.strip(), table, role)

    def test_unbound_binds_without_an_ordering_drop_in(self):
        # Regression (installer VM test): "After=wg-quick@wg0" on unbound formed a cycle through
        # nss-lookup.target and systemd deleted the wg0 start job at boot.
        with open(os.path.join(DEPLOY, "unbound", "applyforge.conf")) as fh:
            self.assertRegex(fh.read(), r"(?m)^\s*ip-freebind:\s*yes\s*$")
        self.assertFalse(os.path.exists(os.path.join(DEPLOY, "systemd", "unbound-applyforge-vpn.conf")))
        with open(INSTALLER) as fh:
            self.assertNotIn("unbound-applyforge-vpn.conf", fh.read())

    @unittest.skipUnless(ROOT, "installs a root-owned file")
    def test_firewall_candidate_for_backup_server_gets_the_app_address(self):
        tmp = tempfile.mkdtemp(prefix="af-fw-")
        self.addCleanup(shutil.rmtree, tmp, True)
        res = bash_lib(f'FW_CANDIDATE={tmp}/cand.nft; S_APP_PUBLIC_IPV4=203.0.113.10\n'
                       f'nft() {{ return 0; }}\nrender_firewall backup && cat {tmp}/cand.nft')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("define APP_PUBLIC_V4 = { 203.0.113.10 }", res.stdout)
        self.assertNotIn("10.20.0.10", res.stdout)


@unittest.skipUnless(ROOT, "site-file tests need root (the file must be root-owned)")
class SiteFileTests(unittest.TestCase):

    GOOD = {"APP_HOST": "applyforge.staging.example.net", "APP_PUBLIC_IPV4": "203.0.113.10",
            "BACKUP_PUBLIC_IPV4": "203.0.113.20", "APP_WG1_PUBKEY": FAKE_PUB, "BACKUP_WG1_PUBKEY": "",
            "ADMIN_USER": "afadmin"}

    def site(self, mode=0o644, **over):
        values = dict(self.GOOD, **over)
        fd, path = tempfile.mkstemp(prefix="af-site-")
        with os.fdopen(fd, "w") as fh:
            fh.write("# comment\n\n" + "".join(f"{k}={v}\n" for k, v in values.items()))
        os.chmod(path, mode)
        self.addCleanup(os.unlink, path)
        return path

    def load(self, path):
        return bash_lib('load_site && echo "OK $S_APP_HOST $S_KEEP_RELEASES $S_INPUTS_DIR"', {"APPLYFORGE_SITE": path})

    def test_valid_site_with_defaults(self):
        res = self.load(self.site())
        self.assertEqual(res.stdout.strip(), "OK applyforge.staging.example.net 3 /root/applyforge-inputs", res.stderr)

    def test_rejections(self):
        cases = {"example host": {"APP_HOST": "applyforge.example.org"}, "uppercase host": {"APP_HOST": "App.Example.net"},
                 "bad ip": {"APP_PUBLIC_IPV4": "203.0.113"}, "bad key": {"APP_WG1_PUBKEY": "notakey="},
                 "bad admin": {"ADMIN_USER": "Root!"}, "keep 1": {"KEEP_RELEASES": "1"}}
        for name, over in cases.items():
            with self.subTest(name):
                res = self.load(self.site(**over))
                self.assertNotEqual(res.returncode, 0)
                self.assertNotIn("OK ", res.stdout)

    def test_group_writable_site_refused(self):
        res = self.load(self.site(mode=0o664))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not group/world-writable", res.stderr)

    def test_site_is_not_evaluated_as_shell(self):
        marker = tempfile.mktemp(prefix="af-pwned-")
        res = self.load(self.site(TIME_ZONE=f"$(touch {marker})"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main()
