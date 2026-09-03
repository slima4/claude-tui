#!/usr/bin/env python3
"""Tests for claudetui.py — version detection and sniffer discovery.

Usage: python3 test_claudetui.py -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claudetui
from claudetui import (
    _FALLBACK_VERSION, _get_version, _is_sniffer_url, _sniffer_ports,
)

CLAUDETUI_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "claudetui.py")
HAVE_GIT = shutil.which("git") is not None


# ── Version detection ─────────────────────────────────────────────

class TestGetVersion(unittest.TestCase):
    def test_reads_git_tag_in_a_checkout(self):
        with mock.patch("claudetui.os.path.exists", return_value=True), \
             mock.patch("claudetui.subprocess.check_output",
                        return_value=b"v1.2.3\n"):
            self.assertEqual(_get_version(), "1.2.3")

    def test_skips_git_entirely_outside_a_checkout(self):
        # git must not even run: from an installed libexec it walks up into
        # whatever repository contains it and reports that project's tag
        with mock.patch("claudetui.os.path.exists", return_value=False), \
             mock.patch("claudetui.subprocess.check_output") as run:
            self.assertEqual(_get_version(), _FALLBACK_VERSION)
            run.assert_not_called()

    def test_falls_back_when_git_fails(self):
        with mock.patch("claudetui.os.path.exists", return_value=True), \
             mock.patch("claudetui.subprocess.check_output",
                        side_effect=subprocess.CalledProcessError(1, "git")):
            self.assertEqual(_get_version(), _FALLBACK_VERSION)

    def test_falls_back_when_no_tag_exists(self):
        with mock.patch("claudetui.os.path.exists", return_value=True), \
             mock.patch("claudetui.subprocess.check_output", return_value=b"\n"):
            self.assertEqual(_get_version(), _FALLBACK_VERSION)


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestGetVersionInstalled(unittest.TestCase):
    """A copy installed under an unrelated repo must not adopt that repo's tag."""

    def test_install_nested_in_another_repo_reports_own_version(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ)
            env.update({
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            })

            def git(*argv):
                subprocess.run(["git", "-C", d, *argv], check=True, env=env,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)

            git("init", "-q")
            git("commit", "-q", "--allow-empty", "-m", "x")
            git("tag", "v99.99.99")

            # Mirrors the Homebrew layout: libexec sits inside /opt/homebrew,
            # which is itself a git checkout
            libexec = os.path.join(d, "Cellar", "claude-tui", "0.0.0", "libexec")
            os.makedirs(libexec)
            shutil.copy(CLAUDETUI_PY, libexec)

            out = subprocess.run(
                [sys.executable, os.path.join(libexec, "claudetui.py"), "--version"],
                capture_output=True, text=True, check=True, env=env,
            ).stdout.strip()

            self.assertEqual(out, f"claudetui {_FALLBACK_VERSION}")
            self.assertNotIn("99.99.99", out)

    def test_checkout_still_reports_its_tag(self):
        out = subprocess.run([sys.executable, CLAUDETUI_PY, "--version"],
                             capture_output=True, text=True, check=True).stdout
        self.assertTrue(out.startswith("claudetui "), out)
        self.assertNotEqual(out.strip(), "claudetui ")


# ── Sniffer discovery ─────────────────────────────────────────────

class TestSnifferPorts(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def add_port(self, port):
        with open(os.path.join(self.dir, f".port.{port}"), "w") as f:
            f.write(str(port))

    def test_empty_dir(self):
        self.assertEqual(_sniffer_ports(self.dir), set())

    def test_missing_dir(self):
        self.assertEqual(_sniffer_ports(os.path.join(self.dir, "nope")), set())

    def test_finds_every_port_file(self):
        self.add_port(7735)
        self.add_port(7736)
        self.assertEqual(_sniffer_ports(self.dir), {"7735", "7736"})

    def test_ignores_log_files(self):
        self.add_port(7735)
        with open(os.path.join(self.dir, "sniffer-20260101-000000.jsonl"), "w") as f:
            f.write("{}\n")
        self.assertEqual(_sniffer_ports(self.dir), {"7735"})


class TestIsSnifferUrl(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        with open(os.path.join(self.dir, ".port.7735"), "w") as f:
            f.write("7735")

    def check(self, url):
        return _is_sniffer_url(url, self.dir)

    def test_running_sniffer(self):
        self.assertTrue(self.check("http://localhost:7735"))
        self.assertTrue(self.check("http://127.0.0.1:7735"))

    def test_local_gateway_is_not_a_sniffer(self):
        # The base URL a user routes through LiteLLM — replacing it silently
        # would send their traffic straight past the gateway
        self.assertFalse(self.check("http://localhost:4000"))

    def test_remote_gateway(self):
        self.assertFalse(self.check("https://gateway.corp/anthropic"))

    def test_url_without_a_port(self):
        self.assertFalse(self.check("http://localhost"))

    def test_ipv6_loopback(self):
        self.assertTrue(self.check("http://[::1]:7735"))

    def test_missing_port_dir(self):
        self.assertFalse(_is_sniffer_url("http://localhost:7735",
                                         os.path.join(self.dir, "nope")))

    def test_malformed_url(self):
        self.assertFalse(self.check("http://localhost:not-a-port"))
        self.assertFalse(self.check(""))


if __name__ == "__main__":
    unittest.main()
