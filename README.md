<p align="center">
  <img src="assets/logo.svg" alt="ClaudeTUI" width="720">
</p>

[![Release](https://img.shields.io/github/v/release/slima4/claude-tui)](https://github.com/slima4/claude-tui/releases)
[![Stars](https://img.shields.io/github/stars/slima4/claude-tui)](https://github.com/slima4/claude-tui/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/slima4/claude-tui)](https://github.com/slima4/claude-tui/commits/main)
[![License](https://img.shields.io/github/license/slima4/claude-tui)](https://github.com/slima4/claude-tui/blob/main/LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue?logo=python&logoColor=white)]()
[![Shell](https://img.shields.io/badge/shell-bash%20%7C%20zsh-black?logo=gnubash&logoColor=white)]()

A real-time **statusline** for Claude Code — context, cost, usage bars, sparkline, and live tool trace, right inside your session.

**Website:** [slima4.github.io/claude-tui](https://slima4.github.io/claude-tui/)

---

## Statusline

**Compact mode** — everything in one line:

![Statusline Compact](assets/statusline-compact.png)

```bash
claudetui mode compact
```

**Full mode** — three lines with context, session/weekly usage bars, sparkline, and live tool trace:

![Statusline](assets/statusline-demo.png)

```bash
claudetui mode full
```

See the [statusline README](./claude-code-statusline/README.md) for the full feature list, customization (`claudetui mode custom`), widgets, color thresholds, and debugging flags.

---

## Install

### macOS (Homebrew)

```bash
brew tap slima4/claude-tui
brew install claude-tui
claudetui setup       # configure statusline, hooks, and commands
```

### macOS / Linux (script)

```bash
curl -sSL https://raw.githubusercontent.com/slima4/claude-tui/main/install.sh | bash
```

Or clone and install locally:

```bash
git clone https://github.com/slima4/claude-tui.git && ./claude-tui/install.sh
```

### Windows (WSL)

ClaudeTUI requires a Unix-like environment. On Windows, use [WSL 2](https://learn.microsoft.com/en-us/windows/wsl/install):

```bash
curl -sSL https://raw.githubusercontent.com/slima4/claude-tui/main/install.sh | bash
```

### Install options

Both `install.sh` and `uninstall.sh` honor a few environment variables:

```bash
CLAUDETUI_PYTHON="$(uv python find 3.13)" ./install.sh   # pin an isolated Python (e.g. uv-managed 3.13); baked into the claudetui wrapper
INSTALL_DIR="$HOME/tools/claude-tui" ./install.sh        # install somewhere other than ~/.claude-ui
CLAUDE_UI_HOME="$HOME/tools/claude-tui" ./install.sh      # alias for INSTALL_DIR
```

### Uninstall

```bash
claudetui uninstall
brew uninstall claude-tui
```

If you already ran `brew uninstall` first:

```bash
curl -sSL https://raw.githubusercontent.com/slima4/claude-tui/main/uninstall.sh | bash
```

---

## More tools

ClaudeTUI ships a few companion utilities alongside the statusline. Each lives in its own directory with its own README:

| Tool | What it does |
|------|--------------|
| [claude-code-statusline](./claude-code-statusline/) | The statusline itself — full docs, widgets, color thresholds |
| [claude-code-monitor](./claude-code-monitor/) | Live session dashboard for a second terminal — `claudetui monitor` |
| [claude-code-sniffer](./claude-code-sniffer/) | API call interceptor proxy — `claudetui sniffer` / `claudetui sniff` |
| [claude-code-session-stats](./claude-code-session-stats/) | Post-session analytics — `claudetui stats` |
| [claude-code-session-manager](./claude-code-session-manager/) | Browse, compare, resume, and export sessions — `claudetui sessions list` |
| [claude-code-hooks](./claude-code-hooks/) | Hooks for automatic in-session context: hotspots, reverse deps, churn |
| [claude-code-commands](./claude-code-commands/) | Custom slash commands: `/tui:session`, `/tui:cost`, `/tui:perf`, `/tui:context` |
| [claude_tui_core](./claude_tui_core/) | Shared domain logic — models, pricing, network, settings |
| [claude_tui_components](./claude_tui_components/) | Shared UI library — progress bars, sparklines, widgets, colors |

## License

MIT
