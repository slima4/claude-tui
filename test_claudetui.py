#!/usr/bin/env python3
"""Tests for claudetui.py — version detection and sniffer discovery.

Usage: python3 test_claudetui.py -v
"""
import os
import re
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


# ── Python environment scrub ──────────────────────────────────────

class TestRestorePythonEnv(unittest.TestCase):
    def test_restores_what_the_wrapper_saved(self):
        env = {"CLAUDETUI_ORIG_PYTHONHOME": "/opt/app/usr",
               "CLAUDETUI_ORIG_PYTHONPATH": "/opt/app/lib"}
        claudetui._restore_python_env(env)
        self.assertEqual(env, {"PYTHONHOME": "/opt/app/usr",
                               "PYTHONPATH": "/opt/app/lib"})

    def test_original_beats_the_path_the_dispatcher_injected(self):
        env = {"PYTHONPATH": "/install/dir:/opt/app/lib",
               "CLAUDETUI_ORIG_PYTHONHOME": "",
               "CLAUDETUI_ORIG_PYTHONPATH": "/opt/app/lib"}
        claudetui._restore_python_env(env)
        self.assertEqual(env, {"PYTHONPATH": "/opt/app/lib"})

    def test_unset_originals_are_removed_not_left_injected(self):
        # The wrapper saves an empty string when the variable was not set; the
        # dispatcher's own PYTHONPATH must not reach claude in that case.
        env = {"PYTHONPATH": "/install/dir",
               "CLAUDETUI_ORIG_PYTHONHOME": "", "CLAUDETUI_ORIG_PYTHONPATH": ""}
        claudetui._restore_python_env(env)
        self.assertEqual(env, {})

    def test_untouched_without_the_wrapper(self):
        # Run straight from a checkout there is nothing saved, so whatever
        # the environment holds is left alone.
        env = {"PYTHONPATH": "/mine", "HOME": "/h"}
        claudetui._restore_python_env(env)
        self.assertEqual(env, {"PYTHONPATH": "/mine", "HOME": "/h"})


INSTALL_SH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "install.sh")
HAVE_BASH = shutil.which("bash") is not None and shutil.which("env") is not None


@unittest.skipUnless(HAVE_BASH, "needs bash and env")
class TestInstalledWrapper(unittest.TestCase):
    """The claudetui wrapper install.sh writes, rendered and run for real."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        with open(INSTALL_SH) as f:
            match = re.search(r'cat > "\$BIN_DIR/claudetui" << EOF\n(.*?)\nEOF\n',
                              f.read(), re.S)
        assert match, "wrapper heredoc not found in install.sh"
        # Let bash expand the heredoc exactly as the installer would.
        rendered = subprocess.run(
            ["bash", "-c", f"cat <<EOF\n{match.group(1)}\nEOF"],
            env={**os.environ,
                 "PYTHON_ABS": sys.executable,
                 "INSTALL_DIR": os.path.dirname(CLAUDETUI_PY)},
            capture_output=True, text=True, check=True,
        ).stdout
        cls.wrapper = os.path.join(cls.tmp, "claudetui")
        with open(cls.wrapper, "w") as f:
            f.write(rendered)
        os.chmod(cls.wrapper, 0o755)

        # A stand-in for `claude` that reports what environment it received.
        cls.bin = os.path.join(cls.tmp, "bin")
        os.mkdir(cls.bin)
        fake = os.path.join(cls.bin, "claude")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\n'
                    'echo "ARGS=$*"\n'
                    'echo "PYTHONHOME=${PYTHONHOME-unset}"\n'
                    'echo "PYTHONPATH=${PYTHONPATH-unset}"\n'
                    'echo "ORIG_HOME=${CLAUDETUI_ORIG_PYTHONHOME-unset}"\n'
                    'echo "ORIG_PATH=${CLAUDETUI_ORIG_PYTHONPATH-unset}"\n')
        os.chmod(fake, 0o755)

        # A session for `sessions resume` to find under the fake HOME.
        project = os.path.join(cls.tmp, ".claude", "projects", "proj")
        os.makedirs(project)
        with open(os.path.join(project, "abc123.jsonl"), "w") as f:
            f.write("{}\n")

    def run_wrapper(self, args, **env):
        base = {k: v for k, v in os.environ.items()
                if k not in ("PYTHONHOME", "PYTHONPATH")}
        # Keep the real HOME out of reach: sniff looks for running sniffers there.
        base.update(HOME=self.tmp, PATH=f"{self.bin}{os.pathsep}{base['PATH']}")
        base.update(env)
        return subprocess.run([self.wrapper, *args], env=base,
                              capture_output=True, text=True, timeout=30)

    def test_inherited_pythonhome_does_not_break_it(self):
        r = self.run_wrapper(["--version"], PYTHONHOME="/nonexistent/usr/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claudetui", r.stdout)

    def test_inherited_pythonpath_is_not_loaded(self):
        poisoned = os.path.join(self.tmp, "poisoned")
        os.makedirs(poisoned, exist_ok=True)
        with open(os.path.join(poisoned, "sitecustomize.py"), "w") as f:
            f.write('print("POISONED")\n')
        r = self.run_wrapper(["--version"], PYTHONPATH=poisoned)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("POISONED", r.stdout)

    def test_sniff_hands_claude_the_original_environment(self):
        r = self.run_wrapper(["sniff"], PYTHONHOME="/nonexistent/usr/",
                             PYTHONPATH="/my/libs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PYTHONHOME=/nonexistent/usr/", r.stdout)
        self.assertIn("PYTHONPATH=/my/libs", r.stdout)
        # ...and the bookkeeping does not leak into claude.
        self.assertIn("ORIG_HOME=unset", r.stdout)
        self.assertIn("ORIG_PATH=unset", r.stdout)

    def test_sniff_adds_nothing_when_the_user_had_nothing(self):
        r = self.run_wrapper(["sniff"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PYTHONHOME=unset", r.stdout)
        self.assertIn("PYTHONPATH=unset", r.stdout)

    def test_resume_hands_claude_the_original_environment(self):
        r = self.run_wrapper(["sessions", "resume", "abc123"],
                             PYTHONHOME="/nonexistent/usr/",
                             PYTHONPATH="/my/libs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ARGS=--resume abc123", r.stdout)
        self.assertIn("PYTHONHOME=/nonexistent/usr/", r.stdout)
        self.assertIn("PYTHONPATH=/my/libs", r.stdout)
        self.assertIn("ORIG_HOME=unset", r.stdout)
        self.assertIn("ORIG_PATH=unset", r.stdout)

    def test_resume_does_not_leak_the_dispatchers_pythonpath(self):
        # The dispatcher injects its own directory into PYTHONPATH for the
        # session manager; claude must not inherit that.
        r = self.run_wrapper(["sessions", "resume", "abc123"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ARGS=--resume abc123", r.stdout)
        self.assertIn("PYTHONPATH=unset", r.stdout)
        self.assertIn("PYTHONHOME=unset", r.stdout)


if __name__ == "__main__":
    unittest.main()
