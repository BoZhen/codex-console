import json
import os
import time
import unittest
from unittest import mock

import codex_console


class StatusCommandTests(unittest.TestCase):
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

    def test_context_uses_configured_window_when_larger_than_reported(self):
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

        self.assertEqual(session.ctx["maxTokens"], 1_000_000)
        self.assertEqual(session.ctx["reportedMaxTokens"], 353_400)
        self.assertEqual(session.ctx["configuredMaxTokens"], 1_000_000)
        self.assertEqual(session.ctx["percentage"], 25.0)
        context_events = [event for event in emitted if event.get("type") == "context"]
        self.assertEqual(context_events[-1]["ctx"], session.ctx)


if __name__ == "__main__":
    unittest.main()
