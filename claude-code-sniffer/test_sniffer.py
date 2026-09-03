#!/usr/bin/env python3
"""Tests for sniffer.py — covers pure functions and stateful trackers.

Usage: python3 claude-code-sniffer/test_sniffer.py -v
"""
import argparse
import contextlib
import http.client
import io
import json
import os
import ssl
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sniffer import (
    _match_pricing, _format_tokens, _format_bytes, _calc_cost,
    _summarize_request, _reassemble_sse, _extract_session_id,
    SessionTracker, CompactionDetector, MODEL_PRICING,
    parse_upstream, DEFAULT_UPSTREAM, _resolve_upstream,
    UPSTREAM_ENV, UPSTREAM_TOKEN_ENV, UPSTREAM_INSECURE_ENV,
)


# ── Format helpers ────────────────────────────────────────────────

class TestFormatTokens(unittest.TestCase):
    def test_small(self):
        self.assertEqual(_format_tokens(0), "0")
        self.assertEqual(_format_tokens(999), "999")

    def test_thousands(self):
        self.assertEqual(_format_tokens(1_000), "1.0k")
        self.assertEqual(_format_tokens(45_200), "45.2k")

    def test_millions(self):
        self.assertEqual(_format_tokens(1_000_000), "1.0M")
        self.assertEqual(_format_tokens(1_500_000), "1.5M")


class TestFormatBytes(unittest.TestCase):
    def test_small(self):
        self.assertEqual(_format_bytes(0), "0B")
        self.assertEqual(_format_bytes(999), "999B")

    def test_kilobytes(self):
        self.assertEqual(_format_bytes(1_000), "1.0KB")
        self.assertEqual(_format_bytes(484_500), "484.5KB")

    def test_megabytes(self):
        self.assertEqual(_format_bytes(1_000_000), "1.0MB")
        self.assertEqual(_format_bytes(2_500_000), "2.5MB")


# ── Pricing ───────────────────────────────────────────────────────

class TestMatchPricing(unittest.TestCase):
    def test_opus(self):
        p = _match_pricing("claude-opus-4-6-20260301")
        self.assertEqual(p["input"], 5.0)

    def test_sonnet(self):
        p = _match_pricing("claude-sonnet-4-6")
        self.assertEqual(p["input"], 3.0)

    def test_haiku(self):
        p = _match_pricing("claude-haiku-4-5-20251001")
        self.assertEqual(p["input"], 1.0)

    def test_fable(self):
        p = _match_pricing("claude-fable-5")
        self.assertEqual(p["input"], 10.0)
        self.assertEqual(p["output"], 50.0)

    def test_opus_4_8(self):
        p = _match_pricing("claude-opus-4-8")
        self.assertEqual(p["input"], 5.0)
        self.assertEqual(p["output"], 25.0)

    def test_unknown_defaults_to_sonnet(self):
        p = _match_pricing("unknown-model")
        self.assertEqual(p, MODEL_PRICING["claude-sonnet-4-6"])

    def test_empty_defaults_to_sonnet(self):
        p = _match_pricing("")
        self.assertEqual(p, MODEL_PRICING["claude-sonnet-4-6"])


class TestCalcCost(unittest.TestCase):
    def test_basic(self):
        usage = {
            "input_tokens": 1000,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 100,
        }
        cost = _calc_cost(usage, "claude-sonnet-4-6")
        # 1000 * 3.0/1M + 100 * 15.0/1M = 0.003 + 0.0015
        self.assertAlmostEqual(cost, 0.0045, places=5)

    def test_with_cache(self):
        usage = {
            "input_tokens": 0,
            "cache_read_input_tokens": 100_000,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
        }
        cost = _calc_cost(usage, "claude-opus-4-6")
        # 100k * 0.5/1M = 0.05
        self.assertAlmostEqual(cost, 0.05, places=5)

    def test_empty_usage(self):
        self.assertEqual(_calc_cost({}, "claude-opus-4-6"), 0.0)


# ── Request summarizer ────────────────────────────────────────────

class TestSummarizeRequest(unittest.TestCase):
    def _make_body(self, **kwargs):
        body = {
            "model": "claude-opus-4-6",
            "max_tokens": 16384,
            "stream": True,
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": "Read"}, {"name": "Agent"}],
        }
        body.update(kwargs)
        return json.dumps(body).encode()

    def test_basic_summary(self):
        s = _summarize_request(self._make_body())
        self.assertEqual(s["model"], "claude-opus-4-6")
        self.assertEqual(s["message_count"], 1)
        self.assertEqual(s["tool_count"], 2)
        self.assertIn("Agent", s["tool_names"])
        self.assertIn("Read", s["tool_names"])
        self.assertEqual(s["system_length"], len("You are helpful."))
        self.assertGreater(s["body_length"], 0)

    def test_system_as_list(self):
        system = [{"type": "text", "text": "Hello"}, {"type": "text", "text": "World"}]
        s = _summarize_request(self._make_body(system=system))
        self.assertEqual(s["system_length"], 10)  # "Hello" + "World"

    def test_no_tools(self):
        body = json.dumps({"model": "x", "messages": []}).encode()
        s = _summarize_request(body)
        self.assertNotIn("tool_names", s)
        self.assertNotIn("tool_count", s)

    def test_full_mode_returns_raw(self):
        raw = self._make_body()
        s = _summarize_request(raw, full=True)
        # full mode returns the raw parsed JSON, not a summary
        self.assertIn("messages", s)
        self.assertIn("tools", s)
        self.assertNotIn("tool_names", s)  # not summarized

    def test_invalid_json(self):
        s = _summarize_request(b"not json")
        self.assertIn("raw_length", s)

    def test_metadata_preserved(self):
        meta = {"user_id": "user_abc_session_12345678"}
        s = _summarize_request(self._make_body(metadata=meta))
        self.assertEqual(s["metadata"], meta)


# ── SSE reassembly ────────────────────────────────────────────────

class TestReassembleSSE(unittest.TestCase):
    def _make_sse(self, events):
        lines = []
        for evt in events:
            lines.append(f"data: {json.dumps(evt)}")
        return "\n".join(lines).encode()

    def test_basic_response(self):
        sse = self._make_sse([
            {"type": "message_start", "message": {
                "model": "claude-opus-4-6",
                "usage": {"input_tokens": 1000},
            }},
            {"type": "content_block_start", "content_block": {"type": "thinking"}},
            {"type": "content_block_start", "content_block": {"type": "text"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 500}},
        ])
        r = _reassemble_sse(sse)
        self.assertEqual(r["model"], "claude-opus-4-6")
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertEqual(r["usage"]["input_tokens"], 1000)
        self.assertEqual(r["usage"]["output_tokens"], 500)
        self.assertEqual(r["content_blocks"], ["thinking", "text"])
        self.assertEqual(r["event_count"], 4)

    def test_tool_use_captures_names(self):
        sse = self._make_sse([
            {"type": "message_start", "message": {"model": "x", "usage": {}}},
            {"type": "content_block_start", "content_block": {
                "type": "tool_use", "name": "Read"}},
            {"type": "content_block_start", "content_block": {
                "type": "tool_use", "name": "Edit"}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
        ])
        r = _reassemble_sse(sse)
        self.assertEqual(r["tool_names"], ["Read", "Edit"])
        self.assertEqual(r["content_blocks"], ["tool_use", "tool_use"])

    def test_empty_stream(self):
        r = _reassemble_sse(b"")
        self.assertEqual(r["model"], "")
        self.assertEqual(r["event_count"], 0)

    def test_server_tool_types(self):
        sse = self._make_sse([
            {"type": "message_start", "message": {"model": "x", "usage": {}}},
            {"type": "content_block_start", "content_block": {"type": "server_tool_use"}},
            {"type": "content_block_start", "content_block": {"type": "web_search_tool_result"}},
        ])
        r = _reassemble_sse(sse)
        self.assertEqual(r["content_blocks"], ["server_tool_use", "web_search_tool_result"])


# ── Session ID extraction ─────────────────────────────────────────

class TestExtractSessionId(unittest.TestCase):
    def test_valid(self):
        meta = {"user_id": "user_abc123_session_2fc49389-f2a2-49e0"}
        self.assertEqual(_extract_session_id(meta), "2fc49389")

    def test_no_session(self):
        self.assertEqual(_extract_session_id({"user_id": "user_abc"}), "")

    def test_none(self):
        self.assertEqual(_extract_session_id(None), "")

    def test_not_dict(self):
        self.assertEqual(_extract_session_id("string"), "")


# ── Session Tracker ───────────────────────────────────────────────

class TestSessionTracker(unittest.TestCase):
    def test_no_tools_returns_empty(self):
        t = SessionTracker()
        self.assertEqual(t.check([]), ("", False))
        self.assertEqual(t.check(None), ("", False))

    def test_main_session_detected(self):
        t = SessionTracker()
        label, is_new = t.check(["Read", "Edit", "Agent", "Bash"])
        self.assertEqual(label, "main")
        self.assertFalse(is_new)

    def test_sub_agent_detected(self):
        t = SessionTracker()
        # First: main session
        t.check(["Read", "Edit", "Agent"])
        # Sub-agent: no Agent tool
        label, is_new = t.check(["Read", "Bash", "Grep"],
                                system_length=3500, model="claude-haiku-4-5")
        self.assertEqual(label, "agent.1")
        self.assertTrue(is_new)

    def test_same_sub_agent_recognized(self):
        t = SessionTracker()
        t.check(["Agent", "Read"])  # main
        t.check(["Read", "Bash"], system_length=3500, model="claude-haiku-4-5")  # new agent
        label, is_new = t.check(["Read", "Bash"], system_length=3800, model="claude-haiku-4-5")
        self.assertEqual(label, "agent.1")
        self.assertFalse(is_new)  # same bucket

    def test_different_sub_agents(self):
        t = SessionTracker()
        t.check(["Agent", "Read"])  # main
        # Agent type 1: haiku explore (sys=3500)
        t.check(["Read", "Bash"], system_length=3500, model="claude-haiku-4-5")
        # Agent type 2: haiku websearch (sys=194)
        label, is_new = t.check(["web_search"], system_length=194, model="claude-haiku-4-5")
        self.assertEqual(label, "agent.2")
        self.assertTrue(is_new)

    def test_agent_count(self):
        t = SessionTracker()
        self.assertEqual(t.agent_count, 0)
        t.check(["Agent"])  # main
        self.assertEqual(t.agent_count, 0)
        t.check(["Read"], system_length=3000, model="haiku")
        self.assertEqual(t.agent_count, 1)
        t.check(["web_search"], system_length=200, model="haiku")
        self.assertEqual(t.agent_count, 2)

    def test_main_always_returns_main(self):
        t = SessionTracker()
        for _ in range(5):
            label, is_new = t.check(["Agent", "Read", "Edit"])
            self.assertEqual(label, "main")
            self.assertFalse(is_new)


# ── Compaction Detector ───────────────────────────────────────────

class TestCompactionDetector(unittest.TestCase):
    def test_ignores_sub_agents(self):
        d = CompactionDetector()
        # Main session: 300 msgs
        d.check({"message_count": 300, "body_length": 500_000},
                is_main_session=True, session_id="abc")
        # Sub-agent: 1 msg — should NOT trigger compaction
        result = d.check({"message_count": 1, "body_length": 5_000},
                         is_main_session=False, session_id="abc")
        self.assertFalse(result)

    def test_detects_message_count_drop(self):
        d = CompactionDetector()
        d.check({"message_count": 300, "body_length": 500_000},
                is_main_session=True, session_id="abc")
        result = d.check({"message_count": 3, "body_length": 50_000},
                         is_main_session=True, session_id="abc")
        self.assertTrue(result)

    def test_detects_body_length_drop(self):
        d = CompactionDetector()
        d.check({"message_count": 2, "body_length": 500_000},
                is_main_session=True, session_id="abc")
        result = d.check({"message_count": 2, "body_length": 50_000},
                         is_main_session=True, session_id="abc")
        self.assertTrue(result)

    def test_no_false_positive_on_first_request(self):
        d = CompactionDetector()
        result = d.check({"message_count": 1, "body_length": 5_000},
                         is_main_session=True, session_id="abc")
        self.assertFalse(result)

    def test_no_false_positive_growing_session(self):
        d = CompactionDetector()
        for msgs in range(1, 50):
            result = d.check({"message_count": msgs, "body_length": msgs * 5000},
                             is_main_session=True, session_id="abc")
            self.assertFalse(result, f"False positive at msgs={msgs}")

    def test_per_session_isolation(self):
        d = CompactionDetector()
        # Session A: 300 msgs
        d.check({"message_count": 300, "body_length": 500_000},
                is_main_session=True, session_id="aaa")
        # Session B: 1 msg (new session, not compaction)
        result = d.check({"message_count": 1, "body_length": 5_000},
                         is_main_session=True, session_id="bbb")
        self.assertFalse(result)

    def test_sub_agent_doesnt_update_state(self):
        d = CompactionDetector()
        d.check({"message_count": 300, "body_length": 500_000},
                is_main_session=True, session_id="abc")
        # Sub-agent with small body — ignored
        d.check({"message_count": 1, "body_length": 5_000},
                is_main_session=False, session_id="abc")
        # Main session continues growing — no compaction
        result = d.check({"message_count": 302, "body_length": 510_000},
                         is_main_session=True, session_id="abc")
        self.assertFalse(result)


# ── Upstream ──────────────────────────────────────────────────────

class TestParseUpstream(unittest.TestCase):
    def test_default(self):
        up = parse_upstream(None)
        self.assertEqual(up.scheme, "https")
        self.assertEqual(up.host, "api.anthropic.com")
        self.assertEqual(up.port, 443)
        self.assertEqual(up.prefix, "")
        self.assertEqual(up.base_url, DEFAULT_UPSTREAM)

    def test_blank_falls_back_to_default(self):
        self.assertEqual(parse_upstream("   ").base_url, DEFAULT_UPSTREAM)

    def test_plain_http_gateway(self):
        up = parse_upstream("http://localhost:4000")
        self.assertEqual(up.scheme, "http")
        self.assertEqual(up.host, "localhost")
        self.assertEqual(up.port, 4000)
        self.assertEqual(up.base_url, "http://localhost:4000")

    def test_default_ports_implied_by_scheme(self):
        self.assertEqual(parse_upstream("http://gw.corp").port, 80)
        self.assertEqual(parse_upstream("https://gw.corp").port, 443)

    def test_bare_host_assumes_https(self):
        up = parse_upstream("gw.corp:8443")
        self.assertEqual(up.scheme, "https")
        self.assertEqual(up.host, "gw.corp")
        self.assertEqual(up.port, 8443)

    def test_base_path_prefix(self):
        up = parse_upstream("https://gw.corp/anthropic/")
        self.assertEqual(up.prefix, "/anthropic")
        self.assertEqual(up.target_path("/v1/messages"), "/anthropic/v1/messages")

    def test_no_prefix_passes_path_through(self):
        up = parse_upstream("https://gw.corp")
        self.assertEqual(up.target_path("/v1/messages"), "/v1/messages")

    def test_rejects_bad_scheme(self):
        with self.assertRaises(ValueError):
            parse_upstream("ftp://gw.corp")

    def test_rejects_missing_host(self):
        with self.assertRaises(ValueError):
            parse_upstream("https:///v1")

    def test_rejects_query_string(self):
        # Silently dropping it would surface as an opaque upstream 401/404
        with self.assertRaises(ValueError):
            parse_upstream("https://gw.corp/v1?api-version=2024-01")

    def test_rejects_fragment(self):
        with self.assertRaises(ValueError):
            parse_upstream("https://gw.corp/v1#frag")

    def test_rejects_inline_credentials(self):
        with self.assertRaises(ValueError):
            parse_upstream("https://user:tok@gw.corp")

    def test_token_and_insecure_carried(self):
        up = parse_upstream("https://gw.corp", insecure=True, token="tok")
        self.assertTrue(up.insecure)
        self.assertEqual(up.token, "tok")


class TestUpstreamNetloc(unittest.TestCase):
    def test_omits_default_port(self):
        self.assertEqual(parse_upstream("https://gw.corp:443").netloc, "gw.corp")
        self.assertEqual(parse_upstream("http://gw.corp:80").netloc, "gw.corp")

    def test_keeps_non_default_port(self):
        self.assertEqual(parse_upstream("http://localhost:4000").netloc,
                         "localhost:4000")

    def test_ipv6_bracketed(self):
        up = parse_upstream("http://[::1]:4000")
        self.assertEqual(up.host, "::1")
        self.assertEqual(up.netloc, "[::1]:4000")


class TestUpstreamLoopGuard(unittest.TestCase):
    def test_same_loopback_port_is_self(self):
        self.assertTrue(parse_upstream("http://localhost:7735").points_at_port(7735))
        self.assertTrue(parse_upstream("http://127.0.0.1:7735").points_at_port(7735))
        self.assertTrue(parse_upstream("http://127.0.0.2:7735").points_at_port(7735))

    def test_other_port_is_not_self(self):
        self.assertFalse(parse_upstream("http://localhost:4000").points_at_port(7735))

    def test_remote_host_is_not_self(self):
        self.assertFalse(parse_upstream("https://gw.corp:7735").points_at_port(7735))


class TestUpstreamConnect(unittest.TestCase):
    def test_https_scheme_uses_tls_connection(self):
        conn = parse_upstream("https://gw.corp").connect()
        self.assertIsInstance(conn, http.client.HTTPSConnection)
        self.assertEqual(conn.port, 443)

    def test_http_scheme_uses_plain_connection(self):
        conn = parse_upstream("http://localhost:4000").connect()
        self.assertIsInstance(conn, http.client.HTTPConnection)
        self.assertNotIsInstance(conn, http.client.HTTPSConnection)
        self.assertEqual(conn.port, 4000)

    def test_insecure_disables_verification(self):
        conn = parse_upstream("https://gw.corp", insecure=True).connect()
        self.assertEqual(conn._context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(conn._context.check_hostname)


class TestResolveUpstream(unittest.TestCase):
    """Source precedence and the gateway-only knobs."""

    GATEWAY = "http://localhost:4000"

    def resolve(self, env=None, upstream=None, insecure=False, port=7735):
        args = argparse.Namespace(upstream=upstream, insecure=insecure, port=port)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=True):
            with contextlib.redirect_stdout(buf):
                result = _resolve_upstream(args)
        return result, buf.getvalue()

    def expect_exit(self, env=None, **kw):
        with self.assertRaises(SystemExit) as cm:
            self.resolve(env, **kw)
        return cm.exception.code

    # ── source precedence

    def test_default_when_nothing_set(self):
        (up, src, _), _out = self.resolve()
        self.assertEqual(up.base_url, DEFAULT_UPSTREAM)
        self.assertEqual(src, "")

    def test_flag_beats_env(self):
        (up, src, _), _out = self.resolve(
            {UPSTREAM_ENV: "https://env.example",
             "ANTHROPIC_BASE_URL": "https://inherited.example"},
            upstream=self.GATEWAY)
        self.assertEqual(up.base_url, self.GATEWAY)
        self.assertEqual(src, "--upstream")

    def test_own_env_beats_inherited_base_url(self):
        (up, src, _), _out = self.resolve(
            {UPSTREAM_ENV: self.GATEWAY,
             "ANTHROPIC_BASE_URL": "https://inherited.example"})
        self.assertEqual(up.base_url, self.GATEWAY)
        self.assertEqual(src, UPSTREAM_ENV)

    def test_inherits_anthropic_base_url(self):
        (up, src, _), _out = self.resolve({"ANTHROPIC_BASE_URL": self.GATEWAY})
        self.assertEqual(up.base_url, self.GATEWAY)
        self.assertEqual(src, "ANTHROPIC_BASE_URL")

    # ── loop guard

    def test_self_pointing_inherited_url_falls_back_to_default(self):
        (up, src, notes), _out = self.resolve(
            {"ANTHROPIC_BASE_URL": "http://localhost:7735"})
        self.assertEqual(up.base_url, DEFAULT_UPSTREAM)
        self.assertEqual(src, "")
        self.assertTrue(any("Ignoring ANTHROPIC_BASE_URL" in n for n in notes))

    def test_self_pointing_flag_exits(self):
        self.assertEqual(self.expect_exit(upstream="http://127.0.0.1:7735"), 1)

    def test_invalid_upstream_exits(self):
        self.assertEqual(self.expect_exit(upstream="ftp://gw.corp"), 1)

    # ── gateway-only knobs must not touch the Anthropic default

    def test_insecure_env_ignored_for_default_upstream(self):
        (up, _src, notes), _out = self.resolve({UPSTREAM_INSECURE_ENV: "1"})
        self.assertFalse(up.insecure)
        self.assertTrue(any(UPSTREAM_INSECURE_ENV in n for n in notes))

    def test_token_env_ignored_for_default_upstream(self):
        (up, _src, notes), _out = self.resolve({UPSTREAM_TOKEN_ENV: "internal-tok"})
        self.assertIsNone(up.token)
        self.assertTrue(any(UPSTREAM_TOKEN_ENV in n for n in notes))

    def test_knobs_ignored_when_self_pointing_url_falls_back(self):
        (up, _src, _notes), _out = self.resolve(
            {"ANTHROPIC_BASE_URL": "http://localhost:7735",
             UPSTREAM_TOKEN_ENV: "internal-tok",
             UPSTREAM_INSECURE_ENV: "1"})
        self.assertEqual(up.base_url, DEFAULT_UPSTREAM)
        self.assertIsNone(up.token)
        self.assertFalse(up.insecure)

    def test_insecure_flag_against_default_upstream_exits(self):
        self.assertEqual(self.expect_exit(insecure=True), 1)

    def test_explicit_anthropic_upstream_still_counts_as_default(self):
        (up, _src, _notes), _out = self.resolve(
            {UPSTREAM_TOKEN_ENV: "internal-tok"}, upstream=DEFAULT_UPSTREAM)
        self.assertIsNone(up.token)

    # ── gateway upstream does get them

    def test_knobs_apply_to_custom_upstream(self):
        (up, _src, notes), _out = self.resolve(
            {UPSTREAM_TOKEN_ENV: "gw-tok", UPSTREAM_INSECURE_ENV: "1"},
            upstream="https://gw.corp")
        self.assertEqual(up.token, "gw-tok")
        self.assertTrue(up.insecure)
        self.assertTrue(any("TLS verification disabled" in n for n in notes))

    def test_insecure_flag_applies_to_custom_upstream(self):
        (up, _src, _notes), _out = self.resolve(upstream="https://gw.corp",
                                                insecure=True)
        self.assertTrue(up.insecure)

    def test_insecure_env_needs_a_truthy_value(self):
        (up, _src, _notes), _out = self.resolve(
            {UPSTREAM_INSECURE_ENV: "0"}, upstream="https://gw.corp")
        self.assertFalse(up.insecure)


if __name__ == "__main__":
    unittest.main()
