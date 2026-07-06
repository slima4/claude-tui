#!/usr/bin/env python3
"""Tests for monitor.py — run before/after refactoring to verify no regressions.

Usage: python3 -m pytest claude-code-monitor/test_monitor.py -v
   or: python3 claude-code-monitor/test_monitor.py
"""
import json
import os
import re
import sys
import tempfile
import unittest

# Import from lib and chart modules directly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib import CONTEXT_LIMIT, DEFAULT_CONTEXT_LIMIT, get_context_limit, format_tokens, parse_transcript
from chart import _build_segments, _render_horizontal_chart, _render_vertical_chart

# Strip ANSI escape codes for assertion comparisons
def strip_ansi(s):
    return re.sub(r"\033\[[^m]*m", "", s)


# ── format_tokens ─────────────────────────────────────────────────────

class TestFormatTokens(unittest.TestCase):
    def test_millions(self):
        self.assertEqual(format_tokens(1_500_000), "1.5M")
        self.assertEqual(format_tokens(2_000_000), "2.0M")

    def test_thousands(self):
        self.assertEqual(format_tokens(167_000), "167.0k")
        self.assertEqual(format_tokens(33_100), "33.1k")
        self.assertEqual(format_tokens(1_000), "1.0k")

    def test_small(self):
        self.assertEqual(format_tokens(999), "999")
        self.assertEqual(format_tokens(0), "0")


# ── _build_segments ───────────────────────────────────────────────────

class TestBuildSegments(unittest.TestCase):
    SYS_PROMPT = 14_328  # typical system prompt size

    def test_no_compactions_active_segment(self):
        """Fresh session with no compactions — single active segment."""
        r = {
            "compact_events": [],
            "last_context": 80_000,
            "system_prompt_tokens": 0,
        }
        segs, n_comp = _build_segments(r)
        self.assertEqual(len(segs), 1)
        self.assertEqual(n_comp, 0)
        seg = segs[0]
        self.assertTrue(seg["active"])
        self.assertEqual(seg["peak"], 80_000)
        self.assertEqual(seg["useful"], 80_000)
        self.assertEqual(seg["system"], 0)
        self.assertEqual(seg["summary"], 0)
        self.assertEqual(seg["headroom"], 0)

    def test_no_compactions_zero_context(self):
        """No compactions, no context yet — no segments."""
        r = {
            "compact_events": [],
            "last_context": 0,
            "system_prompt_tokens": 0,
        }
        segs, n_comp = _build_segments(r)
        self.assertEqual(len(segs), 0)
        self.assertEqual(n_comp, 0)

    def test_one_compaction(self):
        """Single compaction: Seg 1 (completed) + Seg 2 (active)."""
        r = {
            "compact_events": [
                {"context_before": 167_000, "context_after": 33_100,
                 "system_prompt": self.SYS_PROMPT},
            ],
            "last_context": 80_000,
            "system_prompt_tokens": self.SYS_PROMPT,
        }
        segs, n_comp = _build_segments(r)

        # Two segments: one completed, one active
        self.assertEqual(len(segs), 2)
        self.assertEqual(n_comp, 1)

        # Seg 1: system prompt + useful (first segment, no summary)
        s1 = segs[0]
        self.assertEqual(s1["peak"], 167_000)
        self.assertEqual(s1["system"], self.SYS_PROMPT)
        self.assertEqual(s1["summary"], 0)
        self.assertEqual(s1["useful"], 167_000 - self.SYS_PROMPT)  # peak - system (no summary)
        self.assertEqual(s1["headroom"], CONTEXT_LIMIT - 167_000)
        self.assertNotIn("active", s1)

        self.assertEqual(n_comp, 1)

        # Seg 2: active, system + summary + useful
        s2 = segs[1]
        self.assertTrue(s2["active"])
        self.assertEqual(s2["peak"], 80_000)
        self.assertEqual(s2["system"], self.SYS_PROMPT)
        self.assertEqual(s2["summary"], 33_100 - self.SYS_PROMPT)  # 18.8k
        self.assertEqual(s2["useful"], 80_000 - 33_100)  # 46.9k
        self.assertEqual(s2["headroom"], 0)

    def test_two_compactions(self):
        """Two compactions: 3 segments."""
        r = {
            "compact_events": [
                {"context_before": 167_000, "context_after": 33_100,
                 "system_prompt": self.SYS_PROMPT},
                {"context_before": 147_500, "context_after": 24_900,
                 "system_prompt": self.SYS_PROMPT},
            ],
            "last_context": 50_000,
            "system_prompt_tokens": self.SYS_PROMPT,
        }
        segs, n_comp = _build_segments(r)
        self.assertEqual(len(segs), 3)
        self.assertEqual(n_comp, 2)

        # Seg 1: system + useful, no summary
        self.assertEqual(segs[0]["system"], self.SYS_PROMPT)
        self.assertEqual(segs[0]["summary"], 0)
        self.assertEqual(segs[0]["useful"], 167_000 - self.SYS_PROMPT)
        self.assertEqual(segs[0]["headroom"], CONTEXT_LIMIT - 167_000)

        # Seg 2: system + summary from compaction 1 + useful
        self.assertEqual(segs[1]["system"], self.SYS_PROMPT)
        self.assertEqual(segs[1]["summary"], 33_100 - self.SYS_PROMPT)
        self.assertEqual(segs[1]["useful"], 147_500 - 33_100)
        self.assertEqual(segs[1]["headroom"], CONTEXT_LIMIT - 147_500)

        # Seg 3: system + summary from compaction 2 + useful
        self.assertEqual(segs[2]["system"], self.SYS_PROMPT)
        self.assertEqual(segs[2]["summary"], 24_900 - self.SYS_PROMPT)
        self.assertEqual(segs[2]["useful"], 50_000 - 24_900)
        self.assertTrue(segs[2]["active"])

    def test_segment_useful_never_negative(self):
        """If current context <= rebuild, useful should be 0 not negative."""
        r = {
            "compact_events": [
                {"context_before": 167_000, "context_after": 33_100,
                 "system_prompt": self.SYS_PROMPT},
            ],
            "last_context": 33_100,  # exactly rebuild, no new work
            "system_prompt_tokens": self.SYS_PROMPT,
        }
        segs, _ = _build_segments(r)
        # useful = last_context - system - summary = 33100 - 14328 - 18772 = 0
        self.assertEqual(segs[1]["useful"], 0)

    def test_compaction_without_context_after(self):
        """Compaction event missing context_after — no active segment yet."""
        r = {
            "compact_events": [
                {"context_before": 167_000},  # no context_after — not resolved
            ],
            "last_context": 50_000,
            "system_prompt_tokens": self.SYS_PROMPT,
        }
        segs, n_comp = _build_segments(r)
        self.assertEqual(n_comp, 1)
        # Only Seg 1 (completed), no active segment — last_context is stale
        self.assertEqual(len(segs), 1)
        self.assertNotIn("active", segs[0])


# ── Waste model ───────────────────────────────────────────────────────

class TestWasteModel(unittest.TestCase):
    """Test the headroom + summary waste calculation."""
    SYS_PROMPT = 14_328

    def _compute_waste(self, compact_events, last_context):
        """Simulate waste calculation matching parse_transcript logic.

        Waste = headroom + summary (rebuild minus system prompt).
        System prompt is constant overhead, not compaction waste.
        """
        tokens_wasted = 0
        total_context_built = 0
        for evt in compact_events:
            pre = evt["context_before"]
            ctx_after = evt.get("context_after", 0)
            sys_prompt = evt.get("system_prompt", 0)
            if pre > 0:
                headroom = max(0, CONTEXT_LIMIT - pre)
                summary = max(0, ctx_after - sys_prompt)
                tokens_wasted += headroom + summary
            total_context_built += CONTEXT_LIMIT
        total_built = total_context_built + last_context
        eff = max(0, 1 - tokens_wasted / total_built) if total_built > 0 else 1.0
        return tokens_wasted, total_built, eff

    def test_no_compactions_100_percent(self):
        """Fresh session = 100% efficiency."""
        wasted, total, eff = self._compute_waste([], 80_000)
        self.assertEqual(wasted, 0)
        self.assertEqual(total, 80_000)
        self.assertAlmostEqual(eff, 1.0)

    def test_one_compaction(self):
        """Single compaction: waste = headroom + summary."""
        events = [{"context_before": 167_000, "context_after": 33_100,
                    "system_prompt": self.SYS_PROMPT}]
        wasted, total, eff = self._compute_waste(events, 80_000)
        expected_headroom = CONTEXT_LIMIT - 167_000  # 33k
        expected_summary = 33_100 - self.SYS_PROMPT  # 18.8k
        self.assertEqual(wasted, expected_headroom + expected_summary)  # ~51.8k
        self.assertEqual(total, CONTEXT_LIMIT + 80_000)  # 280k
        self.assertAlmostEqual(eff, 1 - wasted / 280_000, places=3)

    def test_two_compactions(self):
        """Two compactions: waste accumulates headroom + summary for each."""
        events = [
            {"context_before": 167_000, "context_after": 33_100,
             "system_prompt": self.SYS_PROMPT},
            {"context_before": 147_500, "context_after": 24_900,
             "system_prompt": self.SYS_PROMPT},
        ]
        wasted, total, eff = self._compute_waste(events, 50_000)
        h1 = CONTEXT_LIMIT - 167_000   # 33k
        s1 = 33_100 - self.SYS_PROMPT  # 18.8k
        h2 = CONTEXT_LIMIT - 147_500   # 52.5k
        s2 = 24_900 - self.SYS_PROMPT  # 10.6k
        self.assertEqual(wasted, h1 + s1 + h2 + s2)
        self.assertEqual(total, CONTEXT_LIMIT * 2 + 50_000)  # 450k
        self.assertAlmostEqual(eff, 1 - wasted / 450_000, places=3)

    def test_efficiency_bounds(self):
        """Efficiency should always be in [0, 1]."""
        events = [{"context_before": 190_000, "context_after": 5_000,
                    "system_prompt": self.SYS_PROMPT}]
        wasted, total, eff = self._compute_waste(events, 5_000)
        self.assertGreaterEqual(eff, 0)
        self.assertLessEqual(eff, 1)


# ── Chart rendering ───────────────────────────────────────────────────

class TestHorizontalChart(unittest.TestCase):
    SYS_PROMPT = 14_328

    def _get_lines(self, compact_events, last_context, width=100):
        r = {
            "compact_events": compact_events,
            "last_context": last_context,
            "system_prompt_tokens": self.SYS_PROMPT,
        }
        segs, n_comp = _build_segments(r)
        return [strip_ansi(l) for l in _render_horizontal_chart(segs, n_comp, width)]

    def test_header_and_legend(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100,
              "system_prompt": 14_328}],
            80_000,
        )
        self.assertTrue(any("HORIZONTAL" in l for l in lines))
        self.assertTrue(any("useful" in l and "summary" in l and "headroom" in l for l in lines))

    def test_segment_labels(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        self.assertTrue(any("Seg 1" in l for l in lines))
        self.assertTrue(any("Seg 2" in l for l in lines))

    def test_active_segment_marker(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        # Active segment should have → prefix
        self.assertTrue(any("→ Seg 2" in l for l in lines))

    def test_compaction_marker(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        self.assertTrue(any("compact #1" in l for l in lines))

    def test_detail_shows_compacted(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        self.assertTrue(any("compacted" in l for l in lines))

    def test_completed_segment_shows_200k(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        # Completed segment should show CONTEXT_LIMIT as total
        self.assertTrue(any("200.0k" in l and "Seg 1" in l for l in lines))

    def test_no_compactions(self):
        """Single active segment, no compactions — no crash."""
        r = {"compact_events": [], "last_context": 80_000, "system_prompt_tokens": 0}
        segs, n_comp = _build_segments(r)
        lines = _render_horizontal_chart(segs, n_comp, 100)
        clean = [strip_ansi(l) for l in lines]
        self.assertTrue(any("Seg 1" in l for l in clean))
        # No compaction markers
        self.assertFalse(any("compact #" in l for l in clean))

    def test_summary_shown_in_seg2(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100,
              "system_prompt": self.SYS_PROMPT}],
            80_000,
        )
        # Seg 2 detail should mention summary and system
        seg2_lines = []
        in_seg2 = False
        for l in lines:
            if "Seg 2" in l:
                in_seg2 = True
            elif "Seg " in l and "Seg 2" not in l:
                in_seg2 = False
            if in_seg2:
                seg2_lines.append(l)
        self.assertTrue(any("summary" in l for l in seg2_lines))
        self.assertTrue(any("system" in l for l in seg2_lines))

    def test_headroom_shown_for_completed(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        self.assertTrue(any("headroom" in l for l in lines))

    def test_no_headroom_for_active(self):
        """Active segment should not show headroom."""
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        # Find lines after active segment marker
        active_detail = []
        found_active = False
        for l in lines:
            if "→ Seg" in l:
                found_active = True
                continue
            if found_active and ("Seg " in l or "compact" in l):
                break
            if found_active:
                active_detail.append(l)
        # Active segment detail should not mention headroom
        self.assertFalse(any("headroom" in l for l in active_detail))


class TestVerticalChart(unittest.TestCase):
    SYS_PROMPT = 14_328

    def _get_lines(self, compact_events, last_context, width=80, height=30):
        r = {
            "compact_events": compact_events,
            "last_context": last_context,
            "system_prompt_tokens": self.SYS_PROMPT,
        }
        segs, n_comp = _build_segments(r)
        return [strip_ansi(l) for l in _render_vertical_chart(segs, n_comp, width, height)]

    def test_header_and_legend(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        self.assertTrue(any("VERTICAL" in l for l in lines))
        self.assertTrue(any("useful" in l and "summary" in l and "headroom" in l for l in lines))

    def test_y_axis_labels(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        self.assertTrue(any("200.0k" in l for l in lines))
        self.assertTrue(any("100.0k" in l for l in lines))
        self.assertTrue(any("0" in l for l in lines))

    def test_segment_labels(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        self.assertTrue(any("S1" in l for l in lines))
        # Active segment
        self.assertTrue(any("→2" in l for l in lines))

    def test_peak_values(self):
        lines = self._get_lines(
            [{"context_before": 167_000, "context_after": 33_100}],
            80_000,
        )
        self.assertTrue(any("167.0k" in l for l in lines))
        self.assertTrue(any("80.0k" in l for l in lines))

    def test_no_compactions(self):
        """Single active segment — no crash."""
        r = {"compact_events": [], "last_context": 80_000, "system_prompt_tokens": 0}
        segs, n_comp = _build_segments(r)
        lines = _render_vertical_chart(segs, n_comp, 80, 30)
        self.assertTrue(len(lines) > 0)


# ── parse_transcript with JSONL ───────────────────────────────────────

class TestParseTranscript(unittest.TestCase):
    def _write_jsonl(self, entries):
        """Write entries to a temp JSONL file, return path."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
        f.close()
        return f.name

    def _make_assistant_usage(self, input_t=0, cache_r=0, cache_c=0, output_t=0, turn=1):
        """Create an assistant message with usage stats."""
        return {
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": input_t,
                    "cache_read_input_tokens": cache_r,
                    "cache_creation_input_tokens": cache_c,
                    "output_tokens": output_t,
                },
                "content": [],
            },
            "turn_number": turn,
        }

    def _make_compaction(self):
        return {"type": "system", "subtype": "compact_boundary"}

    def _make_user_turn(self, turn=1):
        return {"type": "human", "turn_number": turn}

    def test_basic_no_compaction(self):
        """Simple session, no compactions — 100% efficiency."""
        entries = [
            self._make_user_turn(1),
            self._make_assistant_usage(input_t=50000, output_t=10000, turn=1),
            self._make_user_turn(2),
            self._make_assistant_usage(input_t=60000, output_t=15000, turn=2),
        ]
        path = self._write_jsonl(entries)
        try:
            r = parse_transcript(path)
            self.assertEqual(r["compact_count"], 0)
            self.assertEqual(r["tokens_wasted"], 0)
            self.assertEqual(r["total_context_built"], 0)
        finally:
            os.unlink(path)

    def test_compaction_waste_calculation(self):
        """Verify waste = headroom + summary after compaction.

        Summary = rebuild context minus system prompt (cache_read).
        System prompt is constant overhead, not compaction waste.
        """
        # Build up context to 167k, then compact, then resume
        # After compaction: cache_r=5000 (system prompt), rest is summary
        entries = [
            self._make_user_turn(1),
            self._make_assistant_usage(input_t=100000, output_t=67000, turn=1),
            # Compaction fires
            self._make_compaction(),
            # First response after compaction
            self._make_user_turn(2),
            self._make_assistant_usage(input_t=25000, cache_r=5000, output_t=3100, turn=2),
        ]
        path = self._write_jsonl(entries)
        try:
            r = parse_transcript(path)
            self.assertEqual(r["compact_count"], 1)
            # context_before = 100000+67000 = 167000
            # context_after = 25000+5000+3100 = 33100
            # system_prompt = cache_r = 5000
            # headroom = 200000 - 167000 = 33000
            # summary = 33100 - 5000 = 28100 (rebuild minus system prompt)
            # wasted = 33000 + 28100 = 61100
            self.assertEqual(r["tokens_wasted"], 61_100)
            self.assertEqual(r["system_prompt_tokens"], 5_000)
            self.assertEqual(r["total_context_built"], CONTEXT_LIMIT)
            # Verify compact event
            self.assertEqual(len(r["compact_events"]), 1)
            self.assertEqual(r["compact_events"][0]["context_before"], 167_000)
            self.assertEqual(r["compact_events"][0]["context_after"], 33_100)
            self.assertEqual(r["compact_events"][0]["system_prompt"], 5_000)
        finally:
            os.unlink(path)

    def test_two_compactions(self):
        """Two compactions accumulate waste correctly."""
        entries = [
            self._make_user_turn(1),
            self._make_assistant_usage(input_t=100000, output_t=67000, turn=1),
            self._make_compaction(),
            self._make_user_turn(2),
            self._make_assistant_usage(input_t=25000, cache_r=5000, output_t=3100, turn=2),
            # More work in segment 2
            self._make_user_turn(3),
            self._make_assistant_usage(input_t=90000, output_t=57500, turn=3),
            self._make_compaction(),
            self._make_user_turn(4),
            self._make_assistant_usage(input_t=20000, cache_r=3000, output_t=1900, turn=4),
        ]
        path = self._write_jsonl(entries)
        try:
            r = parse_transcript(path)
            self.assertEqual(r["compact_count"], 2)
            # Compaction 1: peak=167000, after=33100, system=5000
            # headroom1 = 33000, summary1 = 33100-5000 = 28100
            # Compaction 2: peak=147500, after=24900, system=3000
            # headroom2 = 52500, summary2 = 24900-3000 = 21900
            self.assertEqual(r["tokens_wasted"], (33_000 + 28_100) + (52_500 + 21_900))
            self.assertEqual(r["total_context_built"], CONTEXT_LIMIT * 2)
        finally:
            os.unlink(path)

    def test_last_context_tracked(self):
        """last_context reflects the final context snapshot."""
        entries = [
            self._make_user_turn(1),
            self._make_assistant_usage(input_t=40000, output_t=10000, turn=1),
            self._make_user_turn(2),
            self._make_assistant_usage(input_t=55000, output_t=15000, turn=2),
        ]
        path = self._write_jsonl(entries)
        try:
            r = parse_transcript(path)
            self.assertEqual(r["last_context"], 55000 + 15000)  # 70k
        finally:
            os.unlink(path)

    def test_line_counts_from_edit_and_write(self):
        """Edit and Write tool_use blocks should accumulate line counts."""
        entries = [
            {"type": "user", "message": {"content": "do stuff"}, "timestamp": "2025-01-01T00:00:00Z"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {
                                "file_path": "/tmp/foo.py",
                                "old_string": "line1\nline2",
                                "new_string": "new1\nnew2\nnew3",
                            },
                        },
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": "/tmp/bar.py",
                                "content": "a\nb\nc\nd",
                            },
                        },
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
                "timestamp": "2025-01-01T00:00:01Z",
            },
        ]
        path = self._write_jsonl(entries)
        try:
            r = parse_transcript(path)
            # Edit: old has 2 lines, new has 3 lines
            # Write: 4 lines (new file)
            self.assertEqual(r["lines_removed"], 2)  # "line1\nline2" = 2 lines
            self.assertEqual(r["lines_added"], 3 + 4)  # Edit new (3) + Write (4)
            self.assertEqual(r["files_created"], 1)
            self.assertEqual(r["files_edited"]["foo.py"], 1)
            self.assertEqual(r["files_edited"]["bar.py"], 1)
        finally:
            os.unlink(path)


# ── Context limit constants and model detection ──────────────────

class TestConstants(unittest.TestCase):
    def test_default_context_limit(self):
        self.assertEqual(DEFAULT_CONTEXT_LIMIT, 200_000)
        self.assertEqual(CONTEXT_LIMIT, 200_000)  # backward compat

    def test_get_context_limit_opus(self):
        self.assertEqual(get_context_limit("claude-opus-4-6"), 1_000_000)
        self.assertEqual(get_context_limit("claude-opus-4-6-20260301"), 1_000_000)
        self.assertEqual(get_context_limit("claude-opus-4-8"), 1_000_000)

    def test_get_context_limit_fable(self):
        self.assertEqual(get_context_limit("claude-fable-5"), 1_000_000)

    def test_get_context_limit_sonnet5(self):
        self.assertEqual(get_context_limit("claude-sonnet-5"), 1_000_000)

    def test_get_context_limit_mythos(self):
        self.assertEqual(get_context_limit("claude-mythos-5"), 1_000_000)
        self.assertEqual(get_context_limit("claude-mythos-preview"), 1_000_000)

    def test_get_context_limit_sonnet(self):
        self.assertEqual(get_context_limit("claude-sonnet-4-6"), 200_000)

    def test_get_context_limit_haiku(self):
        self.assertEqual(get_context_limit("claude-haiku-4-5-20251001"), 200_000)

    def test_get_context_limit_unknown(self):
        self.assertEqual(get_context_limit(""), 200_000)
        self.assertEqual(get_context_limit("some-other-model"), 200_000)


# ── parse_transcript required keys ────────────────────────────────

class TestParseTranscriptRequiredKeys(unittest.TestCase):
    """Ensure parse_transcript returns all keys the monitor dashboard needs.

    This test exists because a refactoring once replaced the full
    parse_transcript with a simplified version that was missing keys
    like 'waiting_for_response', causing KeyError at runtime.
    """

    # Keys the monitor dashboard reads from the parse result
    REQUIRED_KEYS = {
        # Session metadata
        "path", "model", "session_id", "start_time", "end_time",
        # Counters
        "turns", "responses", "compact_count", "tokens_wasted",
        "total_context_built", "thinking_count", "subagent_count",
        "skill_count", "tool_errors",
        # Token tracking
        "tokens", "context_history", "per_response", "context_per_turn",
        "last_context", "context_at_last_compact",
        # Compaction
        "compact_events", "turns_since_compact", "system_prompt_tokens",
        # Tool tracking
        "tool_counts", "tool_error_details", "files_read", "files_edited",
        "lines_added", "lines_removed", "files_created",
        "recent_tools", "last_error_msg",
        # Current turn state (dashboard CURRENT section)
        "turn_tool_counts", "turn_tool_errors",
        "turn_files_read", "turn_files_edited",
        "turn_thinking", "turn_agents_spawned",
        "turn_agents_pending", "turn_skill_active",
        # Turn timer
        "last_user_ts", "last_assist_ts", "waiting_for_response",
        # Context limit
        "context_limit",
        # Event log
        "event_log", "full_log",
    }

    def test_empty_file_has_all_keys(self):
        """Even an empty transcript must return all required keys."""
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        f.close()
        try:
            r = parse_transcript(f.name)
            missing = self.REQUIRED_KEYS - set(r.keys())
            self.assertEqual(missing, set(), f"Missing keys: {missing}")
        finally:
            os.unlink(f.name)

    def test_session_with_data_has_all_keys(self):
        """A real-ish session must return all required keys."""
        entries = [
            {"type": "user", "message": {"content": "hello"}, "timestamp": "2025-01-01T00:00:00Z"},
            {"type": "assistant", "message": {
                "model": "claude-sonnet-4-20250514",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {"input_tokens": 100, "output_tokens": 50,
                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            }, "timestamp": "2025-01-01T00:00:01Z"},
        ]
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
        f.close()
        try:
            r = parse_transcript(f.name)
            missing = self.REQUIRED_KEYS - set(r.keys())
            self.assertEqual(missing, set(), f"Missing keys: {missing}")
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
