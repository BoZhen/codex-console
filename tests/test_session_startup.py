import asyncio
import json
import os
import sys
import unittest
from unittest import mock

import codex_console as c


class SessionStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.proc = mock.MagicMock()
        self.proc.wait = mock.AsyncMock(return_value=0)
        self.spawn = self.patch(c.asyncio, 'create_subprocess_exec',
                               mock.AsyncMock(return_value=self.proc))
        self.patch(c, 'CODEX_BIN', sys.executable)
        self.loop = self.patch(c.tornado.ioloop.IOLoop, 'current').return_value
        self.save = self.patch(c, 'save_pref')

    def patch(self, obj, name, *args):
        patcher = mock.patch.object(obj, name, *args)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def session(self, resume='original-thread'):
        session = c.ChatSession('test-session', os.getcwd(), 'test-model',
                                'read-only', resume_cc=resume)
        session._request = mock.AsyncMock()
        session._notify = mock.Mock()
        return session

    def assert_stopped(self, session):
        self.proc.terminate.assert_called_once()
        self.proc.wait.assert_awaited()
        self.assertIsNone(session.proc)
        self.assertTrue(session.ended)
        self.save.assert_not_called()
        # Only the two readers were scheduled, never ready/rate-limit callbacks.
        self.assertEqual(self.loop.spawn_callback.call_count, 2)

    async def test_resume_errors_never_start_a_new_thread(self):
        for error in (RuntimeError('thread-store conflict: already has an active writer'),
                      RuntimeError('thread not found'), asyncio.TimeoutError()):
            with self.subTest(error=type(error).__name__, message=str(error)):
                self.proc.reset_mock();self.loop.reset_mock();self.save.reset_mock()
                session = self.session()
                session.log = [{'kind': 'user_text', 'text': 'Existing conversation'}]
                session._request.side_effect = [{}, error]
                with self.assertRaises(Exception) as raised:
                    await session.start()
                if 'active writer' in str(error):
                    self.assertIn('Close it there and retry', str(raised.exception))
                    self.assertIs(raised.exception.__cause__, error)
                else:
                    self.assertIs(raised.exception, error)
                self.assertEqual([call.args[0] for call in session._request.await_args_list],
                                 ['initialize', 'thread/resume'])
                self.assertEqual(session.cc_id, 'original-thread')
                self.assertEqual(session.log, [{'kind': 'user_text', 'text': 'Existing conversation'}])
                self.assert_stopped(session)

    async def test_invalid_or_mismatched_resume_result_is_rejected(self):
        for response in (None, {}, {'thread': {}}, {'thread': {'id': ''}},
                         {'thread': {'id': 'other-thread'}}):
            with self.subTest(response=response):
                self.proc.reset_mock();self.loop.reset_mock();self.save.reset_mock()
                session = self.session()
                session._request.side_effect = [{}, response]
                with self.assertRaises(RuntimeError):
                    await session.start()
                self.assertEqual(session.cc_id, 'original-thread')
                self.assertEqual(session._request.await_count, 2)
                self.assert_stopped(session)

    async def test_resume_keeps_identity_and_new_session_still_works(self):
        for resume in ('original-thread', None):
            with self.subTest(resume=resume):
                self.proc.reset_mock();self.loop.reset_mock();self.save.reset_mock()
                session = self.session(resume)
                thread_id = resume or 'new-thread'
                session._request.side_effect = [{}, {'thread': {'id': thread_id}, 'reasoningEffort': 'high'}]
                await session.start()
                calls = session._request.await_args_list
                self.assertEqual(calls[1].args[0], 'thread/resume' if resume else 'thread/start')
                self.assertEqual(calls[1].args[1].get('threadId'), resume)
                self.assertEqual(session.cc_id, thread_id)
                self.assertEqual(session.thread_id, thread_id)
                self.assertFalse(session.ended)
                self.assertEqual(session.effort, 'high')
                self.save.assert_called_once()
                self.proc.terminate.assert_not_called()
                self.assertEqual(self.loop.spawn_callback.call_count, 4)

    async def test_initialize_failure_and_cancellation_reap_process(self):
        for error in (RuntimeError('handshake failed'), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.proc.reset_mock();self.loop.reset_mock();self.save.reset_mock()
                session = self.session()
                session._request.side_effect = error
                with self.assertRaises(type(error)):
                    await session.start()
                self.assertEqual(session._request.await_count, 1)
                self.assert_stopped(session)

    async def test_failed_new_session_is_reaped(self):
        session = self.session(None)
        session._request.side_effect = [{}, RuntimeError('start failed')]
        with self.assertRaisesRegex(RuntimeError, 'start failed'):
            await session.start()
        self.assert_stopped(session)

    async def test_unresponsive_failed_process_is_killed(self):
        session = self.session()
        session._request.side_effect = [{}, RuntimeError('resume failed')]
        self.proc.wait.side_effect = [asyncio.TimeoutError(), 0]
        with self.assertRaisesRegex(RuntimeError, 'resume failed'):
            await session.start()
        self.proc.kill.assert_called_once()
        self.assertEqual(self.proc.wait.await_count, 2)
        self.assert_stopped(session)


class ResumeControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_resume_does_not_replace_viewer_or_register_session(self):
        socket = object.__new__(c.ChatSocket)
        previous = mock.Mock()
        socket.session = previous
        socket._say = mock.Mock()
        failed = mock.Mock()
        failed.start = mock.AsyncMock(side_effect=RuntimeError('resume failed'))
        with mock.patch.object(c, 'CODEX_BIN', sys.executable), \
             mock.patch.object(c, 'CHAT_SESSIONS', {}), \
             mock.patch.object(c, 'load_prefs', return_value={}), \
             mock.patch.object(c, 'ChatSession', return_value=failed):
            await socket.on_message(json.dumps({'type': 'resume', 'cc': 'original-thread',
                                                'cwd': os.getcwd()}))
            self.assertEqual(c.CHAT_SESSIONS, {})
        self.assertIs(socket.session, previous)
        previous.detach.assert_not_called()
        failed.attach.assert_not_called()
        socket._say.assert_called_once()
        self.assertEqual(socket._say.call_args.args[0]['type'], 'error')
        self.assertIn('resume failed', socket._say.call_args.args[0]['error'])


if __name__ == '__main__':
    unittest.main()
