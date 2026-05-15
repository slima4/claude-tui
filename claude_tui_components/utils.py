"""Shared terminal utilities."""

import os
import re
import shutil
import subprocess

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

def visible_len(s):
    """Length of string after stripping ANSI escape codes."""
    return len(_ANSI_RE.sub("", s))

def truncate(s, max_cols):
    """Truncate an ANSI-colored string to max_cols characters."""
    visible = 0
    i = 0
    while i < len(s):
        m = _ANSI_RE.match(s, i)
        if m:
            i = m.end()
            continue
        visible += 1
        if visible > max_cols:
            return s[:i] + "\033[0m"
        i += 1
    return s

def visual_rows(lines, term_width):
    """Count actual terminal rows, accounting for line wrapping."""
    rows = 0
    for line in lines:
        vlen = visible_len(line)
        rows += max(1, -(-vlen // term_width))  # ceil division, min 1
    return rows

def format_tokens(n):
    """Format token count as compact string (e.g. 150k, 1.2M)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def get_terminal_cols():
    """Get terminal width, fallback to 80."""
    import fcntl, struct, termios
    try:
        pid = os.getpid()
        for _ in range(10):
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,tty="], capture_output=True, text=True, timeout=1
            )
            parts = result.stdout.split()
            if len(parts) < 2:
                break
            ppid, tty = parts[0], parts[1]
            if tty not in ("??", "?", ""):
                fd = os.open(f"/dev/{tty}", os.O_RDONLY)
                try:
                    res = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
                    return struct.unpack("HHHH", res)[1]
                finally:
                    os.close(fd)
            pid = int(ppid)
            if pid <= 1:
                break
    except Exception:
        pass
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80
