#!/usr/bin/env bash
set -euo pipefail

# ── Claude UI Uninstaller ────────────────────────────────────────────

INSTALL_DIR="${INSTALL_DIR:-${CLAUDE_UI_HOME:-$HOME/.claude-ui}}"
CLAUDE_DIR="$HOME/.claude"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
COMMANDS_DIR="$CLAUDE_DIR/commands"
BIN_DIR="$HOME/.local/bin"

# Same override as install.sh; only used to rewrite settings.json here, so fall
# back to system python3 if the pinned interpreter is already gone.
PYTHON_BIN="${CLAUDETUI_PYTHON:-python3}"
command -v "$PYTHON_BIN" &>/dev/null || PYTHON_BIN=python3

RED='\033[91m'
GREEN='\033[92m'
YELLOW='\033[93m'
CYAN='\033[96m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'
LOGO_GREEN='\033[38;5;46m'

echo ""
echo -e "  ${BOLD} ██████╗ ██╗      █████╗ ██╗   ██╗██████╗ ███████╗${LOGO_GREEN}████████╗██╗   ██╗██╗${RESET}"
echo -e "  ${BOLD}██╔════╝ ██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝${LOGO_GREEN}╚══██╔══╝██║   ██║██║${RESET}"
echo -e "  ${BOLD}██║      ██║     ███████║██║   ██║██║  ██║█████╗  ${LOGO_GREEN}   ██║   ██║   ██║██║${RESET}"
echo -e "  ${BOLD}██║      ██║     ██╔══██║██║   ██║██║  ██║██╔══╝  ${LOGO_GREEN}   ██║   ██║   ██║██║${RESET}"
echo -e "  ${BOLD}╚██████╗ ███████╗██║  ██║╚██████╔╝██████╔╝███████╗${LOGO_GREEN}   ██║   ╚██████╔╝██║${RESET}"
echo -e "  ${BOLD} ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝${LOGO_GREEN}   ╚═╝    ╚═════╝ ╚═╝${RESET}"
echo -e "    ${DIM}Uninstaller${RESET}"
echo ""

# Remove CLI wrappers
for cmd in claudetui claude-ui-monitor claude-stats claude-sessions claude-ui-mode claude-ui-setup claude-ui-uninstall; do
    if [ -f "$BIN_DIR/$cmd" ]; then
        rm "$BIN_DIR/$cmd"
        echo -e "  ${GREEN}✓${RESET} Removed $BIN_DIR/$cmd"
    fi
done

# Remove commands symlink (check both old and new names)
for cmd_dir in tui ui; do
    if [ -L "$COMMANDS_DIR/$cmd_dir" ]; then
        rm "$COMMANDS_DIR/$cmd_dir"
        echo -e "  ${GREEN}✓${RESET} Removed slash commands ($cmd_dir)"
    fi
done

# Clean settings.json
if [ -f "$SETTINGS_FILE" ]; then
    "$PYTHON_BIN" << 'PYEOF'
import json
import os

settings_file = os.path.expanduser("~/.claude/settings.json")
install_dir = os.environ.get("INSTALL_DIR", os.path.expanduser("~/.claude-ui"))

# Match against all known install locations
match_paths = {install_dir, os.path.realpath(install_dir)}
# Also match brew paths and common install locations
for p in ["/opt/homebrew/opt/claude-tui", "/opt/homebrew/Cellar/claude-tui",
          "/usr/local/opt/claude-tui", "/usr/local/Cellar/claude-tui",
          "/opt/homebrew/opt/claudeui", "/opt/homebrew/Cellar/claudeui",
          "/usr/local/opt/claudeui", "/usr/local/Cellar/claudeui",
          os.path.expanduser("~/.claude-ui")]:
    match_paths.add(p)

try:
    with open(settings_file) as f:
        settings = json.load(f)
except (json.JSONDecodeError, ValueError) as e:
    print(f"  \033[93m⚠\033[0m  settings.json is corrupted ({e}), skipping cleanup")
    import sys; sys.exit(0)

def is_our_command(cmd):
    # Match new PATH-based commands (claudetui statusline/hook)
    if cmd.startswith("claudetui "):
        return True
    # Match old absolute-path commands (pre-v0.3.3)
    return any(p in cmd for p in match_paths)

# Remove statusline if it points to our install
sl = settings.get("statusLine", {})
if is_our_command(sl.get("command", "")):
    del settings["statusLine"]
    print(f"  \033[92m✓\033[0m Removed statusline config")

# Remove our hooks
hooks = settings.get("hooks", {})
for event in list(hooks.keys()):
    hooks[event] = [
        rule for rule in hooks[event]
        if not any(
            is_our_command(h.get("command", ""))
            for h in rule.get("hooks", [])
        )
    ]
    if not hooks[event]:
        del hooks[event]

if hooks:
    settings["hooks"] = hooks
elif "hooks" in settings:
    del settings["hooks"]

print(f"  \033[92m✓\033[0m Cleaned hooks config")

tmp = settings_file + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
os.replace(tmp, settings_file)
PYEOF
fi

# Remove install directory (only if it's our clone, not a symlink to user's repo)
if [ -d "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ]; then
    echo ""
    echo -e "  ${YELLOW}Remove $INSTALL_DIR? (y/N)${RESET}"
    read -r answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        rm -rf "$INSTALL_DIR"
        echo -e "  ${GREEN}✓${RESET} Removed $INSTALL_DIR"
    else
        echo -e "  ${DIM}Kept $INSTALL_DIR${RESET}"
    fi
elif [ -L "$INSTALL_DIR" ]; then
    rm "$INSTALL_DIR"
    echo -e "  ${GREEN}✓${RESET} Removed symlink $INSTALL_DIR"
fi

echo ""
echo -e "  ${GREEN}${BOLD}Uninstalled.${RESET} Restart Claude Code for changes to take effect."
echo ""
