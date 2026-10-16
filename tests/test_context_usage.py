import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

import codex_console as c
from context_usage import parse_context_usage, read_context_usage


TRACE = ('session_loop{thread_id=thread-a}: post sampling token usage '
         'turn_id=turn-a total_usage_tokens=795897 auto_compact_scope_tokens=795897 '
         'auto_compact_scope_limit=Some(784800) auto_compact_limit_scope=Total '
         'full_context_window_limit=Some(828400) token_limit_reached=true')
REQUEST = {'last': {'inputTokens': 552898, 'cachedInputTokens': 411648,
                    'outputTokens': 161, 'totalTokens': 553059},
           'total': {'totalTokens': 485359778}, 'modelContextWindow': 828400}


def sample(at=100.5):
    return {**parse_context_usage(TRACE), 'sampledAt': at}


def session():
    s = c.ChatSession('test', os.getcwd(), 'default', 'read-only', resume_cc='thread-a')
    s._emit = mock.Mock()
    return s


class ContextDiagnosticTests(unittest.TestCase):
    def test_exact_core_values_not_billing_or_request_usage(self):
        result = parse_context_usage(TRACE)
        self.assertEqual(result['totalTokens'], 795897)
        self.assertEqual(result['maxTokens'], 828400)
        self.assertEqual(result['percentage'], 96.1)
        self.assertEqual(result['autoCompactLimit'], 784800)
        self.assertEqual(result['autoCompactTokens'], 795897)
        self.assertEqual(result['source'], 'codex')
        self.assertNotIn('message', result)

    def test_scope_and_zero_are_preserved(self):
        text = TRACE.replace('total_usage_tokens=795897', 'total_usage_tokens=0')
        text = text.replace('scope_tokens=795897', 'scope_tokens=12').replace('scope=Total', 'scope=Window')
        result = parse_context_usage(text)
        self.assertEqual(result['percentage'], 0)
        self.assertEqual(result['autoCompactTokens'], 12)
        self.assertEqual(result['autoCompactScope'], 'Window')
        self.assertIsNone(parse_context_usage(TRACE.replace('Some(828400)', 'None')))
        self.assertIsNone(parse_context_usage(TRACE.replace('Some(828400)', 'Some(0)')))
        self.assertIsNone(parse_context_usage(TRACE.replace('tokens=795897', 'tokens=-1')))
        self.assertIsNone(parse_context_usage('unrelated diagnostic'))

    def test_read_only_latest_thread_sample_and_nanosecond_reset_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp);path = root / 'logs_2.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,'
                           'thread_id TEXT,target TEXT,feedback_log_body TEXT)')
                db.execute('CREATE INDEX by_thread_time ON logs(thread_id,ts DESC,ts_nanos DESC,id DESC)')
                for cc,seconds,nanos,body in [('thread-a',100,100000000,TRACE),
                        ('thread-a',100,500000000,TRACE), ('thread-b',101,0,TRACE),
                        ('thread-a',102,0,'unrelated message')]:
                    db.execute('INSERT INTO logs(ts,ts_nanos,thread_id,target,feedback_log_body) VALUES(?,?,?,?,?)',
                               (seconds,nanos,cc,'codex_core::session::turn',body))
            before = path.read_bytes()
            result = read_context_usage('thread-a',root=root)
            self.assertEqual(result, sample())
            self.assertEqual(read_context_usage('thread-a',100.2,root=root), result)
            self.assertIsNone(read_context_usage('thread-a',100.5,root=root))
            self.assertIsNone(read_context_usage("' OR 1=1 --",root=root))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual({p.name for p in root.iterdir()}, {'logs_2.sqlite'})

    def test_missing_changed_and_locked_database_fall_back_without_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(read_context_usage('thread-a',root=root))
            self.assertEqual(list(root.iterdir()), [])
            path = root / 'logs_3.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE logs(other_column TEXT)')
            self.assertIsNone(read_context_usage('thread-a',root=root))
            with sqlite3.connect(path) as db:
                db.execute('BEGIN EXCLUSIVE')
                self.assertIsNone(read_context_usage('thread-a',root=root))


class ContextSessionTests(unittest.TestCase):
    def test_core_sample_wins_without_api_updates_making_it_jump_back(self):
        s = session()
        with mock.patch.object(c.time, 'time', return_value=100):
            s._on_token_usage(REQUEST,'turn-a')
        self.assertEqual(s.ctx['source'], 'request')
        self.assertEqual(s.ctx['percentage'], 66.7)
        self.assertEqual(s._tok_up, 141250)
        s._apply_context_sample(sample())
        self.assertEqual(s.ctx['percentage'], 96.1)
        self.assertEqual(s.ctx['source'], 'codex')
        with mock.patch.object(c.time, 'time', return_value=101):
            s._on_token_usage(REQUEST,'turn-a')
        self.assertEqual(s.ctx['percentage'], 96.1)
        s._emit.reset_mock()
        s._apply_context_sample(sample())
        s._emit.assert_not_called()

    def test_stale_diagnostics_revert_to_explicit_request_fallback(self):
        s = session();s._apply_context_sample(sample())
        with mock.patch.object(c.time, 'time', return_value=110):
            s._on_token_usage(REQUEST,'turn-a')
        with mock.patch.object(c.time, 'time', return_value=115):
            s._apply_context_sample(sample())
        self.assertEqual(s.ctx['source'], 'request')
        self.assertEqual(s.ctx['percentage'], 66.7)

    def test_compaction_resets_and_rejects_pre_compaction_or_wrong_turn_values(self):
        s = session();s._context_turn_id='turn-a';s._apply_context_sample(sample())
        with mock.patch.object(c.time, 'time', return_value=101):
            s._on_item({'type':'contextCompaction','id':'compaction-a'},started=False)
        self.assertIsNone(s.ctx)
        s._apply_context_sample(sample());self.assertIsNone(s.ctx)
        fresh = sample(102);fresh['totalTokens']=20000;fresh['percentage']=2.4
        s._apply_context_sample({**fresh,'turnId':'other-turn'});self.assertIsNone(s.ctx)
        s._apply_context_sample(fresh);self.assertEqual(s.ctx['totalTokens'],20000)
        with mock.patch.object(c.time,'time',return_value=103):
            s._on_settings_updated({'model':'another-model'})
        self.assertIsNone(s.ctx)
        s._apply_context_sample(fresh);self.assertIsNone(s.ctx)

    def test_window_change_does_not_restore_old_window(self):
        s = session();s._apply_context_sample(sample())
        with mock.patch.object(c.time,'time',return_value=101):
            s._on_token_usage({**REQUEST,'modelContextWindow':1000000},'turn-a')
            s._apply_context_sample(sample())
        self.assertEqual(s.ctx['source'],'request')
        self.assertEqual(s.ctx['maxTokens'],1000000)

    def test_resume_uses_diagnostics_but_never_pre_compaction_values(self):
        rows = [
            {'timestamp':'1970-01-01T00:01:40Z','type':'turn_context','payload':{'model':'test-model'}},
            {'timestamp':'1970-01-01T00:01:40Z','type':'event_msg','payload':{
                'type':'token_count','info':{'last_token_usage':{'input_tokens':552898},'model_context_window':828400}}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rollout.jsonl'
            path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            with mock.patch.object(c,'find_transcript',return_value=str(path)), \
                 mock.patch.object(c,'read_context_usage',return_value=sample()) as read:
                s=session();s.preload()
                self.assertEqual(s.ctx['source'],'codex')
                self.assertEqual(s.ctx['percentage'],96.1)
                read.assert_called_with('thread-a',98.0)
            rows.append({'timestamp':'1970-01-01T00:01:41Z','type':'compacted','payload':{}})
            path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            with mock.patch.object(c,'find_transcript',return_value=str(path)), \
                 mock.patch.object(c,'read_context_usage',return_value=None) as read:
                meta=c.load_transcript_meta('thread-a')
                self.assertNotIn('ctx',meta)
                read.assert_called_with('thread-a',101.0)


class ContextPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_updates_active_session(self):
        s=session()
        with mock.patch.object(c,'CHAT_SESSIONS',{s.id:s}), \
             mock.patch.object(c,'read_context_usage',return_value=sample()):
            await c._context_tick()
        self.assertEqual(s.ctx['percentage'],96.1)

    async def test_inflight_sample_is_ignored_after_reset_removal_or_end(self):
        for change in ('reset','remove','end'):
            with self.subTest(change=change):
                s=session();sessions={s.id:s};started=asyncio.Event();release=asyncio.Event()
                async def slow_read(_executor,_fn):
                    started.set();await release.wait();return [sample()]
                with mock.patch.object(c,'CHAT_SESSIONS',sessions), \
                     mock.patch.object(asyncio.get_running_loop(),'run_in_executor',side_effect=slow_read):
                    task=asyncio.create_task(c._context_tick());await started.wait()
                    if change=='reset':s._reset_context_usage()
                    elif change=='remove':sessions.clear()
                    else:s.ended=True
                    release.set();await task
                self.assertIsNone(s.ctx)

    async def test_idle_sessions_stop_polling_until_reattached(self):
        s=session();s._context_refresh_until=time.monotonic()-1
        with mock.patch.object(c,'CHAT_SESSIONS',{s.id:s}), \
             mock.patch.object(c,'read_context_usage',return_value=sample()) as read:
            await c._context_tick();read.assert_not_called()
            s.attach(mock.Mock());await c._context_tick();read.assert_called_once()


if __name__ == '__main__':
    unittest.main()
