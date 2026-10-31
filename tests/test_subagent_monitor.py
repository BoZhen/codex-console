import asyncio
import json
import os
import unittest
from unittest import mock

import codex_console as c


def session():
    value = c.ChatSession('parent-session', os.getcwd(), 'default', 'full-access')
    value.thread_id = 'parent-thread'
    value.proc = mock.Mock()
    value._emit = mock.Mock()
    return value


class SubagentStateTests(unittest.TestCase):
    def test_child_tools_and_messages_remain_outside_main_chat(self):
        value = session()
        value._on_note('thread/started', {'thread': {'id': 'child', 'parentThreadId': 'parent-thread', 'status': {'type': 'active'}}})
        value._on_note('item/started', {'threadId': 'child', 'item': {
            'type': 'commandExecution', 'id': 'read-one', 'command': 'cat analysis.py',
            'commandActions': [{'type': 'read', 'path': 'analysis.py'}], 'status': 'inProgress'}})
        value._on_note('item/agentMessage/delta', {'threadId': 'child', 'itemId': 'message-one', 'delta': 'Checking '})
        value._on_note('item/agentMessage/delta', {'threadId': 'child', 'itemId': 'message-one', 'delta': 'the boundary conditions.'})
        child = value.subagents_snapshot()[0]
        self.assertEqual(value.thread_id, 'parent-thread')
        self.assertEqual(child['currentTool']['name'], 'Read')
        self.assertIn('analysis.py', child['currentTool']['summary'])
        self.assertEqual(child['message'], 'Checking the boundary conditions.')
        self.assertEqual(value.log, [])
        self.assertEqual(value._emit.call_args.args[0]['sessionId'], 'parent-session')
        value._on_note('turn/completed', {'threadId': 'child', 'turn': {'status': 'completed'}})
        child = value.subagents_snapshot()[0]
        self.assertFalse(child['active'])
        self.assertEqual(child['currentTool']['status'], 'completed')
        value._on_note('item/completed', {'threadId': 'child', 'item': {'type': 'agentMessage', 'id': 'message-one', 'text': 'Done.'}})
        self.assertFalse(value.subagents_snapshot()[0]['active'], 'Late item completion cannot reactivate a completed child')

    def test_idle_and_unloaded_children_are_not_active(self):
        value = session()
        value._upsert_subagent('child', state='running')
        value._upsert_subagent('child', state={'type': 'idle'})
        self.assertFalse(value.subagents_snapshot()[0]['active'])
        value._upsert_subagent('child', state={'type': 'active'})
        self.assertTrue(value.subagents_snapshot()[0]['active'])
        value._upsert_subagent('child', state='completed')
        value._upsert_subagent('child', state={'type': 'notLoaded'})
        self.assertEqual(value.subagents_snapshot()[0]['state'], 'completed')

    def test_closed_child_stops_showing_as_active(self):
        value = session(); value._upsert_subagent('child', state='running')
        value._on_note('thread/closed', {'threadId': 'child'})
        self.assertFalse(value.subagents_snapshot()[0]['active'])
        self.assertEqual(value.thread_id, 'parent-thread')

    def test_resume_recovers_children_from_older_agent_tool_records(self):
        value = session(); value.resume_cc = 'parent-thread'
        history = [{'kind': 'tool_use', 'tool': 'Agent', 'input': {'receiverThreadIds': ['child-old'], 'prompt': 'Check inputs'}}]
        with mock.patch.object(c, 'load_transcript_meta', return_value={}), mock.patch.object(c, 'load_transcript_events', return_value=history):
            value.preload()
        child = value.subagents_snapshot()[0]
        self.assertEqual(child['threadId'], 'child-old')
        self.assertEqual(child['prompt'], 'Check inputs')
        self.assertFalse(child['active'])


class SubagentReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_reads_ten_agents_without_turns_and_bounds_concurrency(self):
        value = session(); requests = []; running = peak = 0
        for index in range(10): value._upsert_subagent(f'child-{index}', state='running')
        async def request(method, params, **kwargs):
            nonlocal running, peak
            requests.append((method, params)); running += 1; peak = max(peak, running)
            await asyncio.sleep(.001); running -= 1
            tid = params['threadId']
            if method == 'thread/read':
                return {'thread': {'id': tid, 'status': {'type': 'idle' if tid == 'child-0' else 'active'}}}
            return {'data': [{'item': {'type': 'commandExecution', 'id': tid + '-cmd', 'command': 'cat analysis.py',
                                      'commandActions': [{'type': 'read', 'path': 'analysis.py'}], 'status': 'completed'}},
                             {'item': {'type': 'agentMessage', 'id': tid + '-msg', 'text': 'Checking inputs'}}]}
        value._request = mock.AsyncMock(side_effect=request)
        first, second = await asyncio.gather(value.refresh_subagents(), value.refresh_subagents())
        self.assertEqual(len(requests), 20)
        self.assertLessEqual(peak, 4)
        self.assertEqual(first, second)
        self.assertEqual(len(first['subagents']), 10)
        self.assertEqual(sum(a['active'] for a in first['subagents']), 9)
        self.assertTrue(all(a['currentTool']['name'] == 'Read' for a in first['subagents']))
        self.assertEqual({name for name, _ in requests}, {'thread/read', 'thread/items/list'})
        self.assertTrue(all(not params['includeTurns'] for name, params in requests if name == 'thread/read'))
        await value.refresh_subagents()
        self.assertEqual(len(requests), 20, 'Other viewers reuse the recent summary')

    async def test_read_snapshots_do_not_override_newer_live_events(self):
        value = session(); value._upsert_subagent('child', state='running')
        entered, release = asyncio.Event(), asyncio.Event()
        async def request(method, params, **kwargs):
            if method == 'thread/read':
                entered.set(); await release.wait()
                return {'thread': {'id': 'child', 'status': {'type': 'active'}}}
            return {'data': []}
        value._request = mock.AsyncMock(side_effect=request)
        task = asyncio.create_task(value.read_subagent_thread('child'))
        await entered.wait()
        value._on_note('turn/completed', {'threadId': 'child', 'turn': {'status': 'completed'}})
        release.set(); result = await task
        self.assertFalse(result['subagent']['active'])
        self.assertEqual(result['subagent']['state'], 'completed')
        self.assertLess(result['readRevision'], result['subagent']['revision'])

    async def test_invalid_children_are_never_read_and_overview_does_not_hydrate_full_history(self):
        value = session(); value._upsert_subagent('child', state='running')
        value._request = mock.AsyncMock(side_effect=RuntimeError('unsupported method'))
        self.assertFalse((await value.read_subagent_thread('not-owned'))['ok'])
        value._request.assert_not_awaited()
        result = await value.read_subagent_thread('child', overview=True)
        self.assertFalse(result['ok'])
        self.assertEqual(value._request.await_count, 2)
        self.assertNotIn(True, [call.args[1].get('includeTurns') for call in value._request.await_args_list])

    async def test_unchanged_reads_do_not_look_like_new_activity(self):
        value = session(); value._upsert_subagent('child', state='running')
        value._request = mock.AsyncMock(side_effect=lambda method, params, **kwargs:
            {'thread': {'id': 'child', 'status': {'type': 'active'}}} if method == 'thread/read' else {'data': []})
        first = await value.read_subagent_thread('child')
        second = await value.read_subagent_thread('child')
        self.assertEqual(first['subagent']['updatedAt'], second['subagent']['updatedAt'])
        self.assertEqual(first['subagent']['revision'], second['subagent']['revision'])

    async def test_completed_children_are_not_repeatedly_polled(self):
        value = session(); value._upsert_subagent('child', state='completed')
        value._request = mock.AsyncMock(side_effect=lambda method, params, **kwargs:
            {'thread': {'id': 'child', 'status': {'type': 'idle'}}} if method == 'thread/read' else {'data': []})
        await value.refresh_subagents()
        self.assertEqual(value._request.await_count, 2)
        value._subagent_refresh_at = 0
        await value.refresh_subagents()
        self.assertEqual(value._request.await_count, 2)
        value._upsert_subagent('child', activity='interacted')
        value._subagent_refresh_at = 0
        await value.refresh_subagents()
        self.assertEqual(value._request.await_count, 4)

    async def test_pending_detail_read_does_not_block_chat_and_cannot_leak_into_another_session(self):
        value = session(); entered, release = asyncio.Event(), asyncio.Event()
        async def read(*args):
            entered.set(); await release.wait(); return {'ok': True, 'threadId': 'child', 'messages': []}
        value.read_subagent_thread = mock.AsyncMock(side_effect=read)
        value.send_user = mock.Mock()
        socket = object.__new__(c.ChatSocket); socket.session = value; socket._say = mock.Mock()
        await asyncio.wait_for(socket.on_message(json.dumps({'type': 'subagent_read', 'sessionId': value.id,
                                                            'threadId': 'child', 'requestId': 'request-one'})), .2)
        await entered.wait()
        await asyncio.wait_for(socket.on_message(json.dumps({'type': 'user', 'text': 'Continue discussing'})), .2)
        value.send_user.assert_called_once()
        socket.session = mock.Mock(id='different-session')
        release.set()
        for _ in range(10):
            if not socket._subagent_jobs: break
            await asyncio.sleep(.001)
        socket._say.assert_not_called()

    async def test_duplicate_detail_requests_share_work_and_acknowledge_latest_request(self):
        value = session(); entered, release, replied = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def read(*args):
            entered.set(); await release.wait(); return {'ok': True, 'threadId': 'child', 'messages': []}
        value.read_subagent_thread = mock.AsyncMock(side_effect=read)
        socket = object.__new__(c.ChatSocket); socket.session = value
        results = []
        def receive(result): results.append(result); replied.set()
        socket._say = receive
        for rid in ('first', 'latest'):
            await socket.on_message(json.dumps({'type': 'subagent_read', 'sessionId': value.id, 'threadId': 'child', 'requestId': rid}))
            if rid == 'first': await entered.wait()
        release.set(); await asyncio.wait_for(replied.wait(), 1)
        value.read_subagent_thread.assert_awaited_once()
        self.assertEqual(results[0]['requestId'], 'latest')
        self.assertEqual(results[0]['sessionId'], value.id)


if __name__ == '__main__':
    unittest.main()
