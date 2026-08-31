import json
import os
import tempfile
import time
import unittest
from unittest import mock

import codex_console


class StatusCommandTests(unittest.TestCase):
    def test_usage_windows_are_classified_by_duration(self):
        usage = codex_console._fmt_usage({
            "limitId": "codex",
            "primary": {
                "usedPercent": 45,
                "windowDurationMins": 10_080,
                "resetsAt": 1_788_752_148,
            },
            "secondary": None,
        }, model="gpt-5.6-sol")

        self.assertNotIn("five_hour", usage)
        self.assertEqual(usage["seven_day"]["utilization"], 45)
        self.assertEqual(usage["seven_day"]["window_minutes"], 10_080)
        self.assertEqual(usage["limit_id"], "codex")

    def test_usage_selects_model_specific_limit_bucket(self):
        payload = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {"usedPercent": 45, "windowDurationMins": 10_080},
                "secondary": None,
            },
            "rateLimitsByLimitId": {
                "codex_bengalfox": {
                    "limitId": "codex_bengalfox",
                    "limitName": "GPT-5.3-Codex-Spark",
                    "primary": {"usedPercent": 90, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 86, "windowDurationMins": 10_080},
                },
            },
        }

        regular = codex_console._fmt_usage(payload, model="gpt-5.6-sol")
        spark = codex_console._fmt_usage(payload, model="gpt-5.3-codex-spark")

        self.assertEqual(regular["limit_id"], "codex")
        self.assertNotIn("five_hour", regular)
        self.assertEqual(regular["seven_day"]["utilization"], 45)
        self.assertEqual(spark["limit_id"], "codex_bengalfox")
        self.assertEqual(spark["five_hour"]["utilization"], 90)
        self.assertEqual(spark["seven_day"]["utilization"], 86)

    def test_spark_updates_do_not_overwrite_global_codex_usage(self):
        old_usage = codex_console._CODEX_USAGE
        old_by_limit = codex_console._CODEX_USAGE_BY_LIMIT
        try:
            codex_console._CODEX_USAGE = {}
            codex_console._CODEX_USAGE_BY_LIMIT = {}
            codex_console._set_usage({
                "limitId": "codex",
                "primary": {"usedPercent": 45, "windowDurationMins": 10_080},
            }, model="gpt-5.6-sol")
            codex_console._set_usage({
                "limitId": "codex_bengalfox",
                "limitName": "GPT-5.3-Codex-Spark",
                "primary": {"usedPercent": 90, "windowDurationMins": 300},
                "secondary": {"usedPercent": 86, "windowDurationMins": 10_080},
            }, model="gpt-5.3-codex-spark")

            self.assertEqual(codex_console.fetch_usage()["seven_day"]["utilization"], 45)
            self.assertNotIn("five_hour", codex_console.fetch_usage())
            self.assertIn("codex_bengalfox", codex_console._CODEX_USAGE_BY_LIMIT)
        finally:
            codex_console._CODEX_USAGE = old_usage
            codex_console._CODEX_USAGE_BY_LIMIT = old_by_limit

    def test_status_is_local_even_while_busy_and_masks_auth(self):
        session = codex_console.ChatSession(
            "sid", os.getcwd(), "default", "full-access", effort="xhigh")
        session.proc = object()
        session.thread_id = "thread-1"
        session.busy = True
        session.turn_started = time.time() - 2

        with mock.patch.object(codex_console, "AUTH", "example:not-real"):
            session.send_user("/status")

        self.assertTrue(session.busy)
        self.assertEqual(session.queue, [])
        self.assertEqual([event["kind"] for event in session.log], ["user_text", "status"])
        status = session.log[1]["status"]
        self.assertTrue(status["session"]["busy"])
        self.assertEqual(status["session"]["thread_id"], "thread-1")
        self.assertTrue(status["service"]["auth"])
        self.assertNotIn("example:not-real", json.dumps(status))

    def test_context_uses_reported_window_for_actionable_percentage(self):
        session = codex_console.ChatSession(
            "sid", os.getcwd(), "default", "full-access", effort="xhigh")
        emitted = []
        session._emit = lambda event: emitted.append(event)

        with mock.patch.object(codex_console, "_configured_context_window",
                               return_value=1_000_000):
            session._on_token_usage({
                "last": {"inputTokens": 250_000},
                "modelContextWindow": 353_400,
            })

        self.assertEqual(session.ctx["maxTokens"], 353_400)
        self.assertEqual(session.ctx["reportedMaxTokens"], 353_400)
        self.assertEqual(session.ctx["configuredMaxTokens"], 1_000_000)
        self.assertEqual(session.ctx["percentage"], 70.7)
        context_events = [event for event in emitted if event.get("type") == "context"]
        self.assertEqual(context_events[-1]["ctx"], session.ctx)

    def test_token_usage_accepts_snake_case_appserver_fields(self):
        session = codex_console.ChatSession(
            "sid", os.getcwd(), "default", "full-access", effort="xhigh")
        emitted = []
        session._emit = lambda event: emitted.append(event)

        session._on_token_usage({
            "last_token_usage": {
                "input_tokens": 25_000,
                "cached_input_tokens": 20_000,
                "output_tokens": 400,
            },
            "model_context_window": 100_000,
        })

        self.assertEqual(session.ctx["maxTokens"], 100_000)
        self.assertEqual(session.ctx["percentage"], 25.0)
        self.assertTrue(any(event.get("type") == "tokens" for event in emitted))

    def test_resume_metadata_prefers_actual_turn_context_model(self):
        rows = [
            {"type": "turn_context", "payload": {"model": "gpt-5.5", "effort": "xhigh"}},
            {"type": "event_msg", "payload": {
                "type": "token_count",
                "info": {
                    "model_context_window": 258_400,
                    "last_token_usage": {"input_tokens": 129_200},
                },
            }},
        ]
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        try:
            with tmp:
                for row in rows:
                    tmp.write(json.dumps(row) + "\n")
            with mock.patch.object(codex_console, "find_transcript",
                                   return_value=tmp.name):
                with mock.patch.object(codex_console, "_configured_context_window",
                                       return_value=1_000_000):
                    session = codex_console.ChatSession(
                        "sid", os.getcwd(), "default", "full-access",
                        resume_cc="thread-1", effort="xhigh")
                    session.preload()
        finally:
            os.unlink(tmp.name)

        self.assertEqual(session.display_model, "gpt-5.5")
        self.assertEqual(session.ctx["maxTokens"], 258_400)
        self.assertEqual(session.ctx["model"], "gpt-5.5")
        self.assertEqual(session.ctx["percentage"], 50.0)


if __name__ == "__main__":
    unittest.main()
