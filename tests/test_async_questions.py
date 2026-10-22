import asyncio
import json
import unittest
from unittest import mock

import codex_console as c


def prompt(iid="call-question"):
    return {"type": "agentMessage", "id": iid, "delivery": "async", "phase": "final_answer",
            "text": "Which dataset?\n- Small\n- Full",
            "questions": [{"title": "Which dataset?", "options": ["Small", "Full"]}]}


class AsyncQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = c.ChatSession("fixture-async", "/tmp", "default", "full-access")
        self.session._emit = lambda value: None
        self.session._send = mock.Mock()
        self.session.title = lambda: "Fixture"
        self.session._drain_queue = mock.Mock()
        self.session.proc = mock.Mock()
        self.session.thread_id = "thread-fixture"
        self.session.turn_id = "turn-fixture"
        self.session.busy = True
        self.session._request = mock.AsyncMock(return_value={"turnId": "turn-fixture"})
        self.notify = mock.patch.object(c, "NOTIFY", mock.Mock())
        self.notify.start()

    async def asyncTearDown(self):
        self.notify.stop()

    def ask(self, iid="call-question"):
        self.session._on_item(prompt(iid), started=True)
        self.session._on_item(prompt(iid), started=False)
        return "async:" + iid

    async def settle(self):
        for _ in range(4):
            await asyncio.sleep(0)

    async def test_async_message_is_one_pending_card_not_text_or_tool(self):
        aid = self.ask()
        self.session._on_item(prompt(), started=False)
        self.assertEqual([e["kind"] for e in self.session.log], ["question"])
        self.assertEqual(self.session.pending_questions_snapshot()[0]["aid"], aid)
        self.assertTrue(self.session.log[0]["questions"][0]["isOther"])
        self.assertEqual(self.session._req, {})

    async def test_answer_steers_once_without_user_echo_or_queue(self):
        aid = self.ask()
        with mock.patch.object(self.session, "send_user") as send:
            self.session.resolve_answer(aid, ["Small"])
            self.session.resolve_answer(aid, ["Small"])
            await self.settle()
        send.assert_not_called()
        self.session._request.assert_awaited_once()
        method, params = self.session._request.call_args.args
        self.assertEqual(method, "turn/steer")
        self.assertEqual(params["expectedTurnId"], "turn-fixture")
        self.assertEqual(params["input"], [{"type": "text", "text": "Which dataset?: Small"}])
        self.assertTrue(params["clientUserMessageId"])
        self.assertFalse(self.session.pending_questions_snapshot())
        self.assertEqual(self.session.queue, [])
        self.assertFalse(any(e["kind"] in ("user_text", "queued") for e in self.session.log))

    async def test_idle_answer_starts_one_turn_and_survives_normal_completion(self):
        aid = self.ask()
        self.session._finish_turn()
        self.assertEqual(len(self.session.pending_questions_snapshot()), 1)
        self.session._request.return_value = {"turn": {"id": "answer-turn"}}
        self.session.resolve_answer(aid, ["Custom dataset"])
        await self.settle()
        self.assertEqual(self.session._request.call_args.args[0], "turn/start")
        self.assertTrue(self.session.busy)
        self.assertEqual(self.session.turn_id, "answer-turn")
        self.assertEqual(self.session.queue, [])

    async def test_rpc_request_takes_precedence_in_either_order(self):
        aid = self.ask()
        request = {"itemId": "call-question", "questions": [{"id": "q", "question": "Which dataset?"}]}
        self.session._on_request(12, "item/tool/requestUserInput", request)
        pending = self.session.pending_questions_snapshot()
        self.assertEqual(len(pending), 1)
        self.assertNotEqual(pending[0]["aid"], aid)
        self.session.resolve_answer(pending[0]["aid"], ["Small"])
        self.session._send.assert_called_once()
        self.session._request.assert_not_called()
        self.session._on_request(13, "item/tool/requestUserInput", dict(request, itemId="other"))
        self.ask("other")
        self.assertEqual(len(self.session.pending_questions_snapshot()), 1)

    async def test_dismiss_and_interrupt_never_send_answer(self):
        aid = self.ask()
        self.session.resolve_answer(aid, [])
        self.assertFalse(self.session.pending_questions_snapshot())
        aid = self.ask("second")
        self.session._finish_turn(interrupted=True)
        self.session.resolve_answer(aid, ["Full"])
        await self.settle()
        self.session._request.assert_not_called()

    async def test_pruned_item_cannot_reopen_answered_question(self):
        aid = self.ask()
        self.session.resolve_answer(aid, [])
        self.session._items.clear()
        self.session._on_item(prompt(), started=False)
        self.assertFalse(self.session.pending_questions_snapshot())
        self.assertEqual(sum(e["kind"] == "question" for e in self.session.log), 1)

    async def test_explicit_failure_unlocks_without_automatic_fallback(self):
        aid = self.ask()
        self.session._request.side_effect = RuntimeError("turn mismatch")
        self.session.resolve_answer(aid, ["Small"])
        await self.settle()
        self.session._request.assert_awaited_once()
        self.assertEqual(self.session.pending_questions_snapshot()[0]["answerState"], "failed")
        self.assertEqual(self.session.log[-1]["kind"], "question_error")
        self.assertEqual(self.session.queue, [])

    async def test_timeout_stays_uncertain_and_does_not_resend(self):
        aid = self.ask()
        self.session._request.side_effect = asyncio.TimeoutError()
        self.session.resolve_answer(aid, ["Small"])
        await self.settle()
        self.session.resolve_answer(aid, ["Small"])
        await self.settle()
        self.session._request.assert_awaited_once()
        self.assertEqual(self.session.pending_questions_snapshot()[0]["answerState"], "uncertain")
        self.session.resolve_answer(aid, [])
        self.assertFalse(self.session.pending_questions_snapshot())

    async def test_matching_user_item_confirms_even_if_rpc_response_is_lost(self):
        aid = self.ask()
        client_id = self.session._async_questions[aid]["client_id"]
        async def received(*args):
            self.session._on_item({"type": "userMessage", "id": "answer", "clientId": client_id}, started=True)
            raise asyncio.TimeoutError()
        self.session._request.side_effect = received
        self.session.resolve_answer(aid, ["Small"])
        await self.settle()
        self.assertFalse(self.session.pending_questions_snapshot())
        self.assertEqual(self.session.log[-1]["kind"], "question_resolved")

    async def test_manual_chat_continuation_closes_unsubmitted_async_cards(self):
        self.ask()
        self.session.send_user("Use the small dataset")
        self.assertFalse(self.session.pending_questions_snapshot())
        self.assertEqual(len(self.session.queue), 1)
        self.session._request.assert_not_called()

    async def test_invalid_answers_never_send(self):
        aid = self.ask()
        for answers in (None, "Small", [None], [""], ["Small", "Full"]):
            self.session.resolve_answer(aid, answers)
        await self.settle()
        self.session._request.assert_not_called()

    def test_history_question_is_read_only_not_raw_json(self):
        events = c.parse_codex({"type": "response_item", "payload": {"type": "function_call",
            "name": "request_user_input_async", "call_id": "call-history",
            "arguments": json.dumps({"questions": prompt()["questions"]})}}, 1)
        self.assertEqual(events[0]["kind"], "question")
        self.assertTrue(events[0]["historical"])

    def test_malformed_metadata_does_not_become_actionable(self):
        for questions in ([{}], ["bad"], [{"title": "Question", "options": [None]}]):
            self.assertEqual(c._async_question_fields(questions), [])
