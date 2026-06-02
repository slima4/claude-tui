#!/usr/bin/env bash
set -euo pipefail

# ── Claude UI Installer ─────────────────────────────────────────────
# Installs the full claude-tui suite for Claude Code:
#   • Statusline — real-time status bar
#   • Hooks — file hotspots, dependency warnings, churn alerts
#   • Commands — /tui:session, /tui:cost, /tui:perf, /tui:context
#   • Monitor — live dashboard (standalone terminal tool)
#   • Session Stats — post-session analytics
#   • Session Manager — browse, compare, export sessions
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/slima4/claude-tui/main/install.sh | bash
#   # or
#   git clone https://github.com/slima4/claude-tui.git && ./claude-tui/install.sh

REPO_URL="https://github.com/slima4/claude-tui.git"
INSTALL_DIR="${INSTALL_DIR:-${CLAUDE_UI_HOME:-$HOME/.claude-ui}}"
CLAUDE_DIR="$HOME/.claude"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"
COMMANDS_DIR="$CLAUDE_DIR/commands"
BIN_DIR="$HOME/.local/bin"

# Colors
RED='\033[91m'
GREEN='\033[92m'
YELLOW='\033[93m'
CYAN='\033[96m'
MAGENTA='\033[95m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'
LOGO_GREEN='\033[38;5;46m'

print_header() {
    echo ""
    echo -e "  ${BOLD} ██████╗ ██╗      █████╗ ██╗   ██╗██████╗ ███████╗${LOGO_GREEN}████████╗██╗   ██╗██╗${RESET}"
    echo -e "  ${BOLD}██╔════╝ ██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝${LOGO_GREEN}╚══██╔══╝██║   ██║██║${RESET}"
    echo -e "  ${BOLD}██║      ██║     ███████║██║   ██║██║  ██║█████╗  ${LOGO_GREEN}   ██║   ██║   ██║██║${RESET}"
    echo -e "  ${BOLD}██║      ██║     ██╔══██║██║   ██║██║  ██║██╔══╝  ${LOGO_GREEN}   ██║   ██║   ██║██║${RESET}"
    echo -e "  ${BOLD}╚██████╗ ███████╗██║  ██║╚██████╔╝██████╔╝███████╗${LOGO_GREEN}   ██║   ╚██████╔╝██║${RESET}"
    echo -e "  ${BOLD} ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝${LOGO_GREEN}   ╚═╝    ╚═════╝ ╚═╝${RESET}"
    echo -e "    ${DIM}Installer${RESET}"
    echo ""
}

step() {
    echo -e "  ${CYAN}▶${RESET} $1"
}

ok() {
    echo -e "  ${GREEN}✓${RESET} $1"
}

warn() {
    echo -e "  ${YELLOW}!${RESET} $1"
}

fail() {
    echo -e "  ${RED}✗${RESET} $1"
    exit 1
}

# ── Preflight checks ────────────────────────────────────────────────

print_header

step "Checking requirements..."

# Python 3.13+
if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install Python 3.13+ first."
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 13 ]; }; then
    fail "Python 3.13+ required, found $PY_VERSION"
fi
ok "Python $PY_VERSION"

# Git
if ! command -v git &>/dev/null; then
    fail "git not found. Install git first."
fi
ok "git $(git --version | awk '{print $3}')"

# Claude Code directory
if [ ! -d "$CLAUDE_DIR" ]; then
    fail "$CLAUDE_DIR not found. Install Claude Code first: https://claude.ai/code"
fi
ok "Claude Code detected"

# ── Migrate from old ClaudeUI name ─────────────────────────────────

# Remove old Homebrew tap if present
if command -v brew &>/dev/null; then
    if brew tap 2>/dev/null | grep -q 'slima4/claudeui'; then
        echo ""
        step "Migrating from old ClaudeUI install..."
        brew untap slima4/claudeui 2>/dev/null && ok "Removed old tap slima4/claudeui"
        if brew list claudeui &>/dev/null; then
            brew uninstall claudeui 2>/dev/null && ok "Uninstalled old brew formula"
        fi
    fi
fi

# Update git remote if pointing to old repo name
if [ -d "$INSTALL_DIR/.git" ]; then
    OLD_REMOTE=$(cd "$INSTALL_DIR" && git remote get-url origin 2>/dev/null || echo "")
    if echo "$OLD_REMOTE" | grep -q 'claudeui'; then
        echo ""
        step "Updating repository URL..."
        (cd "$INSTALL_DIR" && git remote set-url origin "$REPO_URL")
        ok "Remote updated to $REPO_URL"
    fi
fi

# ── Download / Update ────────────────────────────────────────────────

echo ""
if [ -d "$INSTALL_DIR/.git" ]; then
    step "Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull --quiet origin main 2>/dev/null || warn "Could not pull latest (offline?)"
    ok "Updated $INSTALL_DIR"
else
    # Check if we're running from inside the repo already
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$SCRIPT_DIR/claude-code-statusline/statusline.py" ]; then
        # Running from repo — symlink instead of clone
        step "Linking from local repo..."
        if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
            ln -sfn "$SCRIPT_DIR" "$INSTALL_DIR"
            ok "Linked $INSTALL_DIR → $SCRIPT_DIR"
        else
            ok "Already at $INSTALL_DIR"
        fi
    else
        step "Cloning repository..."
        git clone --quiet "$REPO_URL" "$INSTALL_DIR"
        ok "Cloned to $INSTALL_DIR"
    fi
fi

# ── Install slash commands ───────────────────────────────────────────

echo ""
step "Installing slash commands..."

mkdir -p "$COMMANDS_DIR"

# Remove old /ui: symlink if present (renamed to /tui:)
if [ -L "$COMMANDS_DIR/ui" ]; then
    rm "$COMMANDS_DIR/ui"
fi

# Symlink the tui commands directory
if [ -L "$COMMANDS_DIR/tui" ]; then
    rm "$COMMANDS_DIR/tui"
fi
if [ -d "$COMMANDS_DIR/tui" ]; then
    warn "Existing $COMMANDS_DIR/tui/ directory found — backing up"
    mv "$COMMANDS_DIR/tui" "$COMMANDS_DIR/tui.backup.$(date +%s)"
fi
ln -sfn "$INSTALL_DIR/claude-code-commands/tui" "$COMMANDS_DIR/tui"
ok "/tui:session, /tui:cost, /tui:perf, /tui:context"

# ── Choose statusline mode ──────────────────────────────────────────

echo ""
echo -e "  ${BOLD}Statusline mode:${RESET}"
echo ""
echo -e "  ${CYAN}1)${RESET} ${BOLD}full${RESET}     3-line statusline with all metrics, sparkline, tool trace"
echo -e "  ${CYAN}2)${RESET} ${BOLD}compact${RESET}  1-line statusline with essentials (model, context, cost)"
echo -e "  ${CYAN}3)${RESET} ${BOLD}custom${RESET}   Choose which components to show (configure after install)"
echo ""
while true; do
    echo -ne "  Choose [${BOLD}1${RESET}/${BOLD}2${RESET}/${BOLD}3${RESET}] (default: 1): "
    read -rn1 mode_choice < /dev/tty || mode_choice=""
    echo ""
    case "${mode_choice}" in
        1|"") STATUSLINE_MODE="full";    DISPLAY_MODE="full";    break ;;
        2)    STATUSLINE_MODE="compact"; DISPLAY_MODE="compact"; break ;;
        3)    STATUSLINE_MODE="full";    DISPLAY_MODE="custom";  break ;;
        *)    echo -e "  ${RED}✗${RESET} Please enter 1, 2, or 3" ;;
    esac
done
ok "Statusline mode: $DISPLAY_MODE"
LAUNCH_CUSTOM=false
if [ "$DISPLAY_MODE" = "custom" ]; then
    LAUNCH_CUSTOM=true
fi
export STATUSLINE_MODE

# ── Configure settings.json ─────────────────────────────────────────

echo ""
step "Configuring Claude Code settings..."

# Use Python to safely merge into settings.json
python3 << 'PYEOF'
import json
import os
import sys
from pathlib import Path

settings_file = os.path.expanduser("~/.claude/settings.json")
install_dir = os.environ.get("INSTALL_DIR", os.path.expanduser("~/.claude-ui"))

# Load existing settings
settings = {}
if os.path.exists(settings_file):
    try:
        with open(settings_file) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, IOError):
        # Backup corrupted file
        backup = settings_file + ".backup"
        if os.path.exists(settings_file):
            os.rename(settings_file, backup)
            print(f"  \033[93m!\033[0m Backed up corrupted settings to {backup}")

# ── Statusline ──
# Use 'claudetui' from PATH — no absolute paths, survives brew upgrade
mode = os.environ.get("STATUSLINE_MODE", "full")
statusline_cmd = "claudetui statusline"
if mode == "compact":
    statusline_cmd += " --compact"
# Merge into any existing statusLine config so user-set keys (padding,
# hideVimModeIndicator, a custom refreshInterval) survive reinstalls. Only the
# keys we own get rewritten. refreshInterval re-runs the command on a timer so
# duration, rate-limit reset countdowns, and git state stay fresh while the
# session is idle (e.g. waiting on background subagents).
sl = settings.get("statusLine")
if not isinstance(sl, dict):
    sl = {}
before = dict(sl)
sl["type"] = "command"
sl["command"] = statusline_cmd
sl.setdefault("refreshInterval", 10)
if sl != before:
    settings["statusLine"] = sl
    print(f"  \033[92m✓\033[0m Statusline configured ({mode})")
else:
    print(f"  \033[92m✓\033[0m Statusline already configured ({mode})")

# ── Hooks ──
# Use 'claudetui hook <name>' — no absolute paths, survives brew upgrade
hooks = settings.get("hooks", {})

hook_configs = [
    {
        "event": "SessionStart",
        "matcher": "",
        "command": "claudetui hook session-heatmap",
        "label": "SessionStart → file hotspots",
    },
    {
        "event": "PreToolUse",
        "matcher": "Edit|Write",
        "command": "claudetui hook pre-edit-churn",
        "label": "PreToolUse → churn warnings",
    },
    {
        "event": "PostToolUse",
        "matcher": "Edit|Write",
        "command": "claudetui hook post-edit-deps",
        "label": "PostToolUse → dependency check",
    },
]

# Remove old hooks that used absolute paths (from pre-v0.3.3 installs)
for event in list(hooks.keys()):
    hooks[event] = [
        rule for rule in hooks[event]
        if not any(
            "claude-code-hooks/" in h.get("command", "")
            and "claudetui hook" not in h.get("command", "")
            for h in rule.get("hooks", [])
        )
    ]
    if not hooks[event]:
        del hooks[event]

for cfg in hook_configs:
    event = cfg["event"]
    if event not in hooks:
        hooks[event] = []

    # Check if hook already exists
    already_exists = False
    for rule in hooks[event]:
        for h in rule.get("hooks", []):
            if h.get("command") == cfg["command"]:
                already_exists = True
                break
        if already_exists:
            break

    if not already_exists:
        hooks[event].append({
            "matcher": cfg["matcher"],
            "hooks": [{"type": "command", "command": cfg["command"]}],
        })
        print(f"  \033[92m✓\033[0m {cfg['label']}")
    else:
        print(f"  \033[92m✓\033[0m {cfg['label']} (already configured)")

settings["hooks"] = hooks

# Write settings (atomic: write to tmp, then rename)
Path(settings_file).parent.mkdir(parents=True, exist_ok=True)
tmp = settings_file + ".tmp"
with open(tmp, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
os.replace(tmp, settings_file)
PYEOF

# ── Install CLI commands ─────────────────────────────────────────────

echo ""
step "Installing CLI tools..."

mkdir -p "$BIN_DIR"

# Primary CLI command
cat > "$BIN_DIR/claudetui" << EOF
#!/usr/bin/env bash
exec python3 "$INSTALL_DIR/claudetui.py" "\$@"
EOF
chmod +x "$BIN_DIR/claudetui"
ok "claudetui"

# Remove old command names from previous installs
for old_cmd in claude-ui-monitor claude-stats claude-sessions claude-ui-mode claude-ui-setup claude-ui-uninstall; do
    if [ -f "$BIN_DIR/$old_cmd" ]; then
        rm "$BIN_DIR/$old_cmd"
        ok "removed old $old_cmd"
    fi
done

# Check if BIN_DIR is in PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    warn "$BIN_DIR is not in your PATH"
    echo ""
    echo -e "  Add to your shell profile:"
    echo -e "  ${DIM}echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc${RESET}"
fi

# ── Summary ──────────────────────────────────────────────────────────

echo ""
echo -e "  ${GREEN}${BOLD}✓ Installation Complete!${RESET}"
echo ""
echo -e "  ${BOLD}What's installed:${RESET}"
echo ""
echo -e "  ${CYAN}Statusline${RESET}      Real-time status bar in Claude Code (${DISPLAY_MODE} mode)"
echo -e "  ${CYAN}Hooks${RESET}           File hotspots, dependency warnings, churn alerts"
echo -e "  ${CYAN}Commands${RESET}        /tui:session  /tui:cost  /tui:perf  /tui:context"
echo -e "  ${CYAN}Monitor${RESET}         claudetui monitor (live dashboard in separate terminal)"
echo -e "  ${CYAN}Chart${RESET}           claudetui chart (context efficiency visualization)"
echo -e "  ${CYAN}Stats${RESET}           claudetui stats (post-session analytics)"
echo -e "  ${CYAN}Sessions${RESET}        claudetui sessions (browse, compare, export)"
echo ""
echo -e "  ${BOLD}Quick start:${RESET}"
echo ""
echo -e "  ${DIM}# Start Claude Code — statusline and hooks are automatic${RESET}"
echo -e "  claude"
echo ""
echo -e "  ${DIM}# Open a second terminal for the live monitor${RESET}"
echo -e "  claudetui monitor"
echo -e "  claudetui chart     ${DIM}# context efficiency chart${RESET}"
echo ""
echo -e "  ${DIM}# Inside Claude Code, use slash commands${RESET}"
echo -e "  /tui:session    ${DIM}# full session report${RESET}"
echo -e "  /tui:cost       ${DIM}# cost deep dive${RESET}"
echo ""
echo -e "  ${DIM}# Post-session analytics${RESET}"
echo -e "  claudetui stats"
echo -e "  claudetui sessions list"
echo -e "  claudetui mode compact  ${DIM}# switch to 1-line statusline${RESET}"
echo ""
echo -e "  ${DIM}Installed to: $INSTALL_DIR${RESET}"
if [[ "$INSTALL_DIR" == */opt/claude-tui/* || "$INSTALL_DIR" == */Cellar/claude-tui/* ]]; then
    echo -e "  ${DIM}To update:    brew upgrade claude-tui${RESET}"
    echo -e "  ${DIM}To uninstall: claudetui uninstall && brew uninstall claude-tui${RESET}"
else
    echo -e "  ${DIM}To update:    cd $INSTALL_DIR && git pull${RESET}"
    echo -e "  ${DIM}To uninstall: claudetui uninstall${RESET}"
fi
echo ""

# Launch custom configurator if user chose option 3
if [ "$LAUNCH_CUSTOM" = "true" ]; then
    echo -e "  ${BOLD}Opening statusline configurator...${RESET}"
    echo ""
    python3 "$INSTALL_DIR/claude-ui-mode.py" custom
fi
