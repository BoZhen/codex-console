import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import codex_console


THREAD_ID = "019d5738-b9cf-7523-9613-45f999a4aaf2"


def _record(record_type, payload, ts="2026-08-16T12:34:56.000Z"):
    return {"timestamp": ts, "type": record_type, "payload": payload}


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8")


class HistoryImportTests(unittest.TestCase):
    def setUp(self):
        codex_console._index_state.update({"t": 0.0, "err": ""})

    def test_history_index_searches_conversation_and_tool_inputs_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "sessions")
            db = Path(tmp, "history.db")
            project = Path(tmp, "project")
            project.mkdir()
            rollout = root / "2026" / "08" / "16" / (
                "rollout-2026-08-16T12-34-56-%s.jsonl" % THREAD_ID)
            _write(rollout, [
                _record("session_meta", {
                    "id": THREAD_ID, "cwd": os.fspath(project),
                    "timestamp": "2026-08-16T12:34:56.000Z"}),
                _record("response_item", {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text",
                                 "text": "# AGENTS.md instructions\n<INSTRUCTIONS>secret injected</INSTRUCTIONS>"}]}),
                _record("event_msg", {
                    "type": "user_message", "message": "查找热力学 powerlaw"}),
                _record("response_item", {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer mentions beta sweep"}]}),
                _record("response_item", {
                    "type": "function_call", "name": "exec_command",
                    "arguments": json.dumps({"cmd": "nl -ba codex_console.py | sed -n '1,20p'"}),
                    "call_id": "call-1"}),
                _record("response_item", {
                    "type": "function_call_output", "call_id": "call-1",
                    "output": "secret-output-from-tool"}),
            ])

            with mock.patch.object(codex_console, "CODEX_ROOT", os.fspath(root)), \
                    mock.patch.object(codex_console, "INDEX_DB", os.fspath(db)):
                stats = codex_console.reindex()
                self.assertGreaterEqual(stats["messages"], 3)

                user_hits = codex_console.search_history("热", limit=20)["results"]
                self.assertEqual(user_hits[0]["cc"], THREAD_ID)
                self.assertEqual(user_hits[0]["role"], "user")
                self.assertIn("热", user_hits[0]["hit"])

                self.assertEqual(
                    codex_console.search_history("secret injected", limit=20)["results"], [])
                self.assertEqual(
                    codex_console.search_history("secret-output-from-tool", limit=20)["results"], [])

                tool_hits = codex_console.search_history("nl -ba", limit=20)["results"]
                self.assertEqual(tool_hits[0]["role"], "tool")
                thread = codex_console.load_thread(tool_hits[0]["mid"], before=5, after=5)
                roles = [m["role"] for m in thread["messages"]]
                self.assertIn("assistant", roles)
                self.assertIn("tool", roles)

    def test_imported_rollout_uses_codex_date_tree_and_is_findable(self):
        body = (json.dumps(_record("session_meta", {
            "id": THREAD_ID, "cwd": "/tmp/project",
            "timestamp": "2026-08-16T12:34:56.000Z"})) + "\n").encode()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(codex_console, "CODEX_ROOT", tmp):
                dest = codex_console.import_rollout_dest(THREAD_ID, body)
                self.assertTrue(dest.endswith(
                    "2026/08/16/rollout-2026-08-16T12-34-56-%s.jsonl" % THREAD_ID))
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                Path(dest).write_bytes(body)
                self.assertEqual(codex_console.find_transcript(THREAD_ID), dest)
                self.assertEqual(codex_console.transcript_thread_id(body), THREAD_ID)

    def test_html_exposes_search_import_export_and_manual_model_refresh(self):
        html = codex_console.CONSOLE_HTML
        self.assertIn('id="srchopen"', html)
        self.assertIn('api/search?q=', html)
        self.assertIn('api/thread?mid=', html)
        self.assertIn('id="impbtn"', html)
        self.assertIn('api/import', html)
        self.assertIn('api/export?cc=', html)
        self.assertIn('id="mrefresh"', html)
        self.assertIn("loadModels(true)", html)


if __name__ == "__main__":
    unittest.main()
