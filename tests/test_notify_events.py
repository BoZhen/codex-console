import asyncio
import base64
import json
import types
import unittest
from unittest import mock

from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.web import Application

import codex_console as c
from notify_events import NotifyHub


def session(sid="s1"):
    return types.SimpleNamespace(id=sid, ended=False, title=lambda: "Fixture task")


class HubTests(unittest.TestCase):
    def setUp(self):
        self.s = session()
        self.sessions = {self.s.id: self.s}
        self.now = 100000
        self.hub = NotifyHub(lambda: self.sessions, clock=lambda: self.now)

    def emit(self, kind, **fields):
        self.hub.on_events(self.s, [{"kind": kind, **fields}])

    def test_first_connection_does_not_replay_old_completions(self):
        self.emit("turn_done")
        opening = self.hub.opening(None)
        self.assertEqual([x["type"] for x in opening], ["snapshot", "checkpoint"])
        self.assertTrue(opening[0]["reset"])

    def test_disconnect_replays_completions_and_checkpoint_without_replaying_resolved_questions(self):
        cursor = self.hub.cursor
        self.emit("question", aid="ap1", questions=[{"question": "private question"}])
        self.emit("question_resolved", aid="ap1")
        self.emit("turn_done", isError=True)
        opening = self.hub.opening(cursor)
        self.assertEqual([x["type"] for x in opening], ["snapshot", "finished", "checkpoint"])
        self.assertEqual(opening[0]["waiting"], [])
        self.assertTrue(opening[1]["error"])
        self.assertEqual(opening[-1]["id"], self.hub.cursor)
        self.assertEqual(len(self.hub.opening(self.hub.cursor)), 2)

    def test_pending_snapshot_omits_sensitive_content(self):
        self.emit("question", aid="ap1", isBlocking=False,
                  questions=[{"question": "secret", "isSecret": True}])
        self.emit("approval", aid="ap2", input={"command": "secret command"})
        items = self.hub.opening(None)[0]["waiting"]
        self.assertEqual(len(items), 2)
        self.assertFalse(items[0]["blocking"])
        self.assertNotIn("secret", json.dumps(items))

    def test_compaction_and_child_turns_are_silent(self):
        for fields in ({"subtype": "compact"}, {"parent": "child"}, {"notificationSilent": True}):
            self.emit("turn_done", **fields)
        self.assertEqual(self.hub.sequence, 0)
        self.emit("turn_done", interrupted=True)
        self.assertTrue(self.hub.history[-1][2]["interrupted"])

    def test_restart_invalid_and_future_cursors_reset(self):
        for cursor in ("bad", "a" * 24 + ":1", self.hub.epoch + ":99"):
            self.assertTrue(self.hub.opening(cursor)[0]["reset"])

    def test_retention_and_gap(self):
        cursor = self.hub.cursor
        for _ in range(600):
            self.emit("turn_done")
        self.assertEqual(len(self.hub.history), 512)
        self.assertTrue(self.hub.opening(cursor)[0]["gap"])
        self.now += 86401
        self.assertEqual(len(self.hub.opening(cursor)), 2)

    def test_replay_coalesces_each_sessions_latest_completion(self):
        cursor = self.hub.cursor
        other = session("s2")
        self.sessions[other.id] = other
        self.emit("turn_done")
        self.hub.on_events(other, [{"kind": "turn_done"}])
        self.emit("turn_done", isError=True)
        replay = self.hub.opening(cursor)[1:-1]
        self.assertEqual([event["session"] for event in replay], ["s2", "s1"])
        self.assertTrue(all(event["replayed"] for event in replay))
        self.assertTrue(replay[-1]["error"])

    def test_ended_sessions_do_not_replay(self):
        cursor = self.hub.cursor
        self.emit("question", aid="ap1")
        self.emit("turn_done")
        self.s.ended = True
        self.hub.session_gone(self.s)
        self.assertEqual(self.hub.waiting, {})
        self.assertEqual(len(self.hub.opening(cursor)), 2)

    def test_chat_hooks_and_compact_turn(self):
        with mock.patch.object(c, "NOTIFY", self.hub):
            s = c.ChatSession("fixture", "/tmp", "default", "default")
            s._emit = lambda value: None
            s._drain_queue = lambda: None
            s._push([{"kind": "question", "aid": "ap1"}])
            self.assertIn(("fixture", "ap1"), self.hub.waiting)
            s.busy = True
            s._compact_turn = True
            before = self.hub.sequence
            s._finish_turn()
            self.assertEqual(before, self.hub.sequence)
            s.terminate()
            self.assertNotIn(("fixture", "ap1"), self.hub.waiting)


class StreamTests(AsyncHTTPTestCase):
    def setUp(self):
        self.s = session()
        self.hub = NotifyHub(lambda: {self.s.id: self.s})
        self.patchers = [mock.patch.object(c, "NOTIFY", self.hub), mock.patch.object(c, "AUTH", "fixture:only"),
                         mock.patch.object(c.NotifyStreamHandler, "KEEPALIVE", .03)]
        for patcher in self.patchers:
            patcher.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        for patcher in reversed(self.patchers):
            patcher.stop()

    def get_app(self):
        return Application([(r"/api/notify/stream", c.NotifyStreamHandler)])

    def test_auth_required(self):
        self.assertEqual(self.fetch("/api/notify/stream").code, 401)

    @gen_test
    async def test_sse_replay_live_order_and_heartbeat(self):
        cursor = self.hub.cursor
        self.hub.on_events(self.s, [{"kind": "turn_done"}])
        chunks = []
        headers = {"Authorization": "Basic " + base64.b64encode(b"fixture:only").decode(), "Last-Event-ID": cursor}
        pending = self.http_client.fetch(self.get_url("/api/notify/stream"), headers=headers,
                                        streaming_callback=chunks.append, request_timeout=3)
        for _ in range(50):
            if self.hub.streams:
                break
            await asyncio.sleep(.01)
        self.hub.on_events(self.s, [{"kind": "question", "aid": "ap1"}])
        await asyncio.sleep(.08)
        for stream in list(self.hub.streams):
            stream._queue.put_nowait(None)
        response = await pending
        raw = b"".join(chunks).decode()
        events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
        self.assertEqual([x["type"] for x in events], ["snapshot", "finished", "checkpoint", "waiting"])
        self.assertIn(": keep-alive", raw)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertFalse(self.hub.streams)

    def test_slow_consumer_queue_is_bounded(self):
        stream = object.__new__(c.NotifyStreamHandler)
        stream._closed = False
        stream._queue = asyncio.Queue(maxsize=2)
        for _ in range(3):
            stream.send({"type": "finished"})
        self.assertTrue(stream._closed)
        self.assertIsNone(stream._queue.get_nowait())
