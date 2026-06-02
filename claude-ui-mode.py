#!/usr/bin/env python3
"""ClaudeTUI — statusline mode switcher and component configurator."""

import json
import os
import sys

from claude_tui_components import (
    build_progress_bar, build_sparkline,
    RESET, BOLD, DIM, GREEN, RED, CYAN, YELLOW, MAGENTA, LOGO_GREEN, GRAY, WHITE,
    CLEAR, HIDE_CURSOR, SHOW_CURSOR, ALT_SCREEN_ON, ALT_SCREEN_OFF
)
import select
import termios
import tty


SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".claude", "claudeui.json")

# ANSI colors
GREEN = "\033[92m"
RED = "\033[31m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


# ── Help text ──────────────────────────────────────────────────────


MAIN_HELP = f"""\
{BOLD}claudetui mode{RESET} — statusline mode switcher for ClaudeTUI

{BOLD}Usage:{RESET}
  claudetui mode {CYAN}<command>{RESET} [options]

{BOLD}Commands:{RESET}
  {CYAN}full{RESET}           Switch to 3-line statusline (all metrics)
  {CYAN}compact{RESET}        Switch to 1-line statusline (essentials)
  {CYAN}custom{RESET}         Configure which components to show

{BOLD}Options:{RESET}
  {CYAN}-h{RESET}, {CYAN}--help{RESET}     Show this help message

{BOLD}Examples:{RESET}
  claudetui mode                  {DIM}# show current mode{RESET}
  claudetui mode full             {DIM}# 3-line with everything{RESET}
  claudetui mode compact          {DIM}# 1-line essentials{RESET}
  claudetui mode custom           {DIM}# interactive configurator{RESET}
  claudetui mode custom -l        {DIM}# show what's hidden{RESET}

{DIM}Config: ~/.claude/claudeui.json{RESET}
{DIM}Docs:   https://github.com/slima4/claude-tui{RESET}
"""

CUSTOM_HELP = f"""\
{BOLD}claudetui mode custom{RESET} — configure statusline components

{BOLD}Usage:{RESET}
  claudetui mode custom [options]

  Run without options to open the interactive configurator.
  Use arrow keys to navigate, space to toggle, s to save.

{BOLD}Options:{RESET}
  {CYAN}-l{RESET}, {CYAN}--list{RESET}               Show current configuration
  {CYAN}-w{RESET}, {CYAN}--widget{RESET} {DIM}<name>{RESET}       Set widget (matrix, hex, bars, progress, none)
  {CYAN}-b{RESET}, {CYAN}--buffer{RESET} {DIM}<chars>{RESET}      Right edge buffer in chars (0-60, default: 30)
  {CYAN}-p{RESET}, {CYAN}--preset{RESET} {DIM}<name>{RESET}       Apply preset (all, minimal, focused)
      {CYAN}--hide{RESET} {DIM}<components>{RESET}   Hide components (comma-separated)
      {CYAN}--show{RESET} {DIM}<components>{RESET}   Show components (comma-separated)
  {CYAN}-h{RESET}, {CYAN}--help{RESET}               Show this help message

{BOLD}Components:{RESET}
  {DIM}Line 1:{RESET} model, context_bar, token_count, compact_prediction,
          sparkline, cost, duration, compact_count, efficiency, session_id
  {DIM}Line 2:{RESET} usage, cwd, git_branch, turns, files, errors, cache,
          thinking, cost_per_turn, agents, api_status
  {DIM}Line 3:{RESET} usage_weekly, tool_trace, file_edits

{BOLD}Presets:{RESET}
  {CYAN}all{RESET}        Show all components
  {CYAN}minimal{RESET}    Only essentials (context bar, duration, turns, errors)
  {CYAN}focused{RESET}    Hide noise (model, tokens, cost, session ID, cwd, agents)

{BOLD}Examples:{RESET}
  claudetui mode custom                         {DIM}# interactive TUI{RESET}
  claudetui mode custom -l                      {DIM}# list hidden components{RESET}
  claudetui mode custom -p focused              {DIM}# apply focused preset{RESET}
  claudetui mode custom -w hex                  {DIM}# switch to hex widget{RESET}
  claudetui mode custom --hide model,cost       {DIM}# hide model and cost{RESET}
  claudetui mode custom --show model            {DIM}# show model again{RESET}
  claudetui mode custom -b 20                   {DIM}# set buffer to 20 chars{RESET}
  claudetui mode custom -p all -w none          {DIM}# reset all, no widget{RESET}
"""


# ── Settings (full/compact mode) ──────────────────────────────────


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_settings(settings):
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS_PATH)


def show_current():
    settings = load_settings()
    cmd = settings.get("statusLine", {}).get("command", "")
    if not cmd:
        print(
            f"  {RED}\u2717{RESET} No statusline configured. Run claudetui setup first."
        )
        return

    # Check mode flags
    if "--compact" in cmd:
        print(f"  Current mode: {BOLD}{CYAN}compact{RESET}")
    else:
        # Check if custom components configured
        cfg = load_config()
        custom = cfg.get("custom", {})
        has_custom = (
            any(custom.get("line1", {}).values())
            or any(custom.get("line2", {}).values())
            or any(custom.get("line3", {}).values())
        )

        if has_custom:
            print(f"  Current mode: {BOLD}{CYAN}custom{RESET}")
        else:
            print(f"  Current mode: {BOLD}{CYAN}full{RESET}")

    print()
    print(f"  {DIM}Run claudetui mode --help for usage info{RESET}")


def set_mode(mode):
    settings = load_settings()
    current_cmd = settings.get("statusLine", {}).get("command", "")
    if not current_cmd:
        print(
            f"  {RED}\u2717{RESET} No statusline configured. Run claudetui setup first."
        )
        sys.exit(1)

    base_cmd = current_cmd.replace(" --compact", "").strip()

    if mode == "compact":
        cmd = f"{base_cmd} --compact"
    elif mode == "full":
        cmd = base_cmd
    elif mode == "custom":
        # Custom mode is just full without --compact, but keeps custom config
        cmd = base_cmd
    else:
        print(f"  {RED}\u2717{RESET} Unknown mode: {mode}")
        sys.exit(1)

    sl = settings.get("statusLine", {})
    sl.update({"type": "command", "command": cmd})
    # Backfill refreshInterval for installs predating it, without clobbering a
    # value the user set themselves (keep in sync with install.sh default).
    sl.setdefault("refreshInterval", 10)
    settings["statusLine"] = sl
    save_settings(settings)
    print(f"  {GREEN}\u2713{RESET} Statusline mode: {BOLD}{CYAN}{mode}{RESET}")
    print(f"  {DIM}Restart Claude Code for changes to take effect.{RESET}")


# ── Config (custom components) ─────────────────────────────────────

COMPONENTS = [
    ("context_bar", "line1", "Context bar", lambda: build_progress_bar(0.42, 20, threshold=0.83, pct_label="C") + f" {CYAN}112k{RESET}{DIM}/{RESET}{GRAY}1.0M{RESET}", 1),
    ("model", "line1", "Model", lambda: f"{BOLD}{MAGENTA}Opus 4.6{RESET}", 5),
    ("token_count", "line1", "Token count", lambda: f"{YELLOW}⚡{RESET}{CYAN}65.5k{RESET}{DIM}/{RESET}{GRAY}200.0k{RESET}", 3),
    ("compact_prediction", "line1", "Compact predict", lambda: f"{DIM}ETA 24k{RESET}", 3),
    ("sparkline", "line1", "Sparkline", lambda: build_sparkline([1, 2, 8, 3, 4, None, 10, 5, 3, 2, 4], 20), 3),
    ("cost", "line1", "Cost", lambda: f"{YELLOW}$2.34{RESET}", 4),
    ("duration", "line1", "Duration", lambda: f"{WHITE}12m{RESET}", 0),
    ("compact_count", "line1", "Compact count", lambda: f"{CYAN}1{RESET}{DIM}x{RESET}", 3),
    ("efficiency", "line1", "Efficiency", lambda: f"{GREEN}92%{RESET}", 1),
    ("session_id", "line1", "Session ID", lambda: f"{DIM}#{RESET}{GRAY}a1b2c3d4{RESET}", 0),
    (
        "usage",
        "line2",
        "Plan % (session)",
        lambda: build_progress_bar(0.15, 20),
        1,
    ),
    ("cwd", "line2", "Directory", lambda: f"{GREEN}{os.path.basename(os.getcwd())}{RESET}", 0),
    ("git_branch", "line2", "Git branch", lambda: f"{GREEN}⎇ main{RESET} {GREEN}+42{RESET} {RED}-17{RESET}", 3),
    ("turns", "line2", "Turns", lambda: f"{GREEN}18{RESET} {DIM}turns{RESET}", 3),
    ("files", "line2", "Files", lambda: f"{CYAN}5{RESET} {DIM}files{RESET}", 3),
    ("errors", "line2", "Errors", lambda: f"{GREEN}0{RESET} {DIM}err{RESET}", 1),
    ("cache", "line2", "Cache %", lambda: f"{GREEN}82%{RESET} {DIM}cache{RESET}", 3),
    ("thinking", "line2", "Thinking", lambda: f"{GREEN}4{RESET} {DIM}think{RESET}", 5),
    ("cost_per_turn", "line2", "Cost/turn", lambda: f"{YELLOW}~$0.13/turn{RESET}", 4),
    ("agents", "line2", "Agents", lambda: f"{CYAN}2{RESET} {DIM}agents{RESET}", 3),
    ("api_status", "line2", "API status", lambda: f"{YELLOW}▲ degraded{RESET}", 4),
    (
        "usage_weekly",
        "line3",
        "Plan % (weekly)",
        lambda: build_progress_bar(0.73, 20),
        1,
    ),
    (
        "tool_trace",
        "line3",
        "Tool trace",
        lambda: f"{GRAY}read{RESET} {GREEN}utils.py{RESET} {GRAY}→{RESET} {GRAY}bash{RESET} {GREEN}ls{RESET}",
        0,
    ),
    ("file_edits", "line3", "File edits", lambda: f"{YELLOW}statusline.py{RESET}{GRAY}×3{RESET} {YELLOW}README.md{RESET}{GRAY}×1{RESET}", 0),
]

COMPONENT_IDS = {c[0] for c in COMPONENTS}

WIDGETS = ["matrix", "hex", "bars", "progress", "none"]

PRESETS = {
    "all": {},
    "minimal": {
        "line1": {
            "model": False,
            "token_count": False,
            "cost": False,
            "compact_prediction": False,
            "compact_count": False,
            "efficiency": False,
            "session_id": False,
        },
        "line2": {
            "cwd": False,
            "git_branch": False,
            "cache": False,
            "cost_per_turn": False,
            "agents": False,
            "thinking": False,
            "files": False,
            "api_status": False,
            "usage": False,
        },
        "line3": {"file_edits": False, "usage_weekly": False},
    },
    "focused": {
        "line1": {
            "model": False,
            "token_count": False,
            "cost": False,
            "session_id": False,
        },
        "line2": {"cwd": False, "cost_per_turn": False, "agents": False},
    },
}


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def get_toggle(custom, comp_id, line):
    return custom.get(line, {}).get(comp_id, True)


def set_toggle(custom, comp_id, line, value):
    if line not in custom:
        custom[line] = {}
    custom[line][comp_id] = value


def get_widget(custom):
    w = custom.get("widget", os.environ.get("STATUSLINE_WIDGET", "matrix"))
    return w if w in WIDGETS else "matrix"


def apply_preset(custom, preset_name):
    preset = PRESETS.get(preset_name, {})
    for comp_id, line, _, _, _ in COMPONENTS:
        set_toggle(custom, comp_id, line, True)
    for line, overrides in preset.items():
        for comp_id, value in overrides.items():
            set_toggle(custom, comp_id, line, value)


def find_component(name):
    """Find a component by ID. Returns (comp_id, line) or None."""
    for comp_id, line, _, _, _ in COMPONENTS:
        if comp_id == name:
            return comp_id, line
    return None


# ── Curses TUI ─────────────────────────────────────────────────────


def build_menu():
    items = []
    current_line = None
    line_labels = {
        "line1": "Line 1 \u2014 Session Core",
        "line2": "Line 2 \u2014 Project Telemetry",
        "line3": "Line 3 \u2014 Activity Trace",
    }
    for i, (comp_id, line, name, preview, color) in enumerate(COMPONENTS):
        if line != current_line:
            items.append({"type": "header", "label": line_labels[line]})
            current_line = line
        items.append(
            {
                "type": "component",
                "index": i,
                "comp_id": comp_id,
                "line": line,
                "name": name,
                "preview": preview,
                "color": color,
            }
        )
    items.append({"type": "header", "label": ""})
    items.append({"type": "widget", "label": "Widget"})
    items.append({"type": "preset", "label": "Preset"})
    items.append({"type": "buffer", "label": "Buffer"})
    items.append({"type": "header", "label": ""})
    items.append({"type": "save", "label": "Save & exit"})
    return items


def interactive_configurator(custom):
    menu = build_menu()
    selectable = [i for i, m in enumerate(menu) if m["type"] != "header"]
    cursor_idx = 0
    preset_names = list(PRESETS.keys()) + ["custom"]
    preset_idx = len(preset_names) - 1

    import tty
    import termios
    import select
    
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    
    try:
        tty.setraw(fd)
        sys.stdout.write(ALT_SCREEN_ON)
        sys.stdout.write(HIDE_CURSOR)
        sys.stdout.flush()
        
        while True:
            sys.stdout.write(CLEAR)
            
            # Draw logo
            logo_claude = [
                " ██████╗ ██╗      █████╗ ██╗   ██╗██████╗ ███████╗",
                "██╔════╝ ██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝",
                "██║      ██║     ███████║██║   ██║██║  ██║█████╗  ",
                "██║      ██║     ██╔══██║██║   ██║██║  ██║██╔══╝  ",
                "╚██████╗ ███████╗██║  ██║╚██████╔╝██████╔╝███████╗",
                " ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝",
            ]
            logo_ui = [
                "████████╗██╗   ██╗██╗",
                "╚══██╔══╝██║   ██║██║",
                "   ██║   ██║   ██║██║",
                "   ██║   ██║   ██║██║",
                "   ██║   ╚██████╔╝██║",
                "   ╚═╝    ╚═════╝ ╚═╝",
            ]
            
            for i, (cl, ui) in enumerate(zip(logo_claude, logo_ui)):
                sys.stdout.write(f"\033[{i+2};3H{WHITE}{BOLD}{cl}{LOGO_GREEN}{BOLD}{ui}{RESET}")
                
            sys.stdout.write(f"\033[9;5H{DIM}Statusline Configurator{RESET}")
            sys.stdout.write(f"\033[11;3H{CYAN}↑↓{RESET} navigate  {CYAN}Space{RESET} toggle  {CYAN}←→{RESET} preset  {CYAN}s{RESET} save  {CYAN}q{RESET} quit")
            
            widget = get_widget(custom)
            current_sel = selectable[cursor_idx]
            row = 13
            
            for idx, item in enumerate(menu):
                is_selected = idx == current_sel
                sys.stdout.write(f"\033[{row};3H")
                
                if item["type"] == "header":
                    if item["label"]:
                        sys.stdout.write(f"{BOLD}{item['label']}{RESET}")
                    row += 1
                    continue
                    
                sel_prefix = f"{CYAN}{BOLD}▸ {RESET}" if is_selected else "  "
                sys.stdout.write(sel_prefix)
                
                if item["type"] == "component":
                    enabled = get_toggle(custom, item["comp_id"], item["line"])
                    mark = f"{GREEN}✓{RESET}" if enabled else f"{RED}✗{RESET}"
                    name_attr = BOLD if is_selected else ""
                    sys.stdout.write(f"{mark} {name_attr}{item['name']:<18s}{RESET}")
                    
                    prev_str = item["preview"]()
                    if enabled:
                        sys.stdout.write(f"{BOLD}{prev_str}{RESET}")
                    else:
                        import re
                        clean = re.sub(r'\033\[[0-9;]+m', '', prev_str)
                        sys.stdout.write(f"{DIM}{clean}{RESET}")
                    row += 1
                    
                elif item["type"] == "widget":
                    sys.stdout.write(f"{(BOLD if is_selected else '')}Widget: {RESET}{CYAN}{BOLD}◂ {RESET}")
                    for j, wn in enumerate(WIDGETS):
                        if j > 0:
                            sys.stdout.write(f"{DIM}  {RESET}")
                        if wn == widget:
                            sys.stdout.write(f"{CYAN}{BOLD}[{wn}]{RESET}")
                        else:
                            sys.stdout.write(f"{DIM}{wn}{RESET}")
                    sys.stdout.write(f" {CYAN}{BOLD}▸{RESET}")
                    row += 1
                    
                elif item["type"] == "preset":
                    sys.stdout.write(f"{(BOLD if is_selected else '')}Preset: {RESET}{CYAN}{BOLD}◂ {RESET}")
                    for j, name in enumerate(preset_names):
                        if j > 0:
                            sys.stdout.write(f"{DIM}  {RESET}")
                        if j == preset_idx:
                            sys.stdout.write(f"{CYAN}{BOLD}[{name}]{RESET}")
                        else:
                            sys.stdout.write(f"{DIM}{name}{RESET}")
                    sys.stdout.write(f" {CYAN}{BOLD}▸{RESET}")
                    row += 1
                    
                elif item["type"] == "buffer":
                    buf_val = custom.get("buffer", 30)
                    sys.stdout.write(f"{(BOLD if is_selected else '')}Buffer: {RESET}{CYAN}{BOLD}◂ {RESET}{CYAN}{BOLD}{buf_val}{RESET} {CYAN}{BOLD}▸{RESET} {DIM} chars from edge{RESET}")
                    row += 1
                    
                elif item["type"] == "save":
                    clr = GREEN if is_selected else DIM
                    sys.stdout.write(f"{clr}{BOLD}Save & exit{RESET}")
                    row += 1

            sys.stdout.flush()
            
            # Wait for input
            while True:
                r, _, _ = select.select([sys.stdin], [], [])
                if r:
                    key = os.read(fd, 1).decode('utf-8', errors='ignore')
                    if key == '\x1b':
                        r2, _, _ = select.select([sys.stdin], [], [], 0.05)
                        if r2:
                            seq1 = os.read(fd, 1).decode('utf-8', errors='ignore')
                            if seq1 == '[':
                                r3, _, _ = select.select([sys.stdin], [], [], 0.05)
                                if r3:
                                    seq2 = os.read(fd, 1).decode('utf-8', errors='ignore')
                                    if seq2 == 'A': key = 'k'  # UP
                                    elif seq2 == 'B': key = 'j' # DOWN
                                    elif seq2 == 'C': key = 'l' # RIGHT
                                    elif seq2 == 'D': key = 'h' # LEFT
                        else:
                            key = 'q'
                    break
                    
            if key in ('k', 'K'):
                if cursor_idx > 0: cursor_idx -= 1
            elif key in ('j', 'J'):
                if cursor_idx < len(selectable) - 1: cursor_idx += 1
            elif key == ' ':
                item = menu[selectable[cursor_idx]]
                if item["type"] == "component":
                    cid, ln = item["comp_id"], item["line"]
                    set_toggle(custom, cid, ln, not get_toggle(custom, cid, ln))
                    preset_idx = len(preset_names) - 1
                elif item["type"] == "widget":
                    wi = WIDGETS.index(widget)
                    custom["widget"] = WIDGETS[(wi + 1) % len(WIDGETS)]
                elif item["type"] == "preset":
                    preset_idx = (preset_idx + 1) % len(preset_names)
                    if preset_names[preset_idx] != "custom":
                        apply_preset(custom, preset_names[preset_idx])
                elif item["type"] == "buffer":
                    custom["buffer"] = min(60, custom.get("buffer", 30) + 5)
            elif key in ('h', 'l'):
                item = menu[selectable[cursor_idx]]
                dir = 1 if key == 'l' else -1
                if item["type"] == "widget":
                    wi = WIDGETS.index(widget)
                    custom["widget"] = WIDGETS[(wi + dir) % len(WIDGETS)]
                elif item["type"] == "preset":
                    preset_idx = (preset_idx + dir) % len(preset_names)
                    if preset_names[preset_idx] != "custom":
                        apply_preset(custom, preset_names[preset_idx])
                elif item["type"] == "buffer":
                    buf = custom.get("buffer", 30)
                    custom["buffer"] = max(0, min(60, buf + dir * 5))
            elif key in ('\n', '\r'):
                item = menu[selectable[cursor_idx]]
                if item["type"] == "component":
                    cid, ln = item["comp_id"], item["line"]
                    set_toggle(custom, cid, ln, not get_toggle(custom, cid, ln))
                    preset_idx = len(preset_names) - 1
                elif item["type"] == "widget":
                    wi = WIDGETS.index(widget)
                    custom["widget"] = WIDGETS[(wi + 1) % len(WIDGETS)]
                elif item["type"] == "preset":
                    preset_idx = (preset_idx + 1) % len(preset_names)
                    if preset_names[preset_idx] != "custom":
                        apply_preset(custom, preset_names[preset_idx])
                elif item["type"] == "buffer":
                    custom["buffer"] = min(60, custom.get("buffer", 30) + 5)
                elif item["type"] == "save":
                    return True
            elif key == 's':
                return True
            elif key in ('q', '\x03'):
                return False
            elif key == '1':
                preset_idx = 0
                apply_preset(custom, preset_names[0])
            elif key == '2':
                preset_idx = 1
                apply_preset(custom, preset_names[1])
            elif key == '3':
                preset_idx = 2
                apply_preset(custom, preset_names[2])

    finally:
        sys.stdout.write(SHOW_CURSOR)
        sys.stdout.write(ALT_SCREEN_OFF)
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)



# ── CLI ────────────────────────────────────────────────────────────


def print_current(custom):
    """Print current custom configuration."""
    widget = get_widget(custom)
    buf_val = custom.get("buffer", 30)
    print(f"  {BOLD}Widget:{RESET} {CYAN}{widget}{RESET}")
    print(f"  {BOLD}Buffer:{RESET} {CYAN}{buf_val}{RESET} chars from edge")

    line_labels = {"line1": "Line 1", "line2": "Line 2", "line3": "Line 3"}

    for line_key in ("line1", "line2", "line3"):
        hidden = [
            name
            for cid, ln, name, _, _ in COMPONENTS
            if ln == line_key and not get_toggle(custom, cid, ln)
        ]
        if hidden:
            print(f"  {BOLD}{line_labels[line_key]}:{RESET}")
            for name in hidden:
                print(f"    {RED}\u2717{RESET} {name}")

    all_visible = all(get_toggle(custom, cid, ln) for cid, ln, _, _, _ in COMPONENTS)
    if all_visible:
        print(f"  {GREEN}\u2713{RESET} All components visible")
    print()


def parse_component_list(value):
    """Parse comma-separated component list, validating each name."""
    names = [n.strip() for n in value.split(",") if n.strip()]
    invalid = [n for n in names if n not in COMPONENT_IDS]
    if invalid:
        print(f"  {RED}\u2717{RESET} Unknown component(s): {', '.join(invalid)}")
        print()
        print(f"  {DIM}Available components:{RESET}")
        for comp_id, _, name, _, _ in COMPONENTS:
            print(f"    {CYAN}{comp_id:<22s}{RESET} {DIM}{name}{RESET}")
        print()
        sys.exit(1)
    return names


def cmd_custom(args):
    """Handle the 'custom' subcommand."""
    cfg = load_config()
    custom = cfg.get("custom", {})

    if not args:
        # Interactive mode
        should_save = interactive_configurator(custom)
        if should_save:
            cfg["custom"] = custom
            save_config(cfg)
            print()
            print(
                f"  {GREEN}\u2713{RESET} Configuration saved to {DIM}{CONFIG_PATH}{RESET}"
            )
            print(f"  {DIM}Changes apply on next statusline refresh{RESET}")

            hidden = [
                name
                for comp_id, line, name, _, _ in COMPONENTS
                if not get_toggle(custom, comp_id, line)
            ]
            if hidden:
                print(f"  {DIM}Hidden: {', '.join(hidden)}{RESET}")
            else:
                print(f"  {DIM}All components visible{RESET}")
            print(f"  {DIM}Widget: {get_widget(custom)}{RESET}")
            print()
        else:
            print()
            print(f"  {DIM}No changes saved{RESET}")
            print()
        return

    # Parse CLI flags
    modified = False
    i = 0
    while i < len(args):
        arg = args[i]

        if arg in ("-h", "--help"):
            print(CUSTOM_HELP)
            return

        elif arg in ("-l", "--list"):
            print_current(custom)
            i += 1
            continue

        elif arg in ("-b", "--buffer"):
            if i + 1 >= len(args):
                print(f"  {RED}\u2717{RESET} --buffer requires a value (0-60)")
                sys.exit(1)
            i += 1
            try:
                val = int(args[i])
            except ValueError:
                print(f"  {RED}\u2717{RESET} --buffer must be a number (0-60)")
                sys.exit(1)
            if val < 0 or val > 60:
                print(f"  {RED}\u2717{RESET} --buffer must be between 0 and 60")
                sys.exit(1)
            custom["buffer"] = val
            modified = True

        elif arg in ("-w", "--widget"):
            if i + 1 >= len(args):
                print(f"  {RED}\u2717{RESET} --widget requires a value")
                print(f"  {DIM}Available: {', '.join(WIDGETS)}{RESET}")
                sys.exit(1)
            i += 1
            w = args[i]
            if w not in WIDGETS:
                print(f"  {RED}\u2717{RESET} Unknown widget: {w}")
                print(f"  {DIM}Available: {', '.join(WIDGETS)}{RESET}")
                sys.exit(1)
            custom["widget"] = w
            modified = True

        elif arg in ("-p", "--preset"):
            if i + 1 >= len(args):
                print(f"  {RED}\u2717{RESET} --preset requires a value")
                print(f"  {DIM}Available: {', '.join(PRESETS.keys())}{RESET}")
                sys.exit(1)
            i += 1
            p = args[i]
            if p not in PRESETS:
                print(f"  {RED}\u2717{RESET} Unknown preset: {p}")
                print(f"  {DIM}Available: {', '.join(PRESETS.keys())}{RESET}")
                sys.exit(1)
            apply_preset(custom, p)
            modified = True

        elif arg == "--hide":
            if i + 1 >= len(args):
                print(f"  {RED}\u2717{RESET} --hide requires component names")
                sys.exit(1)
            i += 1
            for name in parse_component_list(args[i]):
                found = find_component(name)
                if found:
                    set_toggle(custom, found[0], found[1], False)
            modified = True

        elif arg == "--show":
            if i + 1 >= len(args):
                print(f"  {RED}\u2717{RESET} --show requires component names")
                sys.exit(1)
            i += 1
            for name in parse_component_list(args[i]):
                found = find_component(name)
                if found:
                    set_toggle(custom, found[0], found[1], True)
            modified = True

        else:
            print(f"  {RED}\u2717{RESET} Unknown option: {arg}")
            print(f"  {DIM}Run claudetui mode custom --help for usage{RESET}")
            sys.exit(1)

        i += 1

    if modified:
        cfg["custom"] = custom
        save_config(cfg)
        print(f"  {GREEN}\u2713{RESET} Configuration saved")
        print(f"  {DIM}Changes apply on next statusline refresh{RESET}")


# ── Main ───────────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]

    if not args:
        show_current()
        return

    cmd = args[0]

    if cmd in ("-h", "--help"):
        print(MAIN_HELP)
    elif cmd == "full":
        set_mode("full")
    elif cmd == "compact":
        set_mode("compact")
    elif cmd == "custom":
        # Remove --compact flag if present, then launch custom configurator
        settings = load_settings()
        current_cmd = settings.get("statusLine", {}).get("command", "")
        if current_cmd and "--compact" in current_cmd:
            base_cmd = current_cmd.replace(" --compact", "").strip()
            settings["statusLine"]["command"] = base_cmd
            save_settings(settings)
            print(f"  {DIM}Removed compact flag for custom mode{RESET}")
        cmd_custom(args[1:])
    else:
        print(f"  {RED}\u2717{RESET} Unknown command: {cmd}")
        print()
        print(f"  {DIM}Run claudetui mode --help for usage{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
