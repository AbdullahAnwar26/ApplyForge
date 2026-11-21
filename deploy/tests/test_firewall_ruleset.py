"""Guards for deploy/nftables/applyforge.nft (network rehearsal, 2026-11-21).

The text checks always run. The syntax check (nft -c) runs only as root with nft installed;
it never changes the live ruleset.
"""

import os
import re
import shutil
import subprocess
import unittest

RULESET = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "nftables", "applyforge.nft")
BACKUP_RULESET = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "nftables",
                              "applyforge-backup-server.nft")


def rules(path=RULESET):
    with open(path, encoding="utf-8") as fh:
        return [line.split("#", 1)[0].strip() for line in fh if line.split("#", 1)[0].strip()]


def input_chain(path=RULESET):
    text = "\n".join(rules(path))
    match = re.search(r"chain input \{(.*?)\n\}", text, re.S)
    if not match:
        raise AssertionError("no input chain")
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]


class RulesetTextTests(unittest.TestCase):

    def test_input_default_policy_is_drop(self):
        self.assertIn("type filter hook input priority filter; policy drop;", input_chain())

    def test_forward_default_policy_is_drop(self):
        self.assertIn("type filter hook forward priority filter; policy drop;", rules())

    def test_admin_and_web_ports_only_on_the_vpn_interface(self):
        for line in input_chain():
            if re.search(r"tcp dport", line):
                self.assertTrue(line.startswith('iifname "wg0" '), f"TCP accepted outside wg0: {line}")
        self.assertIn('iifname "wg0" tcp dport { 22, 80, 443 } accept', input_chain())

    def test_only_wireguard_is_reachable_from_outside(self):
        outside = [l for l in input_chain() if l.endswith("accept") and not l.startswith('iifname "wg0"')
                   and not l.startswith('iifname "lo"') and "ct state" not in l
                   and not l.startswith(("icmp type", "icmpv6 type"))]
        self.assertEqual(outside, ["udp dport $WG_PORT accept"])
        self.assertIn("define WG_PORT = 51820", rules())

    def test_echo_requests_only_on_the_vpn_interface(self):
        for line in input_chain():
            if "echo-request" in line:
                self.assertTrue(line.startswith('iifname "wg0" '), line)

    def test_established_accepted_and_invalid_dropped_first(self):
        chain = input_chain()
        self.assertLess(chain.index("ct state invalid drop"), chain.index("ct state { established, related } accept"))
        self.assertLess(chain.index("ct state { established, related } accept"), chain.index("udp dport $WG_PORT accept"))

    def test_drops_are_counted(self):
        self.assertIn('counter comment "dropped inbound"', input_chain())


class AppRulesetDnsTests(unittest.TestCase):
    """Two-server rehearsal: the private resolver answers on the VPN interface only."""

    def test_dns_only_on_the_vpn_interface(self):
        chain = input_chain()
        self.assertIn('iifname "wg0" udp dport 53 accept', chain)
        self.assertIn('iifname "wg0" tcp dport 53 accept', chain)
        for line in chain:
            if "dport 53" in line:
                self.assertTrue(line.startswith('iifname "wg0" '), line)


class BackupServerRulesetTests(unittest.TestCase):
    """deploy/nftables/applyforge-backup-server.nft (two-server rehearsal)."""

    def chain(self):
        return input_chain(BACKUP_RULESET)

    def test_default_drop(self):
        self.assertIn("type filter hook input priority filter; policy drop;", self.chain())
        self.assertIn("type filter hook forward priority filter; policy drop;", rules(BACKUP_RULESET))

    def test_nfs_only_on_the_backup_link_from_the_app_link_address(self):
        nfs = [l for l in self.chain() if "2049" in l]
        self.assertEqual(nfs, ['iifname "wg1" ip saddr $APP_LINK_ADDR tcp dport 2049 accept'])
        self.assertIn("define APP_LINK_ADDR = 10.98.0.1", rules(BACKUP_RULESET))

    def test_ssh_only_on_the_admin_interface(self):
        self.assertEqual([l for l in self.chain() if "dport 22" in l], ['iifname "wg0" tcp dport 22 accept'])

    def test_only_the_two_wireguard_ports_from_outside(self):
        outside = [l for l in self.chain() if l.endswith("accept")
                   and not l.startswith(('iifname "wg0"', 'iifname "wg1"', 'iifname "lo"'))
                   and "ct state" not in l and not l.startswith(("icmp type", "icmpv6 type"))]
        self.assertEqual(sorted(outside), sorted(["udp dport $WG_ADMIN_PORT accept",
                                                  "ip saddr $APP_PUBLIC_V4 udp dport $WG_LINK_PORT accept"]))

    def test_no_other_tcp_service(self):
        self.assertEqual(len([l for l in self.chain() if "tcp dport" in l]), 2)


@unittest.skipUnless(shutil.which("nft") and hasattr(os, "geteuid") and os.geteuid() == 0,
                     "nft -c needs root and the nft tool")
class RulesetSyntaxTests(unittest.TestCase):

    def test_nft_check_accepts_the_rulesets(self):
        for path in (RULESET, BACKUP_RULESET):
            result = subprocess.run(["nft", "-c", "-f", path], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
