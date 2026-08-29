import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import codex_console


def _record(record_type, payload):
    return {"type": record_type, "payload": payload}


def _write_rollout(path, records, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    os.utime(path, (mtime, mtime))


class SessionDiscoveryTests(unittest.TestCase):
    def test_agents_instruction_variants_are_injected_content(self):
        self.assertTrue(codex_console._codex_injected(
            "# AGENTS.md instructions\n<INSTRUCTIONS>"))
        self.assertTrue(codex_console._codex_injected(
            "#AGENTS.md instructions for /repo\n<INSTRUCTIONS>"))
        self.assertFalse(codex_console._codex_injected("# AGENTS.md usage notes"))

    def test_title_skips_agents_block_and_uses_real_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp, "rollout.jsonl")
            _write_rollout(rollout, [
                _record("session_meta", {"cwd": tmp, "source": "cli"}),
                _record("response_item", {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text":
                                 "# AGENTS.md instructions\n<INSTRUCTIONS>internal</INSTRUCTIONS>"}]}),
                _record("event_msg", {
                    "type": "user_message", "message": "真正的用户问题"}),
            ], 1)

            cwd, _, title, is_subagent = codex_console._peek_codex(rollout)

        self.assertEqual(cwd, tmp)
        self.assertEqual(title, "真正的用户问题")
        self.assertFalse(is_subagent)

    def test_list_sessions_hides_subagents_before_applying_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common_user = _record("event_msg", {
                "type": "user_message", "message": "real session"})
            _write_rollout(root / "2026" / "root.jsonl", [
                _record("session_meta", {"cwd": tmp, "source": "cli"}), common_user,
            ], 1)
            for index in range(2):
                _write_rollout(root / "2026" / f"subagent-{index}.jsonl", [
                    _record("session_meta", {
                        "cwd": tmp,
                        "source": {"subagent": {"thread_spawn": {"depth": 1}}}}),
                    common_user,
                ], 10 + index)

            with mock.patch.object(codex_console, "CODEX_ROOT", tmp):
                sessions = codex_console.list_sessions(limit=1)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(Path(sessions[0]["id"]).name, "root.jsonl")
        self.assertEqual(sessions[0]["title"], "real session")


if __name__ == "__main__":
    unittest.main()
