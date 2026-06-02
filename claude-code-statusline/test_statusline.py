#!/usr/bin/env python3
"""Comprehensive tests for statusline SRP modules."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import statusline
from statusline_core import api_clients, calculations, git_info, render, transcript
from statusline_core.constants import DEFAULT_CONTEXT_LIMIT


def _write_jsonl(path, rows):
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


class TestTranscriptParsing(unittest.TestCase):
    def test_parse_input_data(self):
        data = {
            "model": {"display_name": "Sonnet", "id": "claude-sonnet-4-6"},
            "workspace": {"current_dir": "/tmp/project"},
            "transcript_path": "/tmp/t.jsonl",
            "session_id": "abcdef123456",
            "context_window": {
                "total_input_tokens": 12345,
                "context_window_size": 1_000_000,
                "used_percentage": 42.5,
            },
            "cost": {"total_cost_usd": 1.23, "total_duration_ms": 125000},
            "rate_limits": {
                "five_hour": {"used_percentage": 25, "resets_at": 4102444800},
                "seven_day": {"used_percentage": 75, "resets_at": 4102444800},
            },
        }
        parsed = transcript.parse_input_data(data)
        self.assertEqual(parsed["model"], "Sonnet")
        self.assertEqual(parsed["cwd"], "project")
        self.assertEqual(parsed["session_id"], "abcdef12")
        self.assertEqual(parsed["context_tokens"], 12345)
        self.assertEqual(parsed["context_limit"], 1_000_000)
        self.assertEqual(parsed["used_percentage"], 42.5)
        self.assertEqual(parsed["cost_usd"], 1.23)
        self.assertEqual(parsed["duration_ms"], 125000)
        self.assertEqual(parsed["usage"]["five_hour"]["utilization"], 25)
        self.assertTrue(parsed["usage"]["five_hour"]["resets_at"])

    def test_parse_input_data_defaults_when_fields_absent(self):
        parsed = transcript.parse_input_data(
            {"model": {"display_name": "Opus", "id": "claude-opus-4-8"}}
        )
        self.assertEqual(parsed["context_tokens"], 0)
        self.assertEqual(parsed["context_limit"], DEFAULT_CONTEXT_LIMIT)
        self.assertIsNone(parsed["used_percentage"])
        self.assertEqual(parsed["cost_usd"], 0.0)
        self.assertEqual(parsed["duration_ms"], 0)
        self.assertIsNone(parsed["usage"])

    def test_parse_transcript_metrics_and_tools(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "t.jsonl")
            rows = [
                {
                    "type": "user",
                    "timestamp": "2026-04-07T00:00:00Z",
                    "message": {"content": [{"type": "text", "text": "hi"}]},
                },
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 100,
                            "cache_creation_input_tokens": 10,
                            "cache_read_input_tokens": 40,
                            "output_tokens": 20,
                        },
                        "content": [
                            {"type": "thinking", "text": "..."},
                            {
                                "type": "tool_use",
                                "name": "Edit",
                                "input": {"path": "/tmp/a.py"},
                            },
                            {
                                "type": "tool_use",
                                "id": "task_1",
                                "name": "Task",
                                "input": {},
                            },
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "is_error": True}]},
                },
                {"type": "system", "subtype": "compact_boundary"},
            ]
            _write_jsonl(p, rows)
            m = transcript.parse_transcript(p, context_limit=200_000)
            self.assertEqual(m["turn_count"], 1)
            self.assertEqual(m["tool_calls"], 2)
            self.assertEqual(m["tool_errors"], 1)
            self.assertEqual(m["compact_count"], 1)
            self.assertEqual(m["thinking_count"], 1)
            self.assertIn("/tmp/a.py", m["files_touched"])
            self.assertEqual(m["subagent_count"], 1)

    def test_compaction_prediction_and_efficiency(self):
        metrics = {
            "context_per_turn": [(1, 1000), (2, 1300), (3, 1700)],
            "context_at_last_compact": 900,
            "total_context_built": 10000,
            "tokens_wasted": 1000,
        }
        pred = calculations.calculate_compaction_prediction(
            1700, 200_000, 3, metrics, ratio=0.2
        )
        self.assertIn("ETA", pred)
        with mock.patch("statusline_core.calculations.is_visible", return_value=True):
            eff = calculations.calculate_efficiency(metrics, 2000)
        self.assertIn("eff", eff)

    def test_context_metrics_prefers_stdin_percentage(self):
        # stdin used_percentage wins over tokens/limit when present
        m = calculations.calculate_context_metrics(999, 200_000, used_percentage=42.5)
        self.assertAlmostEqual(m["ratio"], 0.425)
        # falls back to tokens/limit when absent
        m2 = calculations.calculate_context_metrics(50_000, 200_000)
        self.assertAlmostEqual(m2["ratio"], 0.25)


class TestFormattingAndRender(unittest.TestCase):
    def test_token_cost_duration_format(self):
        from claude_tui_components.utils import format_tokens
        self.assertEqual(format_tokens(1200), "1.2k")
        self.assertEqual(calculations.format_cost(0.001), "<$0.01")
        self.assertEqual(calculations.format_duration_ms(0), "0m")
        self.assertEqual(calculations.format_duration_ms(125000), "2m")
        self.assertEqual(calculations.format_duration_ms(3_660_000), "1h 01m")
        self.assertEqual(calculations.format_duration_ms(None), "0m")
        self.assertEqual(calculations.format_duration_ms(-125000), "0m")

    def test_cache_ratio_and_part(self):
        metrics = {"input_tokens_total": 100, "cache_read_tokens_total": 100}
        pct, color = calculations.calculate_cache_ratio(metrics)
        part = calculations.format_cache_part(pct, color)
        self.assertIn("cache", part)

    def test_progress_bar_and_sparkline(self):
        bar = render.build_progress_bar(0.5, length=10, threshold=0.8)
        self.assertIn("%", bar)
        sp = render.build_sparkline([10, 20, None, 5], width=8)
        self.assertTrue(len(sp) > 0)

    def test_wrap_and_terminal_width(self):
        lines = render.wrap_line_parts(
            ["a", "b", "c"],
            ["file.py"],
            max_width=6,
        )
        self.assertTrue(len(lines) >= 1)
        self.assertIsInstance(render.calculate_terminal_width(), int)

    def test_line_builders(self):
        metrics = {
            "compact_count": 1,
            "turn_count": 2,
            "files_touched": {"a.py"},
            "tool_errors": 0,
            "thinking_count": 1,
            "subagent_count": 1,
            "recent_tools": ["Edit a.py"],
            "current_turn_file_edits": {"a.py": 2},
        }
        from statusline_core.display_state import DisplayState
        ds = DisplayState(
            model="Sonnet",
            session_id="abcd1234",
            cwd="proj",
            bar="bar",
            tokens_str="1k",
            limit_str="200k",
            metrics=metrics,
            usage={"five_hour": {"utilization": 10, "resets_at": ""}},
            compact_prediction="ETA 10 turns",
            sparkline_part="spark",
            cost_str="$1.00",
            duration_str="10m",
            efficiency_part="90% eff",
            branch_part="main",
            cache_part="80% cache",
            cache_pct=80,
            cost_per_turn="~$0.50/turn",
            bar_length=20,
        )
        with mock.patch("statusline_core.render.is_visible", return_value=True):
            l1 = render.build_line1_parts(ds)
            l2 = render.build_line2_parts(ds)
            ds_l3 = DisplayState(
                metrics=metrics,
                usage={"seven_day": {"utilization": 50, "resets_at": ""}},
                bar_length=20,
            )
            l3 = render.build_line3_parts(ds_l3)
            compact = render.build_compact_line(ds)
        self.assertTrue(len(l1) > 0)
        self.assertTrue(len(l2) > 0)
        self.assertIsInstance(l3, list)
        self.assertTrue(isinstance(compact, str))


class TestApiClientFormatting(unittest.TestCase):
    def test_api_status_formatting(self):
        data = {
            "status": "none",
            "components": {"Claude Code API": "major_outage"},
            "incidents": [],
        }
        with mock.patch("claude_tui_core.formatting.get_setting", return_value=False):
            out = api_clients.format_api_status(data)
        self.assertIn("outage", out)

    def test_usage_bar_formatters(self):
        usage = {
            "five_hour": {"utilization": 25, "resets_at": "2099-01-01T00:00:00Z"},
            "seven_day": {"utilization": 75, "resets_at": "2099-01-02T00:00:00Z"},
        }
        s = api_clients.format_usage_session(usage)
        w = api_clients.format_usage_weekly(usage)
        self.assertIn("%", s)
        self.assertIn("W ", w)

    def test_fetch_short_circuit_when_disabled(self):
        with mock.patch("claude_tui_core.network.get_setting", return_value=False):
            self.assertIsNone(api_clients.fetch_usage())
            self.assertIsNone(api_clients.fetch_api_status())


class TestGitInfo(unittest.TestCase):
    def test_git_helpers_fail_closed(self):
        with mock.patch("subprocess.run", side_effect=RuntimeError("boom")):
            self.assertEqual(git_info.get_git_branch(), "")
            self.assertEqual(git_info.get_git_diff_stat(), "")


class TestEntrypointIntegration(unittest.TestCase):
    def _minimal_payload(self, transcript_path):
        return {
            "model": {"display_name": "Sonnet", "id": "claude-sonnet-4-6"},
            "workspace": {"current_dir": "/tmp/proj"},
            "transcript_path": transcript_path,
            "session_id": "abcdef123456",
            "context_window": {
                "total_input_tokens": 5000,
                "context_window_size": 200_000,
            },
            "cost": {"total_cost_usd": 0.5, "total_duration_ms": 60000},
        }

    def _write_min_transcript(self):
        td = tempfile.TemporaryDirectory()
        p = os.path.join(td.name, "session.jsonl")
        _write_jsonl(
            p,
            [
                {
                    "type": "user",
                    "timestamp": "2026-04-07T00:00:00Z",
                    "message": {"content": [{"type": "text", "text": "hi"}]},
                },
                {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 100,
                            "cache_creation_input_tokens": 10,
                            "cache_read_input_tokens": 40,
                            "output_tokens": 20,
                        },
                        "content": [],
                    },
                },
            ],
        )
        return td, p

    def test_main_compact_mode_outputs_line(self):
        td, p = self._write_min_transcript()
        self.addCleanup(td.cleanup)
        payload = self._minimal_payload(p)
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["statusline.py", "--compact"]), mock.patch(
            "sys.stdin", io.StringIO(json.dumps(payload))
        ), mock.patch(
            "statusline.fetch_api_status", return_value=None
        ), mock.patch(
            "statusline.format_api_status", return_value=""
        ), redirect_stdout(
            out
        ):
            statusline.main()
        self.assertTrue(out.getvalue().strip())

    def test_main_full_mode_outputs_lines(self):
        td, p = self._write_min_transcript()
        self.addCleanup(td.cleanup)
        payload = self._minimal_payload(p)
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["statusline.py"]), mock.patch(
            "sys.stdin", io.StringIO(json.dumps(payload))
        ), mock.patch(
            "statusline.fetch_api_status", return_value=None
        ), mock.patch(
            "statusline.format_api_status", return_value=""
        ), mock.patch.dict(
            os.environ, {"STATUSLINE_WIDGET": "none"}, clear=False
        ), redirect_stdout(
            out
        ):
            statusline.main()
        self.assertTrue(len(out.getvalue().splitlines()) >= 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
