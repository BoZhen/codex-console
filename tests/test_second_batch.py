import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codex_console as c
import tornado.web
from tornado.testing import AsyncHTTPTestCase


class CompressedHistoryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('zstd'), 'zstd required')
    def test_compressed_discovery_preload_index_and_representation_transition(self):
        cc = '11111111-2222-4333-8444-555555555555'
        records = [
            {'type': 'session_meta', 'payload': {'id': cc, 'cwd': '/project'}},
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'needle query'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': 'needle answer'}]}},
            {'type': 'turn_context', 'payload': {'model': 'test-model', 'effort': 'high'}},
        ]
        data = ''.join(json.dumps(r)+'\n' for r in records).encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, 'sessions'); root.mkdir()
            plain = root / ('rollout-test-' + cc + '.jsonl')
            compressed = Path(str(plain)+'.zst')
            compressed.write_bytes(subprocess.check_output(['zstd', '-q', '-c'], input=data))
            with mock.patch.object(c, 'CODEX_ROOT', str(root)), mock.patch.object(c, 'INDEX_DB', str(Path(tmp, 'index.db'))):
                self.assertEqual(c.find_transcript(cc), str(compressed))
                self.assertEqual(c.list_sessions()[0]['title'], 'needle query')
                self.assertEqual(c.load_transcript_meta(cc)['model'], 'test-model')
                self.assertEqual(c.load_transcript_events(cc)[0]['text'], 'needle answer')
                with c._open_rollout(plain, binary=True) as source:
                    self.assertEqual(source.read(), data)
                self.assertEqual(c.reindex()['messages'], 2)
                self.assertEqual(c.reindex()['added'], 0)
                self.assertEqual(len(c.search_history('needle')['results']), 2)
                plain.write_bytes(data)
                self.assertEqual(c._rollout_paths(), [str(plain)])
                self.assertEqual(c.reindex()['messages'], 2)
                # A stale compressed path must resolve to the current plain file.
                with c._open_rollout(compressed, binary=True) as source:
                    self.assertEqual(source.read(), data)
                with mock.patch.object(c.subprocess, 'run') as trash:
                    self.assertTrue(c.trash_transcript(cc)['ok'])
                    self.assertEqual(trash.call_args.args[0], ['gio', 'trash', str(plain), str(compressed)])

    @unittest.skipUnless(shutil.which('zstd'), 'zstd required')
    def test_invalid_compressed_stream_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, 'broken.jsonl.zst'); p.write_bytes(b'not zstd')
            with self.assertRaises(OSError):
                with c._open_rollout(p, binary=True) as source:
                    source.read()


class CompressedExportTests(AsyncHTTPTestCase):
    def get_app(self):
        return tornado.web.Application([(r'/export', c.ExportHandler)])

    @unittest.skipUnless(shutil.which('zstd'), 'zstd required')
    def test_export_returns_plain_jsonl(self):
        data = b'{"type":"session_meta","payload":{}}\n'
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, 'test.jsonl.zst')
            p.write_bytes(subprocess.check_output(['zstd', '-q', '-c'], input=data))
            with mock.patch.object(c, 'find_transcript', return_value=str(p)), mock.patch.object(c, 'AUTH', ''):
                response = self.fetch('/export?cc=example')
            self.assertEqual(response.code, 200)
            self.assertEqual(response.body, data)
            self.assertIn('example.jsonl', response.headers['Content-Disposition'])


class BackgroundTerminalTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        s = c.ChatSession('sid', '/project', 'test-model', 'read-only')
        s.proc = object(); s.thread_id = 'parent'
        return s

    async def test_pages_are_collected_and_stop_is_scoped(self):
        s = self.session()
        s._request = mock.AsyncMock(side_effect=[
            {'data': [{'processId': 'a'}], 'nextCursor': 'next'},
            {'data': [{'processId': 'b'}]}, {'terminated': True}])
        self.assertTrue((await s.stop_background_terminal('b'))['ok'])
        self.assertEqual(s._request.call_args.args, ('thread/backgroundTerminals/terminate',
                         {'threadId': 'parent', 'processId': 'b'}))
        s._request = mock.AsyncMock(return_value={'data': [{'processId': 'a'}]})
        self.assertFalse((await s.stop_background_terminal('foreign'))['ok'])
        self.assertEqual(s._request.call_count, 1)

    async def test_unsupported_and_repeated_cursor_fail_without_stopping(self):
        s = self.session()
        s._request = mock.AsyncMock(side_effect=RuntimeError('method not found'))
        self.assertFalse((await s.background_terminals())['ok'])
        s._request = mock.AsyncMock(return_value={'data': [], 'nextCursor': 'again'})
        self.assertFalse((await s.stop_background_terminal('a'))['ok'])
        self.assertEqual(s._request.call_count, 2)

    async def test_child_completion_updates_activity_and_recent_items_are_chronological(self):
        s = self.session()
        s._on_foreign_thread_note('turn/completed', {'threadId': 'child', 'turn': {'status': 'completed'}})
        self.assertFalse(s.subagents_snapshot()[0]['active'])
        s._request = mock.AsyncMock(side_effect=[{'thread': {'id': 'child', 'status': 'idle'}},
            {'data': [{'item': {'id': 'new', 'type': 'agentMessage', 'text': 'new'}},
                      {'item': {'id': 'old', 'type': 'agentMessage', 'text': 'old'}}]}])
        result = await s.read_subagent_thread('child', 20)
        self.assertEqual([m['txt'] for m in result['messages']], ['old', 'new'])
        self.assertEqual(s._request.call_args.args[1]['sortDirection'], 'desc')


if __name__ == '__main__':
    unittest.main()
