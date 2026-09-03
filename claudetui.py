#!/usr/bin/env python3
"""ClaudeTUI — unified CLI for Claude Code utilities."""

import os
import subprocess
import sys
import urllib.parse

_FALLBACK_VERSION = "0.8.7"


def _get_version():
    """Get version from git tag (clone/dev), fall back to hardcoded (curl/brew)."""
    try:
        v = (
            subprocess.check_output(
                ["git", "describe", "--tags", "--abbrev=0"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
            .lstrip("v")
        )
        if v:
            return v
    except Exception:
        pass
    return _FALLBACK_VERSION


VERSION = _get_version()
_RAW_DIR = os.path.dirname(os.path.abspath(__file__))


def _stable_dir(d):
    """Convert Homebrew Cellar path to stable opt symlink.

    /opt/homebrew/Cellar/claude-tui/0.3.2/libexec
    → /opt/homebrew/opt/claude-tui/libexec

    The opt path is a symlink Homebrew maintains across upgrades,
    so settings.json paths survive 'brew upgrade'.
    """
    if "/Cellar/" not in d:
        return d
    prefix, rest = d.split("/Cellar/", 1)
    parts = rest.split("/")
    if len(parts) >= 3:
        formula = parts[0]
        after_version = "/".join(parts[2:])
        return f"{prefix}/opt/{formula}/{after_version}"
    return d


SCRIPT_DIR = _stable_dir(_RAW_DIR)

SUBCOMMANDS = {
    "monitor": ("python", "claude-code-monitor/monitor.py", "Live session dashboard"),
    "chart": (
        "python",
        "claude-code-monitor/monitor.py",
        "Context efficiency chart",
        ["--chart"],
    ),
    "stats": (
        "python",
        "claude-code-session-stats/session-stats.py",
        "Post-session analytics",
    ),
    "sessions": (
        "python",
        "claude-code-session-manager/session-manager.py",
        "Browse, compare, export sessions",
    ),
    "mode": (
        "python",
        "claude-ui-mode.py",
        "Switch statusline mode (full/compact/custom)",
    ),
    "statusline": (
        "python",
        "claude-code-statusline/statusline.py",
        "Run statusline (used by Claude Code)",
    ),
    "sniffer": (
        "python",
        "claude-code-sniffer/sniffer.py",
        "API call interceptor proxy",
    ),
    "setup": ("bash", "install.sh", "Configure statusline, hooks, and commands"),
    "uninstall": ("bash", "uninstall.sh", "Remove ClaudeTUI configuration"),
}

HOOKS = {
    "session-heatmap": "claude-code-hooks/session-heatmap.py",
    "pre-edit-churn": "claude-code-hooks/pre-edit-churn.py",
    "post-edit-deps": "claude-code-hooks/post-edit-deps.py",
}

HELP = """\
claudetui — CLI for ClaudeTUI (Claude Code utilities)

Usage:
  claudetui <command> [args...]

Commands:
  monitor     Live session dashboard (separate terminal)
  chart       Context efficiency chart (token waste per segment)
  sniffer     API call interceptor (capture raw requests/responses)
  sniff       Launch claude through sniffer proxy (auto-detects port)
  stats       Post-session analytics
  sessions    Browse, compare, resume, export sessions
  mode        Switch statusline mode (full/compact/custom)
  setup       Configure statusline, hooks, and commands
  uninstall   Remove ClaudeTUI configuration

Options:
  -h, --help     Show this help
  -v, --version  Show version

Examples:
  claudetui monitor              # live dashboard
  claudetui chart                # efficiency chart for current session
  claudetui sniffer              # intercept API calls (start proxy)
  claudetui sniff                # launch claude through sniffer
  claudetui sniff --resume abc   # resume session through sniffer
  claudetui stats --days 7 -s    # weekly summary
  claudetui sessions list        # browse sessions
  claudetui mode compact         # switch to 1-line statusline
  claudetui mode custom          # interactive configurator\
"""


def _sniffer_ports(port_dir):
    """Ports of the sniffers currently running, from their .port.{PORT} files."""
    try:
        return {f.split(".", 2)[2] for f in os.listdir(port_dir)
                if f.startswith(".port.")}
    except FileNotFoundError:
        return set()


def _is_sniffer_url(url, port_dir):
    """True when a base URL points at a sniffer running on this machine.

    Checked against the port files rather than the hostname alone — a local
    gateway (LiteLLM on localhost:4000) is not a sniffer.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return False
    is_local = host in ("localhost", "0.0.0.0", "::", "::1") or host.startswith("127.")
    return is_local and port is not None and str(port) in _sniffer_ports(port_dir)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(HELP)
        sys.exit(0)

    if sys.argv[1] in ("-v", "--version"):
        print(f"claudetui {VERSION}")
        sys.exit(0)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    # Hook dispatch: claudetui hook <name> [args...]
    if cmd == "hook":
        if not args or args[0] in ("-h", "--help"):
            print("Usage: claudetui hook <name>\n")
            print("Hooks:")
            for name in sorted(HOOKS):
                print(f"  {name}")
            sys.exit(0)
        hook_name = args[0]
        if hook_name not in HOOKS:
            print(f"claudetui: unknown hook '{hook_name}'", file=sys.stderr)
            print(f"Available: {', '.join(sorted(HOOKS))}", file=sys.stderr)
            sys.exit(2)
        target_path = os.path.join(SCRIPT_DIR, HOOKS[hook_name])
        if not os.path.exists(target_path):
            print(f"claudetui: hook script not found at {target_path}", file=sys.stderr)
            sys.exit(1)
        # ── Dependency Injection ──────────────────────────────────────
        # Inject our script directory into PYTHONPATH so sub-tools can find
        # the shared libraries: claude_tui_components and claude_tui_core.
        env = os.environ.copy()
        curr_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{SCRIPT_DIR}:{curr_pp}" if curr_pp else SCRIPT_DIR
        os.execvpe(sys.executable, [sys.executable, target_path] + args[1:], env)

    # Sniff dispatch: claudetui sniff [--port PORT] [claude args...]
    if cmd == "sniff":
        sniff_port = None
        claude_args = args

        # Parse --port from sniff args
        if len(args) >= 2 and args[0] == "--port":
            sniff_port = args[1]
            claude_args = args[2:]

        port_dir = os.path.join(os.path.expanduser("~"), ".claude", "api-sniffer")

        if sniff_port:
            # User specified a port — verify sniffer is running
            port_file = os.path.join(port_dir, f".port.{sniff_port}")
            if not os.path.exists(port_file):
                print(
                    f"  Sniffer not found on port {sniff_port} — starting claude without proxy"
                )
                sniff_port = None
        else:
            # Auto-detect: find any running sniffer
            ports = sorted(_sniffer_ports(port_dir))

            if len(ports) == 1:
                sniff_port = ports[0]
            elif len(ports) > 1:
                print(f"  Multiple sniffers running: {', '.join(ports)}")
                print(f"  Use: claudetui sniff --port <port> [claude args...]")
                sys.exit(1)
            else:
                print("  Sniffer not running — starting claude without proxy")

        if sniff_port:
            # A pre-existing base URL is about to be replaced by the proxy's.
            # The sniffer only forwards there if it was started with it visible.
            prev_base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
            if prev_base and not _is_sniffer_url(prev_base, port_dir):
                print(f"  Note: ANTHROPIC_BASE_URL={prev_base} is being replaced")
                print("        If the sniffer was not started with that URL in its")
                print("        environment, restart it with:")
                print(f"        claudetui sniffer --port {sniff_port} --upstream {prev_base}")
            os.environ["ANTHROPIC_BASE_URL"] = f"http://localhost:{sniff_port}"
            print(f"  Routing through sniffer on port {sniff_port}")
        # execvp replaces this process — anything still buffered would be lost
        sys.stdout.flush()
        os.execvp("claude", ["claude"] + claude_args)

    if cmd not in SUBCOMMANDS:
        print(f"claudetui: unknown command '{cmd}'\n", file=sys.stderr)
        print(HELP, file=sys.stderr)
        sys.exit(2)

    entry = SUBCOMMANDS[cmd]
    kind, target = entry[0], entry[1]
    prefix_args = entry[3] if len(entry) > 3 else []
    target_path = os.path.join(SCRIPT_DIR, target)

    if not os.path.exists(target_path):
        print(f"claudetui: {target} not found at {target_path}", file=sys.stderr)
        print("Run 'claudetui setup' to install.", file=sys.stderr)
        sys.exit(1)

    if kind == "bash":
        os.environ["INSTALL_DIR"] = SCRIPT_DIR
        os.execvp("bash", ["bash", target_path] + prefix_args + args)
    else:
        env = os.environ.copy()
        if "PYTHONPATH" in env:
            env["PYTHONPATH"] = f"{SCRIPT_DIR}:{env['PYTHONPATH']}"
        else:
            env["PYTHONPATH"] = SCRIPT_DIR
        os.execvpe(
            sys.executable, [sys.executable, target_path] + prefix_args + args, env
        )


if __name__ == "__main__":
    main()
