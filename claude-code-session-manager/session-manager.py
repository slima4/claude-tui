#!/usr/bin/env python3
"""
Claude Code Session Manager — Browse, Search, and Export Sessions

List, filter, compare, and export Claude Code sessions.

Usage:
    # List recent sessions
    python3 session-manager.py list

    # List sessions for a specific project
    python3 session-manager.py list --project my-project

    # Show details for a session
    python3 session-manager.py show abc12345

    # Resume a session (opens Claude Code with --resume)
    python3 session-manager.py resume abc12345

    # Compare two sessions
    python3 session-manager.py diff abc12345 def67890

    # Export session as readable markdown
    python3 session-manager.py export abc12345

    # Export as JSON
    python3 session-manager.py export abc12345 --json
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ANSI
RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
ORANGE = "\033[38;5;208m"
RED = "\033[31m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"
GRAY = "\033[90m"
DIM = "\033[2m"

from claude_tui_core.models import MODEL_PRICING


def get_projects_dir():
    return Path.home() / ".claude" / "projects"


def find_all_sessions(project_filter=None, days=None, limit=20):
    """Find all sessions, optionally filtered."""
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return []

    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    sessions = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue

        project_name = project_dir.name
        if project_filter and project_filter.lower() not in project_name.lower():
            continue

        for jsonl_file in project_dir.glob("*.jsonl"):
            mtime = datetime.fromtimestamp(
                jsonl_file.stat().st_mtime, tz=timezone.utc
            )
            if cutoff and mtime < cutoff:
                continue

            # Quick parse for metadata
            meta = quick_parse(jsonl_file)
            meta["path"] = jsonl_file
            meta["session_id"] = jsonl_file.stem
            meta["project"] = project_name
            meta["modified"] = mtime
            meta["size_kb"] = jsonl_file.stat().st_size / 1024

            sessions.append(meta)

    sessions.sort(key=lambda s: s["modified"], reverse=True)
    return sessions[:limit] if limit else sessions


def quick_parse(transcript_path):
    """Fast parse — only extracts metadata without full analysis."""
    meta = {
        "model": "",
        "version": "",
        "git_branch": "",
        "start_time": None,
        "end_time": None,
        "user_messages": 0,
        "compact_count": 0,
        "cost_estimate": 0.0,
        "slug": "",
    }

    input_total = 0
    cache_read_total = 0
    cache_creation_total = 0
    output_total = 0

    try:
        with open(transcript_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if meta["start_time"] is None and "timestamp" in obj:
                    meta["start_time"] = obj["timestamp"]

                if "timestamp" in obj:
                    meta["end_time"] = obj["timestamp"]

                if not meta["model"] and obj.get("type") == "assistant":
                    msg = obj.get("message", {})
                    if "model" in msg:
                        meta["model"] = msg["model"]

                if not meta["version"] and "version" in obj:
                    meta["version"] = obj["version"]

                if not meta["git_branch"] and "gitBranch" in obj:
                    meta["git_branch"] = obj["gitBranch"]

                if not meta["slug"] and "slug" in obj:
                    meta["slug"] = obj["slug"]

                if obj.get("type") == "user" and not obj.get("isMeta"):
                    content = obj.get("message", {}).get("content", "")
                    has_text = False
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                has_text = True
                                break
                    elif isinstance(content, str) and content.strip():
                        has_text = True
                    if has_text:
                        meta["user_messages"] += 1

                if (obj.get("type") == "summary" or
                        (obj.get("type") == "system" and obj.get("subtype") == "compact_boundary")):
                    meta["compact_count"] += 1

                if (
                    obj.get("type") == "assistant"
                    and "message" in obj
                    and "usage" in obj["message"]
                ):
                    usage = obj["message"]["usage"]
                    input_total += usage.get("input_tokens", 0)
                    cache_read_total += usage.get("cache_read_input_tokens", 0)
                    cache_creation_total += usage.get("cache_creation_input_tokens", 0)
                    output_total += usage.get("output_tokens", 0)

    except (FileNotFoundError, PermissionError):
        return meta

    # Estimate cost
    pricing = MODEL_PRICING.get("claude-sonnet-4-6")
    for key, p in MODEL_PRICING.items():
        if key in meta["model"]:
            pricing = p
            break

    meta["cost_estimate"] = (
        input_total * pricing["input"] / 1_000_000
        + cache_read_total * pricing["cache_read"] / 1_000_000
        + cache_creation_total * pricing.get("cache_write", pricing["input"] * 1.25) / 1_000_000
        + output_total * pricing["output"] / 1_000_000
    )

    return meta


def find_session_by_id(session_id):
    """Find a specific session by ID prefix. Warns on ambiguous matches."""
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return None

    matches = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            if jsonl_file.stem.startswith(session_id):
                matches.append(jsonl_file)

    if not matches:
        return None
    if len(matches) > 1:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        print(f"  {YELLOW}Ambiguous prefix '{session_id}' matches "
              f"{len(matches)} sessions — using most recent{RESET}")
    return matches[0]


def format_time(ts):
    """Format ISO timestamp to local readable time."""
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:16]


def format_time_short(ts):
    """Format ISO timestamp to short local time."""
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        now = datetime.now(dt.tzinfo)
        if dt.date() == now.date():
            return dt.strftime("today %H:%M")
        elif dt.date() == (now - timedelta(days=1)).date():
            return dt.strftime("yesterday %H:%M")
        return dt.strftime("%b %d %H:%M")
    except Exception:
        return "?"


def format_duration_from_timestamps(start, end):
    """Calculate and format duration between two timestamps."""
    if not start or not end:
        return "?"
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        e = datetime.fromisoformat(end.replace("Z", "+00:00"))
        minutes = int((e - s).total_seconds() / 60)
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins:02d}m"
    except Exception:
        return "?"


def project_short_name(project):
    """Extract readable project name."""
    parts = project.replace("-Users-", "").split("-")
    # Skip username parts, keep project name
    if len(parts) > 2:
        return "-".join(parts[2:])
    return parts[-1] if parts else project


# ─── Commands ───


def cmd_list(args):
    """List sessions."""
    sessions = find_all_sessions(
        project_filter=args.project,
        days=args.days,
        limit=args.limit,
    )

    if not sessions:
        print(f"  {RED}No sessions found.{RESET}")
        return

    print()
    print(f"  {BOLD}{WHITE}Recent Sessions{RESET}")
    print(f"  {GRAY}{'─' * 100}{RESET}")
    print(
        f"  {GRAY}{'ID':8} {'When':>16} {'Duration':>8} "
        f"{'Cost':>8} {'Msgs':>5} {'⟳':>3} {'Project':20} {'Branch':17} {'Model':15}{RESET}"
    )
    print(f"  {GRAY}{'─' * 100}{RESET}")

    for s in sessions:
        sid = s["session_id"][:8]
        when = format_time_short(s["start_time"])
        duration = format_duration_from_timestamps(s["start_time"], s["end_time"])
        cost = s["cost_estimate"]
        cost_color = GREEN if cost < 1 else YELLOW if cost < 5 else RED
        project = project_short_name(s["project"])[:20]
        branch = (s["git_branch"] or "")[:17]
        model = s["model"].replace("claude-", "")[:15] if s["model"] else "?"

        print(
            f"  {WHITE}{sid}{RESET} {when:>16} {duration:>8} "
            f"{cost_color}${cost:>6.2f}{RESET} {s['user_messages']:>5} "
            f"{s['compact_count']:>3} {CYAN}{project:20}{RESET} "
            f"{GREEN}{branch:17}{RESET} {GRAY}{model:15}{RESET}"
        )

    print(f"  {GRAY}{'─' * 100}{RESET}")
    total_cost = sum(s["cost_estimate"] for s in sessions)
    total_color = GREEN if total_cost < 5 else YELLOW if total_cost < 20 else RED
    print(f"  {GRAY}Total cost: {total_color}${total_cost:.2f}{RESET}")
    print()


def cmd_show(args):
    """Show session details."""
    path = find_session_by_id(args.session_id)
    if not path:
        print(f"  {RED}Session '{args.session_id}' not found.{RESET}")
        return

    # Reuse session-stats parse logic
    meta = quick_parse(path)

    print()
    print(f"  {BOLD}{WHITE}Session Details{RESET}")
    print(f"  {GRAY}{'─' * 50}{RESET}")
    print(f"  {GRAY}ID:       {RESET}{path.stem}")
    print(f"  {GRAY}Project:  {RESET}{project_short_name(path.parent.name)}")
    print(f"  {GRAY}Model:    {RESET}{meta['model']}")
    print(f"  {GRAY}Branch:   {RESET}{GREEN}{meta['git_branch']}{RESET}")
    print(f"  {GRAY}Version:  {RESET}{meta['version']}")
    print(f"  {GRAY}Slug:     {RESET}{meta['slug']}")
    print(f"  {GRAY}Started:  {RESET}{format_time(meta['start_time'])}")
    print(f"  {GRAY}Ended:    {RESET}{format_time(meta['end_time'])}")
    print(f"  {GRAY}Duration: {RESET}{format_duration_from_timestamps(meta['start_time'], meta['end_time'])}")
    print(f"  {GRAY}Messages: {RESET}{meta['user_messages']}")
    print(f"  {GRAY}Compacts: {RESET}{meta['compact_count']}")
    cost_color = GREEN if meta["cost_estimate"] < 1 else YELLOW if meta["cost_estimate"] < 5 else RED
    print(f"  {GRAY}Cost:     {RESET}{cost_color}${meta['cost_estimate']:.2f}{RESET}")
    print(f"  {GRAY}File:     {RESET}{DIM}{path}{RESET}")
    print()
    print(f"  {GRAY}Resume:   {CYAN}claude --resume {path.stem}{RESET}")
    print()


def cmd_resume(args):
    """Resume a session."""
    path = find_session_by_id(args.session_id)
    if not path:
        print(f"  {RED}Session '{args.session_id}' not found.{RESET}")
        return

    full_id = path.stem
    print(f"  {GREEN}Resuming session {full_id[:8]}...{RESET}")
    # The claudetui wrapper runs Python without the user's PYTHONHOME/PYTHONPATH
    # and keeps them in CLAUDETUI_ORIG_* (see _restore_python_env in claudetui.py).
    # claude should get them back, not the dispatcher's injected PYTHONPATH.
    env = os.environ.copy()
    for var in ("PYTHONHOME", "PYTHONPATH"):
        marker = f"CLAUDETUI_ORIG_{var}"
        if marker in env:
            orig = env.pop(marker)
            if orig:
                env[var] = orig
            else:
                env.pop(var, None)
    os.execvpe("claude", ["claude", "--resume", full_id], env)


def cmd_diff(args):
    """Compare two sessions."""
    path1 = find_session_by_id(args.session_id_1)
    path2 = find_session_by_id(args.session_id_2)

    if not path1:
        print(f"  {RED}Session '{args.session_id_1}' not found.{RESET}")
        return
    if not path2:
        print(f"  {RED}Session '{args.session_id_2}' not found.{RESET}")
        return

    m1 = quick_parse(path1)
    m2 = quick_parse(path2)

    print()
    print(f"  {BOLD}{WHITE}Session Comparison{RESET}")
    print(f"  {GRAY}{'─' * 60}{RESET}")
    print(f"  {'':20} {CYAN}{'Session A':>18}{RESET}  {MAGENTA}{'Session B':>18}{RESET}")
    print(f"  {GRAY}{'─' * 60}{RESET}")

    rows = [
        ("ID", path1.stem[:8], path2.stem[:8]),
        ("Project", project_short_name(path1.parent.name), project_short_name(path2.parent.name)),
        ("Model", m1["model"].replace("claude-", ""), m2["model"].replace("claude-", "")),
        ("Branch", m1["git_branch"], m2["git_branch"]),
        ("Date", format_time(m1["start_time"])[:10], format_time(m2["start_time"])[:10]),
        ("Duration", format_duration_from_timestamps(m1["start_time"], m1["end_time"]),
                     format_duration_from_timestamps(m2["start_time"], m2["end_time"])),
        ("Messages", str(m1["user_messages"]), str(m2["user_messages"])),
        ("Compactions", str(m1["compact_count"]), str(m2["compact_count"])),
        ("Cost", f"${m1['cost_estimate']:.2f}", f"${m2['cost_estimate']:.2f}"),
    ]

    for label, v1, v2 in rows:
        # Highlight differences
        if v1 != v2:
            print(f"  {label:20} {CYAN}{v1:>18}{RESET}  {MAGENTA}{v2:>18}{RESET}")
        else:
            print(f"  {label:20} {v1:>18}  {v2:>18}")

    # Cost difference
    cost_diff = m2["cost_estimate"] - m1["cost_estimate"]
    diff_color = GREEN if cost_diff <= 0 else RED
    print(f"  {GRAY}{'─' * 60}{RESET}")
    print(f"  {'Cost difference':20} {diff_color}{'+' if cost_diff > 0 else ''}${cost_diff:.2f}{RESET}")
    print()


def cmd_export(args):
    """Export session as markdown or JSON."""
    path = find_session_by_id(args.session_id)
    if not path:
        print(f"  {RED}Session '{args.session_id}' not found.{RESET}", file=sys.stderr)
        return

    entries = []
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, PermissionError):
        print(f"  {RED}Cannot read session file.{RESET}", file=sys.stderr)
        return

    if args.json:
        # Filter to conversation entries only
        conversation = []
        for e in entries:
            if e.get("type") in ("user", "assistant") and "message" in e:
                conversation.append({
                    "type": e["type"],
                    "timestamp": e.get("timestamp", ""),
                    "message": e["message"],
                    "isMeta": e.get("isMeta", False),
                })
        print(json.dumps(conversation, indent=2))
        return

    # Markdown export
    meta = quick_parse(path)
    output = []
    output.append(f"# Session {path.stem[:8]}")
    output.append("")
    output.append(f"- **Project**: {project_short_name(path.parent.name)}")
    output.append(f"- **Model**: {meta['model']}")
    if meta["git_branch"]:
        output.append(f"- **Branch**: {meta['git_branch']}")
    output.append(f"- **Date**: {format_time(meta['start_time'])}")
    output.append(f"- **Duration**: {format_duration_from_timestamps(meta['start_time'], meta['end_time'])}")
    output.append(f"- **Cost**: ${meta['cost_estimate']:.2f}")
    output.append("")
    output.append("---")
    output.append("")

    compact_num = 0
    for entry in entries:
        # Mark compaction boundaries
        if (entry.get("type") == "system"
                and entry.get("subtype") == "compact_boundary"):
            compact_num += 1
            ts = entry.get("timestamp", "")
            time_str = format_time(ts) if ts else ""
            meta_c = entry.get("compactMetadata", {})
            trigger = meta_c.get("trigger", "?")
            pre_tokens = meta_c.get("preTokens", 0)
            output.append(f"---")
            output.append("")
            output.append(f"**⚡ Compaction #{compact_num}** ({time_str}) — {trigger}, {pre_tokens:,} tokens before")
            output.append("")
            output.append(f"---")
            output.append("")
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        if entry.get("isMeta"):
            continue

        msg = entry.get("message", {})
        role = entry["type"]
        timestamp = entry.get("timestamp", "")
        time_str = format_time(timestamp) if timestamp else ""

        if role == "user":
            output.append(f"## User {GRAY}({time_str}){RESET}" if sys.stdout.isatty() else f"## User ({time_str})")
            output.append("")
            content = msg.get("content", "")
            if isinstance(content, str):
                output.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            output.append(block.get("text", ""))
                        elif block.get("type") == "tool_result":
                            # Skip tool results in export
                            continue
                    elif isinstance(block, str):
                        output.append(block)
            output.append("")

        elif role == "assistant":
            output.append(f"## Assistant ({time_str})")
            output.append("")
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        output.append(block.get("text", ""))
                        output.append("")
                    elif block.get("type") == "tool_use":
                        tool = block.get("name", "?")
                        inp = block.get("input", {})
                        file_path = inp.get("file_path", inp.get("path", inp.get("command", "")))
                        if file_path:
                            output.append(f"> **{tool}**: `{file_path}`")
                        else:
                            output.append(f"> **{tool}**")
                        output.append("")

    print("\n".join(output))


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code Session Manager"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # list
    list_parser = subparsers.add_parser("list", aliases=["ls"], help="List sessions")
    list_parser.add_argument("--project", "-p", help="Filter by project name")
    list_parser.add_argument("--days", "-d", type=int, default=30, help="Show sessions from last N days (default: 30)")
    list_parser.add_argument("--limit", "-n", type=int, default=20, help="Max sessions to show (default: 20)")

    # show
    show_parser = subparsers.add_parser("show", help="Show session details")
    show_parser.add_argument("session_id", help="Session ID (prefix match)")

    # resume
    resume_parser = subparsers.add_parser("resume", help="Resume a session")
    resume_parser.add_argument("session_id", help="Session ID (prefix match)")

    # diff
    diff_parser = subparsers.add_parser("diff", aliases=["compare"], help="Compare two sessions")
    diff_parser.add_argument("session_id_1", help="First session ID")
    diff_parser.add_argument("session_id_2", help="Second session ID")

    # export
    export_parser = subparsers.add_parser("export", help="Export session transcript")
    export_parser.add_argument("session_id", help="Session ID (prefix match)")
    export_parser.add_argument("--json", "-j", action="store_true", help="Export as JSON")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        "list": cmd_list,
        "ls": cmd_list,
        "show": cmd_show,
        "resume": cmd_resume,
        "diff": cmd_diff,
        "compare": cmd_diff,
        "export": cmd_export,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)


if __name__ == "__main__":
    main()
