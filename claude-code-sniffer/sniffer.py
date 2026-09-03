#!/usr/bin/env python3
"""ClaudeTUI API Sniffer — intercept and log Claude Code API calls.

A transparent HTTP proxy that captures all API requests/responses between
Claude Code and the API it talks to. Uses ANTHROPIC_BASE_URL to redirect
traffic through localhost — no TLS interception or certificates needed.

Usage:
    claudetui sniffer                  # start on default port 7735
    claudetui sniffer --port 8080      # custom port
    claudetui sniffer --full           # log complete request/response bodies
    claudetui sniffer --quiet          # no terminal output, log only
    claudetui sniffer --upstream URL   # forward to a gateway instead of Anthropic
"""

import argparse
import errno
import http.client
import http.server
import json
import os
import signal
import ssl
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from claude_tui_core.models import MODEL_PRICING, get_model_pricing_fuzzy as _match_pricing

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_UPSTREAM = "https://api.anthropic.com"
UPSTREAM_ENV = "CLAUDETUI_UPSTREAM"
UPSTREAM_TOKEN_ENV = "CLAUDETUI_UPSTREAM_TOKEN"
UPSTREAM_INSECURE_ENV = "CLAUDETUI_UPSTREAM_INSECURE"
DEFAULT_PORT = 7735
LOG_DIR = Path.home() / ".claude" / "api-sniffer"
PORT_DIR = LOG_DIR  # port files stored as .port.{PORT}

REDACT_HEADERS = {"x-api-key", "authorization"}

# ANSI
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
GRAY = "\033[90m"
LOGO_GREEN = "\033[38;5;46m"

LOGO_LINES = [
    (f" {BOLD} ██████╗ ██╗      █████╗ ██╗   ██╗██████╗ ███████╗", f"{LOGO_GREEN}████████╗██╗   ██╗██╗{RESET}"),
    (f" {BOLD}██╔════╝ ██║     ██╔══██╗██║   ██║██╔══██╗██╔════╝", f"{LOGO_GREEN}╚══██╔══╝██║   ██║██║{RESET}"),
    (f" {BOLD}██║      ██║     ███████║██║   ██║██║  ██║█████╗  ", f"{LOGO_GREEN}   ██║   ██║   ██║██║{RESET}"),
    (f" {BOLD}██║      ██║     ██╔══██║██║   ██║██║  ██║██╔══╝  ", f"{LOGO_GREEN}   ██║   ██║   ██║██║{RESET}"),
    (f" {BOLD}╚██████╗ ███████╗██║  ██║╚██████╔╝██████╔╝███████╗", f"{LOGO_GREEN}   ██║   ╚██████╔╝██║{RESET}"),
    (f" {BOLD} ╚═════╝ ╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝", f"{LOGO_GREEN}   ╚═╝    ╚═════╝ ╚═╝{RESET}"),
]

# Pricing now imported from core via fuzzy matcher

# Shared SSL context — reused across all requests (avoids reloading CA bundle)
_SSL_CTX = ssl.create_default_context()

# Built lazily — only needed when --insecure is active
_SSL_CTX_INSECURE = None

# Hosts that resolve back to this machine, for the self-forwarding loop guard
_LOOPBACK_HOSTS = {"localhost", "0.0.0.0", "::", "::1"}


def _insecure_ssl_context():
    """SSL context with verification disabled — for self-signed gateways."""
    global _SSL_CTX_INSECURE
    if _SSL_CTX_INSECURE is None:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        _SSL_CTX_INSECURE = ctx
    return _SSL_CTX_INSECURE


# ── Upstream target ──────────────────────────────────────────────────

class Upstream:
    """Where the proxy forwards requests — Anthropic by default, or a gateway."""

    __slots__ = ("scheme", "host", "port", "prefix", "insecure", "token")

    def __init__(self, scheme, host, port, prefix="", insecure=False, token=None):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.prefix = prefix
        self.insecure = insecure
        self.token = token

    @property
    def default_port(self):
        return self.port == (443 if self.scheme == "https" else 80)

    @property
    def netloc(self):
        """Host[:port] for the Host header — port omitted when it's the default."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.default_port else f"{host}:{self.port}"

    @property
    def base_url(self):
        return f"{self.scheme}://{self.netloc}{self.prefix}"

    def target_path(self, path):
        """Prepend the upstream's base path to an incoming request path.

        Gateways often live under a prefix ("https://gw.corp/anthropic"), so the
        path Claude Code would have sent there is prefix + path.
        """
        return self.prefix + path if self.prefix else path

    def connect(self, timeout=300):
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.scheme == "https":
            ctx = _insecure_ssl_context() if self.insecure else _SSL_CTX
            return http.client.HTTPSConnection(host, self.port, context=ctx,
                                               timeout=timeout)
        return http.client.HTTPConnection(host, self.port, timeout=timeout)

    def points_at_port(self, port):
        """True when forwarding here would loop back into a local listener."""
        host = self.host.lower()
        is_local = host in _LOOPBACK_HOSTS or host.startswith("127.")
        return is_local and self.port == port

    def __repr__(self):
        return f"<Upstream {self.base_url}>"


def parse_upstream(url, insecure=False, token=None):
    """Parse an upstream base URL into an Upstream.

    Accepts a full URL, a bare host ("gw.corp:8080", https assumed), and an
    optional base path prefix. Parts that would be silently dropped — a query
    string, a fragment, inline credentials — are rejected instead.
    """
    raw = (url or "").strip() or DEFAULT_UPSTREAM
    if "://" not in raw:
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme {parts.scheme!r} — use http or https")
    if not parts.hostname:
        raise ValueError(f"no host in {raw!r}")
    if parts.query or parts.fragment:
        raise ValueError("a query string or fragment is not supported — "
                         "give only scheme, host and base path")
    if parts.username or parts.password:
        raise ValueError(f"credentials in the URL are not supported — "
                         f"use {UPSTREAM_TOKEN_ENV}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return Upstream(parts.scheme, parts.hostname, port, parts.path.rstrip("/"),
                    insecure=insecure, token=token)


def _format_tokens(n):
    """Format token count: 1234 -> '1.2k', 1234567 -> '1.2M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _format_bytes(n):
    """Format byte count: 1234 -> '1.2KB', 1234567 -> '1.2MB'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f}KB"
    return f"{n}B"


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _calc_cost(usage, model_id):
    """Calculate cost from usage dict."""
    pricing = _match_pricing(model_id)
    input_t = usage.get("input_tokens", 0)
    cache_r = usage.get("cache_read_input_tokens", 0)
    cache_w = usage.get("cache_creation_input_tokens", 0)
    output_t = usage.get("output_tokens", 0)
    return (
        input_t * pricing["input"]
        + cache_r * pricing["cache_read"]
        + cache_w * pricing["cache_write"]
        + output_t * pricing["output"]
    ) / 1_000_000


# ── Request/Response Summarizers ─────────────────────────────────────

def _summarize_request(body_bytes, full=False):
    """Extract key fields from request body for logging."""
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"raw_length": len(body_bytes)}

    if full:
        return body

    summary = {}
    for key in ("model", "max_tokens", "stream", "temperature", "top_p",
                "top_k", "stop_sequences", "metadata"):
        if key in body:
            summary[key] = body[key]

    # System prompt — just length
    system = body.get("system")
    if system:
        if isinstance(system, str):
            summary["system_length"] = len(system)
        elif isinstance(system, list):
            summary["system_length"] = sum(
                len(b.get("text", "")) for b in system if isinstance(b, dict)
            )

    # Messages — count and approximate size (avoid re-serializing)
    messages = body.get("messages", [])
    summary["message_count"] = len(messages)
    summary["body_length"] = len(body_bytes)

    # Tools — just names
    tools = body.get("tools", [])
    if tools:
        summary["tool_count"] = len(tools)
        summary["tool_names"] = [t.get("name", "?") for t in tools if isinstance(t, dict)]

    return summary


def _reassemble_sse(raw_bytes):
    """Parse SSE byte stream into structured response summary."""
    model = ""
    stop_reason = ""
    usage = {}
    block_types = []
    tool_names = []
    event_count = 0

    # Process line-by-line from bytes to avoid full decode + split copy
    for raw_line in raw_bytes.split(b"\n"):
        if not raw_line.startswith(b"data: "):
            continue
        try:
            data = json.loads(raw_line[6:])
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        event_count += 1
        etype = data.get("type", "")

        if etype == "message_start":
            msg = data.get("message", {})
            model = msg.get("model", "")
            usage = msg.get("usage", {})
        elif etype == "content_block_start":
            block = data.get("content_block", {})
            btype = block.get("type", "unknown")
            block_types.append(btype)
            if btype == "tool_use":
                tool_names.append(block.get("name", "?"))
        elif etype == "message_delta":
            delta_usage = data.get("usage", {})
            usage.update(delta_usage)
            delta = data.get("delta", {})
            if "stop_reason" in delta:
                stop_reason = delta["stop_reason"]

    return {
        "model": model,
        "stop_reason": stop_reason,
        "usage": usage,
        "content_blocks": block_types,
        "tool_names": tool_names,
        "event_count": event_count,
    }


# ── Session Tracker ──────────────────────────────────────────────────

class SessionTracker:
    """Detect sub-agents by tool availability.

    Main session always has the 'Agent' tool in its tool list. Sub-agents
    never do. Claude Code uses the SAME metadata session ID for sub-agents,
    so we can't distinguish by session ID — tool presence is the reliable
    signal. Sub-agents are grouped by model + system_length for labeling.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._agent_counter = 0
        self._contexts = {}          # (model_key, sys_len_bucket) -> agent_num

    def check(self, tool_names, system_length=0, model=""):
        """Return (label, is_new) based on tool availability.

        Returns:
            ("main", False) for main session (has Agent tool)
            ("agent.1", True) for first request from a new sub-agent
            ("agent.1", False) for known sub-agent context
            ("", False) for requests without tools (e.g. count_tokens)
        """
        if not tool_names:
            return ("", False)

        # Main session always has the Agent tool
        if "Agent" in tool_names:
            return ("main", False)

        with self._lock:
            # Sub-agent: group by model family + system_length bucket (2k range)
            # so haiku-explore (sys=3898) != haiku-websearch (sys=194)
            parts = model.split("-")[:3]
            model_key = "-".join(parts) if parts else model
            bucket = (model_key, system_length // 2000)

            if bucket in self._contexts:
                return (f"agent.{self._contexts[bucket]}", False)

            self._agent_counter += 1
            self._contexts[bucket] = self._agent_counter
            return (f"agent.{self._agent_counter}", True)

    @property
    def agent_count(self):
        with self._lock:
            return self._agent_counter


# ── Compaction Detection ─────────────────────────────────────────────

def _extract_session_id(metadata):
    """Extract short session ID from metadata user_id field."""
    if not metadata or not isinstance(metadata, dict):
        return ""
    user_id = metadata.get("user_id", "")
    if "_session_" in user_id:
        return user_id.split("_session_", 1)[1][:8]
    return ""


class CompactionDetector:
    """Detect compaction by comparing consecutive main-session requests.

    Only tracks main session requests (sub-agents are ignored). Tracks per
    session ID so a new Claude Code session connecting to the same sniffer
    doesn't trigger a false compaction.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}  # session_id -> (prev_msg_count, prev_body_length)

    def check(self, request_summary, is_main_session, session_id=""):
        """Return True if this main-session request looks post-compaction."""
        if not is_main_session:
            return False

        msg_count = request_summary.get("message_count", 0)
        body_length = request_summary.get("body_length", 0)
        key = session_id or "_default"

        with self._lock:
            prev_msg, prev_body = self._sessions.get(key, (0, 0))
            self._sessions[key] = (msg_count, body_length)

            if prev_msg > 5 and msg_count < prev_msg * 0.5:
                return True
            if prev_body > 10_000 and body_length < prev_body * 0.3:
                return True

        return False


# ── Sniffer Handler ──────────────────────────────────────────────────

class SnifferHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that forwards requests to the upstream API and logs them."""

    def do_POST(self):
        self._forward()

    def do_GET(self):
        self._forward()

    def do_PUT(self):
        self._forward()

    def do_DELETE(self):
        self._forward()

    def do_OPTIONS(self):
        self._forward()

    def do_HEAD(self):
        self._forward()

    def _forward(self):
        request_id = self.server.next_id()
        start_time = time.monotonic()
        timestamp = _now_iso()

        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        # Summarize request — always parse a logic summary for tracking,
        # use full body only for logging when --full is active
        logic_summary = _summarize_request(body, full=False)
        request_summary = (_summarize_request(body, full=True)
                           if self.server.full_bodies else logic_summary)
        is_streaming = False
        model_id = ""
        system_length = 0
        session_id = ""
        req_tool_names = []
        if isinstance(logic_summary, dict):
            is_streaming = logic_summary.get("stream", False)
            model_id = logic_summary.get("model", "")
            system_length = logic_summary.get("system_length", 0)
            session_id = _extract_session_id(logic_summary.get("metadata"))
            req_tool_names = logic_summary.get("tool_names", [])

        req_entry = {
            "type": "request",
            "id": request_id,
            "timestamp": timestamp,
            "method": self.command,
            "path": self.path,
            "headers": self._clean_headers(dict(self.headers)),
            "body": request_summary,
        }
        self.server.write_log(req_entry)

        # Forward to upstream
        conn = None
        try:
            upstream = self.server.upstream
            conn = upstream.connect()
            fwd_headers = {}
            has_auth = False
            for key, val in self.headers.items():
                lk = key.lower()
                if lk in ("host", "transfer-encoding", "accept-encoding"):
                    continue
                if lk == "authorization":
                    has_auth = True
                fwd_headers[key] = val
            fwd_headers["Host"] = upstream.netloc
            fwd_headers["Accept-Encoding"] = "identity"
            # Gateway token — only fills in when the client sent no bearer of its own
            if upstream.token and not has_auth:
                fwd_headers["Authorization"] = f"Bearer {upstream.token}"

            conn.request(self.command, upstream.target_path(self.path),
                         body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except Exception as e:
            if conn:
                conn.close()
            # Upstream connection failed
            error_body = json.dumps({"error": str(e)}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)

            err_entry = {
                "type": "error",
                "id": request_id,
                "timestamp": _now_iso(),
                "error": str(e),
                "latency_ms": int((time.monotonic() - start_time) * 1000),
            }
            self.server.write_log(err_entry)
            self.server.print_line(
                request_id, self.command, self.path, model_id,
                0, 0, 0, (time.monotonic() - start_time) * 1000,
                error=str(e), status=502,
            )
            return

        # Send response status and headers to client
        self.send_response(resp.status)
        skip_headers = {"transfer-encoding", "connection"}
        content_type = ""
        for key, val in resp.getheaders():
            if key.lower() not in skip_headers:
                self.send_header(key, val)
            if key.lower() == "content-type":
                content_type = val
        self.end_headers()

        # Detect SSE: check request body flag + response content-type
        is_sse = is_streaming or "text/event-stream" in content_type

        buffer = bytearray()
        try:
            if is_sse:
                # SSE streaming — use read1() for non-blocking chunk reads
                while True:
                    try:
                        chunk = resp.read1(8192)
                    except AttributeError:
                        chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    buffer.extend(chunk)
            else:
                # Non-streaming — read all at once
                data = resp.read()
                self.wfile.write(data)
                buffer.extend(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Client disconnected
        finally:
            conn.close()

        latency_ms = int((time.monotonic() - start_time) * 1000)

        # Build response log entry
        resp_entry = {
            "type": "response",
            "id": request_id,
            "timestamp": _now_iso(),
            "status": resp.status,
            "latency_ms": latency_ms,
            "streaming": is_sse,
        }

        if is_sse:
            assembled = _reassemble_sse(bytes(buffer))
            resp_model = assembled.get("model", "") or model_id
            resp_usage = assembled.get("usage", {})
            resp_entry.update(assembled)
        else:
            resp_model = model_id
            resp_usage = {}
            try:
                resp_body = json.loads(bytes(buffer))
                resp_usage = resp_body.get("usage", {})
                resp_model = resp_body.get("model", model_id)
            except (json.JSONDecodeError, UnicodeDecodeError):
                resp_body = {"raw_length": len(buffer)}
            resp_entry["model"] = resp_model
            resp_entry["usage"] = resp_usage
            resp_entry["body"] = resp_body if self.server.full_bodies else {"length": len(buffer)}

        # Session / sub-agent tracking (must run before compaction detection)
        session_label, is_new_agent = self.server.session_tracker.check(
            req_tool_names, system_length=system_length,
            model=resp_model or model_id)
        is_main = session_label == "main"

        # Compaction detection (main session only, per session ID)
        is_compaction = self.server.compaction_detector.check(
            logic_summary, is_main_session=is_main,
            session_id=session_id)
        if is_compaction:
            resp_entry["is_compaction"] = True

        self.server.write_log(resp_entry)

        # Extract metadata for display
        if is_sse:
            stop_reason = assembled.get("stop_reason", "")
            block_types = assembled.get("content_blocks", [])
            tool_names = assembled.get("tool_names", [])
        else:
            stop_reason = resp_body.get("stop_reason", "") if isinstance(resp_body, dict) else ""
            content = resp_body.get("content", []) if isinstance(resp_body, dict) else []
            block_types = [b.get("type", "") for b in content if isinstance(b, dict)]
            tool_names = [b.get("name", "?") for b in content
                         if isinstance(b, dict) and b.get("type") == "tool_use"]

        # Terminal output
        input_t = resp_usage.get("input_tokens", 0)
        cache_r = resp_usage.get("cache_read_input_tokens", 0)
        cache_w = resp_usage.get("cache_creation_input_tokens", 0)
        output_t = resp_usage.get("output_tokens", 0)
        total_in = input_t + cache_r + cache_w
        cache_ratio = cache_r / (cache_r + cache_w) if (cache_r + cache_w) > 0 else 0

        self.server.print_line(
            request_id, self.command, self.path, resp_model,
            total_in, output_t, _calc_cost(resp_usage, resp_model),
            latency_ms, req_bytes=len(body), resp_bytes=len(buffer),
            stop_reason=stop_reason, block_types=block_types,
            tool_names=tool_names, cache_ratio=cache_ratio,
            session_label=session_label,
            is_new_agent=is_new_agent,
            is_compaction=is_compaction, status=resp.status,
        )

    def _clean_headers(self, headers):
        """Remove sensitive headers for logging."""
        if self.server.redact_keys:
            return {k: v for k, v in headers.items()
                    if k.lower() not in REDACT_HEADERS}
        return headers

    def log_message(self, format, *args):
        """Suppress default HTTP server log output."""
        pass


# ── Sniffer Server ───────────────────────────────────────────────────

class SnifferServer(http.server.ThreadingHTTPServer):
    """Threaded HTTP server with logging and state tracking."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, log_path, full_bodies=False, redact_keys=True,
                 quiet=False, upstream=None):
        super().__init__(("127.0.0.1", port), SnifferHandler)
        self.port = port
        self.log_path = log_path
        self.upstream = upstream or parse_upstream(None)
        self.full_bodies = full_bodies
        self.redact_keys = redact_keys
        self.quiet = quiet
        self.compaction_detector = CompactionDetector()
        self.session_tracker = SessionTracker()

        self._lock = threading.Lock()
        self._counter = 0
        self._total_cost = 0.0
        self._total_in = 0
        self._total_out = 0
        self._total_req_bytes = 0
        self._total_resp_bytes = 0
        self._tool_counts = {}  # tool_name -> count

        # Open log file with restricted permissions
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._log_file = os.fdopen(fd, "w")

    def next_id(self):
        with self._lock:
            self._counter += 1
            return self._counter

    def write_log(self, entry):
        with self._lock:
            self._log_file.write(json.dumps(entry, separators=(",", ":")) + "\n")
            self._log_file.flush()

    def print_line(self, req_id, method, path, model, total_in, total_out,
                   cost, latency_ms, req_bytes=0, resp_bytes=0,
                   stop_reason="", block_types=None, tool_names=None,
                   cache_ratio=0, session_label="", is_new_agent=False,
                   error=None, is_compaction=False, status=200):
        """Print one-line summary to terminal."""
        if self.quiet:
            return

        # Shorten model name
        short_model = model
        for prefix in ("claude-", "anthropic-"):
            short_model = short_model.replace(prefix, "")
        # Remove date suffix like -20260301
        parts = short_model.rsplit("-", 1)
        if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
            short_model = parts[0]

        traffic = f"  {DIM}{_format_bytes(req_bytes)}/{_format_bytes(resp_bytes)}{RESET}"

        # Content block type abbreviations
        _BLOCK_ABBREV = {
            "thinking": "T",
            "text": "t",
            "tool_use": "U",
            "server_tool_use": "S",
            "web_search_tool_result": "W",
            "mcp_tool_use": "M",
            "mcp_tool_result": "m",
        }
        block_abbrevs = []
        for bt in (block_types or []):
            block_abbrevs.append(_BLOCK_ABBREV.get(bt, "?"))
        blocks_str = f"  {DIM}[{''.join(block_abbrevs)}]{RESET}" if block_abbrevs else ""

        # Cache ratio
        if total_in == 0:
            cache_str = ""
        elif cache_ratio == 0:
            cache_str = f"  {RED}0%c{RESET}"
        else:
            cache_str = f"  {DIM}{cache_ratio:.0%}c{RESET}"

        # Stop reason tag
        stop_str = ""
        if stop_reason == "max_tokens":
            stop_str = f"  {RED}max_tokens{RESET}"
        elif stop_reason == "tool_use" and tool_names:
            stop_str = f"  {DIM}{','.join(tool_names)}{RESET}"
        elif stop_reason == "tool_use":
            stop_str = f"  {DIM}tool{RESET}"

        # Session label
        session_str = ""
        if is_new_agent:
            session_str = f"  {CYAN}{BOLD}+{session_label}{RESET}"
        elif session_label and session_label != "main":
            session_str = f"  {CYAN}{session_label}{RESET}"

        with self._lock:
            self._total_cost += cost
            self._total_in += total_in
            self._total_out += total_out
            self._total_req_bytes += req_bytes
            self._total_resp_bytes += resp_bytes
            for tn in (tool_names or []):
                self._tool_counts[tn] = self._tool_counts.get(tn, 0) + 1

            if error:
                print(f"  {RED}#{req_id:<3}{RESET} {method} {path}  "
                      f"{RED}ERROR: {error[:60]}{RESET}")
            elif status >= 400:
                print(f"  {RED}#{req_id:<3}{RESET} {method} {path}  "
                      f"{short_model}  {RED}{status}{RESET}  "
                      f"{latency_ms:.0f}ms")
            else:
                compact_tag = f"  {YELLOW}compaction{RESET}" if is_compaction else ""
                print(f"  {GREEN}#{req_id:<3}{RESET} {method} {path}  "
                      f"{CYAN}{short_model}{RESET}  "
                      f"{_format_tokens(total_in)}{DIM}->{RESET}{_format_tokens(total_out)}  "
                      f"{DIM}${cost:.3f}{RESET}  "
                      f"{DIM}{latency_ms:.0f}ms{RESET}"
                      f"{traffic}"
                      f"{cache_str}"
                      f"{blocks_str}"
                      f"{stop_str}"
                      f"{session_str}"
                      f"{compact_tag}")

    def print_summary(self):
        """Print final summary on shutdown."""
        if self.quiet:
            return
        with self._lock:
            print()
            agents = self.session_tracker.agent_count
            agent_str = f"  {DIM}|{RESET}  {agents} sub-agent{'s' if agents != 1 else ''}" if agents else ""
            print(f"  {BOLD}Summary:{RESET} {self._counter} requests  "
                  f"{DIM}|{RESET}  ${self._total_cost:.3f}  "
                  f"{DIM}|{RESET}  {_format_tokens(self._total_in)} in  "
                  f"{DIM}|{RESET}  {_format_tokens(self._total_out)} out  "
                  f"{DIM}|{RESET}  {_format_bytes(self._total_req_bytes)} sent  "
                  f"{DIM}|{RESET}  {_format_bytes(self._total_resp_bytes)} recv"
                  f"{agent_str}")
            # Activity: tool breakdown
            if self._tool_counts:
                top = sorted(self._tool_counts.items(), key=lambda x: -x[1])
                parts = [f"{name}:{count}" for name, count in top]
                print(f"  {BOLD}Activity:{RESET} {DIM}{'  '.join(parts)}{RESET}")
            print(f"  {DIM}Log: {self.log_path}{RESET}")
            print()

    def close(self):
        self._log_file.close()


# ── Main ─────────────────────────────────────────────────────────────

def _env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_upstream(args):
    """Pick the upstream from --upstream / env / default, in that order.

    Returns (upstream, source_label, notes). Exits on an unusable upstream.
    """
    if args.upstream:
        url, src = args.upstream, "--upstream"
    elif os.environ.get(UPSTREAM_ENV):
        url, src = os.environ[UPSTREAM_ENV], UPSTREAM_ENV
    elif os.environ.get("ANTHROPIC_BASE_URL"):
        # Inherited from the shell — sniff a gateway with no extra flags
        url, src = os.environ["ANTHROPIC_BASE_URL"], "ANTHROPIC_BASE_URL"
    else:
        url, src = None, ""

    try:
        upstream = parse_upstream(url)
    except ValueError as e:
        origin = src or "default"
        print(f"  {RED}Invalid upstream ({origin}): {e}{RESET}")
        sys.exit(1)

    notes = []

    # Forwarding to our own listen port would bounce every request back here
    if upstream.points_at_port(args.port):
        if src == "ANTHROPIC_BASE_URL":
            notes.append(f"  {DIM}Ignoring ANTHROPIC_BASE_URL={url} "
                         f"— points at this sniffer{RESET}")
            upstream = parse_upstream(None)
            src = ""
        else:
            print(f"  {RED}Upstream {upstream.base_url} points at this sniffer "
                  f"— requests would loop.{RESET}")
            sys.exit(1)

    # Gateway-only knobs. They never apply to Anthropic: an exported
    # CLAUDETUI_UPSTREAM_* left over from a gateway session must not disable TLS
    # checks on, or leak an internal token to, api.anthropic.com.
    insecure = args.insecure or _env_flag(UPSTREAM_INSECURE_ENV)
    token = os.environ.get(UPSTREAM_TOKEN_ENV) or None

    if upstream.base_url == DEFAULT_UPSTREAM:
        if args.insecure:
            print(f"  {RED}--insecure applies to a custom --upstream only, "
                  f"not to {DEFAULT_UPSTREAM}.{RESET}")
            sys.exit(1)
        ignored = []
        if insecure:
            ignored.append(UPSTREAM_INSECURE_ENV)
        if token:
            ignored.append(UPSTREAM_TOKEN_ENV)
        if ignored:
            notes.append(f"  {DIM}Ignoring {' and '.join(ignored)} "
                         f"— upstream is {DEFAULT_UPSTREAM}{RESET}")
    else:
        upstream.insecure = insecure
        upstream.token = token
        if insecure and upstream.scheme == "https":
            notes.append(f"  {YELLOW}TLS verification disabled for the upstream{RESET}")
        if token:
            notes.append(f"  {DIM}Injecting bearer token from {UPSTREAM_TOKEN_ENV}{RESET}")

    return upstream, src, notes


def main():
    parser = argparse.ArgumentParser(
        prog="claudetui sniffer",
        description="Intercept and log Claude Code API calls.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Proxy port (default: {DEFAULT_PORT})")
    parser.add_argument("--full", action="store_true",
                        help="Log complete request/response bodies (large files)")
    parser.add_argument("--no-redact", action="store_true",
                        help="Don't redact API keys from logs")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress terminal output")
    parser.add_argument("--upstream", metavar="URL",
                        help=f"Upstream API base URL (default: {DEFAULT_UPSTREAM}; "
                             f"env: {UPSTREAM_ENV}, else ANTHROPIC_BASE_URL)")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS verification for the upstream (self-signed gateways)")
    args = parser.parse_args()

    upstream, upstream_src, notes = _resolve_upstream(args)

    # Create log directory
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"sniffer-{timestamp}.jsonl"

    # Warn about --no-redact
    if args.no_redact:
        print(f"  {RED}{BOLD}WARNING:{RESET} API keys will be written to log files!")
        print()

    # Start server
    try:
        server = SnifferServer(
            port=args.port,
            log_path=log_path,
            full_bodies=args.full,
            redact_keys=not args.no_redact,
            quiet=args.quiet,
            upstream=upstream,
        )
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"  {RED}Port {args.port} is already in use.{RESET}")
            print(f"  Try: claudetui sniffer --port {args.port + 1}")
            sys.exit(1)
        raise

    # Write port file so other tools can discover the sniffer
    port_file = PORT_DIR / f".port.{args.port}"
    fd = os.open(str(port_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(str(args.port))

    if not args.quiet:
        print()
        for claude_part, tui_part in LOGO_LINES:
            print(claude_part + tui_part)
        print()
        print(f"  {BOLD}API Sniffer{RESET} {DIM}— listening on "
              f"http://127.0.0.1:{args.port}{RESET}")
        if upstream.base_url != DEFAULT_UPSTREAM:
            src = f" {DIM}(via {upstream_src}){RESET}" if upstream_src else ""
            print(f"  {DIM}forwarding to{RESET} {CYAN}{upstream.base_url}{RESET}{src}")
        print()
        for note in notes:
            print(note)
        if notes:
            print()
        print(f"  {BOLD}Use:{RESET}  "
              f"{CYAN}ANTHROPIC_BASE_URL=http://localhost:{args.port} claude{RESET}")
        print(f"  {BOLD}Log:{RESET}  {DIM}{log_path}{RESET}")
        print()
        if args.full:
            print(f"  {YELLOW}Full body logging enabled — log files may be large{RESET}")
            print()

    # Handle Ctrl+C gracefully — run cleanup in a thread to avoid
    # deadlocking on threading locks held during request processing
    def shutdown(sig, frame):
        def _do_shutdown():
            if not args.quiet:
                print(f"\n  {DIM}Shutting down...{RESET}")
            port_file.unlink(missing_ok=True)
            server.print_summary()
            server.close()
            os._exit(0)
        threading.Thread(target=_do_shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.serve_forever()


if __name__ == "__main__":
    main()
