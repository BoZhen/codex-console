import inspect
import os
import unittest

import codex_console


def _session():
    session = codex_console.ChatSession(
        "sid", os.getcwd(), "default", "full-access", effort="xhigh")
    emitted = []
    session._emit = lambda event: emitted.append(event)
    return session, emitted


def _emitted_events(messages):
    return [event
            for message in messages if message.get("type") == "events"
            for event in message.get("events", [])]


class AppServerV2EventTests(unittest.TestCase):
    def test_streamed_agent_message_is_updated_not_duplicated_on_completion(self):
        session, _ = _session()

        session._on_note("item/started", {
            "item": {"type": "agentMessage", "id": "msg-1", "text": ""}})
        session._on_note("item/agentMessage/delta", {
            "itemId": "msg-1", "delta": "hel"})
        session._on_note("item/agentMessage/delta", {
            "itemId": "msg-1", "delta": "lo"})
        session._on_note("item/completed", {
            "item": {"type": "agentMessage", "id": "msg-1", "text": "hello"}})

        self.assertEqual(
            [event["kind"] for event in session.log],
            ["assistant_delta", "assistant_delta"])

    def test_incremental_attach_uses_monotonic_event_sequences(self):
        session, _ = _session()
        session._push([{"kind": "notice", "text": "one"}])
        first_seq = session.log[-1]["_seq"]
        session._push([{"kind": "notice", "text": "two"}])

        events, incremental = session.events_since(first_seq)

        self.assertTrue(incremental)
        self.assertEqual([event["text"] for event in events], ["two"])
        self.assertGreater(events[0]["_seq"], first_seq)

    def test_completed_item_bookkeeping_is_bounded(self):
        session, _ = _session()
        for index in range(codex_console.ITEM_HISTORY_LIMIT + 40):
            session._items[f"done-{index}"] = {
                "completed": True,
                "text": "x" * 100,
            }
        session._items["active"] = {"completed": False, "text": "running"}

        session._prune_items(force=True)

        completed = [item for item in session._items.values()
                     if item.get("completed")]
        self.assertEqual(len(completed), codex_console.ITEM_HISTORY_LIMIT)
        self.assertIn("active", session._items)
        self.assertNotIn("done-0", session._items)

    def test_command_actions_classify_pipeline_reads_as_read(self):
        session, _ = _session()
        command = "nl -ba codex_console.py | sed -n '390,470p'"

        session._on_note("item/started", {
            "item": {
                "type": "commandExecution",
                "id": "cmd-1",
                "command": ["bash", "-lc", command],
                "cwd": os.getcwd(),
                "commandActions": [
                    {"type": "read", "path": "codex_console.py", "command": command}
                ],
            }})

        event = session.log[-1]
        self.assertEqual(event["kind"], "tool_use")
        self.assertEqual(event["tool"], "Read")
        self.assertEqual(event["input"]["command"], command)
        self.assertEqual(event["input"]["display"], "codex_console.py")

    def test_command_approval_uses_same_semantic_tool_classification(self):
        session, _ = _session()
        command = "nl -ba codex_console.py | sed -n '390,470p'"

        session._on_request("rpc-1", "item/commandExecution/requestApproval", {
            "itemId": "cmd-1",
            "command": ["bash", "-lc", command],
            "cwd": os.getcwd(),
            "commandActions": [
                {"type": "read", "path": "codex_console.py", "command": command}
            ],
        })

        event = session.log[-1]
        self.assertEqual(event["kind"], "approval")
        self.assertEqual(event["tool"], "Read")
        self.assertEqual(event["input"]["command"], command)
        self.assertEqual(event["input"]["display"], "codex_console.py")

    def test_plan_diff_and_settings_notifications_are_surfaced(self):
        session, emitted = _session()

        session._on_note("turn/plan/updated", {
            "turnId": "turn-1",
            "explanation": "validate first",
            "plan": [{"step": "inspect schema", "status": "in_progress"}],
        })
        session._on_note("turn/diff/updated", {
            "turnId": "turn-1",
            "diff": "diff --git a/a.py b/a.py\n+print('x')",
        })
        session._on_note("thread/settings/updated", {
            "threadSettings": {
                "model": "gpt-5.6-sol",
                "effort": "high",
                "cwd": os.getcwd(),
            }})

        self.assertEqual(session.log[0]["kind"], "plan")
        self.assertEqual(session.display_model, "gpt-5.6-sol")
        self.assertEqual(session.effort, "high")
        kinds = [event["kind"] for event in _emitted_events(emitted)]
        self.assertIn("plan", kinds)
        self.assertIn("turn_diff", kinds)
        self.assertIn("settings", kinds)

    def test_web_search_uses_action_payload_when_query_field_is_empty(self):
        session, _ = _session()

        session._on_note("item/started", {
            "item": {
                "type": "webSearch",
                "id": "web-1",
                "query": "",
                "action": {"type": "search", "query": None,
                           "queries": ["codex app-server turn steer", "codex subagents"]},
            }})
        session._on_note("item/completed", {
            "item": {
                "type": "webSearch",
                "id": "web-1",
                "query": "",
                "status": "completed",
                "action": {"type": "search",
                           "queries": ["codex app-server turn steer"]},
                "results": None,
            }})

        use, result = session.log[-2:]
        self.assertEqual(use["kind"], "tool_use")
        self.assertEqual(use["tool"], "web_search")
        self.assertEqual(use["input"]["query"], "codex app-server turn steer")
        self.assertEqual(use["input"]["queries"][1], "codex subagents")
        self.assertNotEqual(use["input"].get("display"), "")
        self.assertEqual(result["kind"], "tool_result")
        self.assertEqual(result["content"], "")
        self.assertEqual(result["status"], "completed")

    def test_historical_web_search_call_is_normalized(self):
        events = codex_console.parse_codex({
            "timestamp": "2026-08-31T00:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "web_search_call",
                "status": "completed",
                "action": {"type": "open_page",
                           "url": "https://learn.chatgpt.com/docs/app-server"},
            },
        }, 7)

        self.assertEqual([event["kind"] for event in events], ["tool_use", "tool_result"])
        self.assertEqual(events[0]["tool"], "web_search")
        self.assertEqual(events[0]["input"]["action"], "openPage")
        self.assertEqual(events[0]["input"]["url"], "https://learn.chatgpt.com/docs/app-server")

    def test_frontend_web_search_rendering_handles_query_and_empty_results(self):
        html = codex_console.CONSOLE_HTML

        self.assertIn("if(i.query)return", html)
        self.assertIn("if(t==='web_search')", html)
        self.assertIn("else if(resultBits(ev).length)", html)

    def test_collab_agent_updates_subagent_snapshot(self):
        session, emitted = _session()

        session._on_note("item/started", {
            "item": {
                "type": "collabAgentToolCall",
                "id": "agent-1",
                "tool": "spawn_agent",
                "status": "running",
                "receiverThreadIds": ["child-1"],
                "prompt": "audit the renderer",
                "model": "gpt-5.6-sol",
                "reasoningEffort": "high",
                "agentsStates": {
                    "child-1": {"status": "running", "message": "reading files"}
                },
            }})

        snap = session.subagents_snapshot()
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["threadId"], "child-1")
        self.assertEqual(snap[0]["state"], "running")
        self.assertTrue(snap[0]["active"])
        self.assertEqual(snap[0]["prompt"], "audit the renderer")
        self.assertEqual(snap[0]["message"], "reading files")
        self.assertIn("subagents", [e["kind"] for e in _emitted_events(emitted)])
        self.assertEqual(session.log[-1]["tool"], "Agent")

    def test_subagent_activity_dedupes_and_marks_completed(self):
        session, _ = _session()
        item = {
            "type": "subAgentActivity",
            "id": "act-1",
            "kind": "completed",
            "agentThreadId": "child-1",
            "agentPath": "/root/reviewer",
        }

        session._on_note("item/started", {"item": item})
        session._on_note("item/completed", {"item": item})

        events = [e for e in session.log if e["kind"] == "subagent_activity"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["subagent"]["label"], "reviewer")
        self.assertFalse(session.subagents_snapshot()[0]["active"])

    def test_foreign_subagent_item_is_filtered_from_main_chat(self):
        session, _ = _session()
        session.thread_id = "parent-1"

        session._on_note("item/completed", {
            "threadId": "child-1",
            "item": {
                "type": "agentMessage",
                "id": "msg-1",
                "text": "child detail should stay in the panel",
            }})

        self.assertFalse(any(e.get("kind") == "assistant_text" for e in session.log))
        snap = session.subagents_snapshot()
        self.assertEqual(snap[0]["threadId"], "child-1")
        self.assertIn("child detail", snap[0]["message"])

    def test_historical_subagent_activity_is_normalized(self):
        events = codex_console.parse_codex({
            "timestamp": "2026-08-31T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "sub_agent_activity",
                "event_id": "call-1",
                "occurred_at_ms": 1788150000123,
                "agent_thread_id": "child-1",
                "agent_path": "/root/executor_t1",
                "kind": "started",
            },
        }, 11)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "subagent_activity")
        self.assertEqual(events[0]["subagent"]["threadId"], "child-1")
        self.assertEqual(events[0]["subagent"]["label"], "executor_t1")
        self.assertTrue(events[0]["subagent"]["active"])

    def test_subagent_item_message_summarizes_command(self):
        msg = codex_console._subagent_item_message({
            "turnId": "turn-1",
            "item": {
                "type": "commandExecution",
                "id": "cmd-1",
                "command": "nl -ba codex_console.py | sed -n '1,3p'",
                "commandActions": [
                    {"type": "read", "path": "codex_console.py"}
                ],
                "aggregatedOutput": "1 line",
                "status": "completed",
            },
        })

        self.assertEqual(msg["role"], "tool")
        self.assertIn("Read codex_console.py", msg["txt"])
        self.assertIn("1 line", msg["txt"])

    def test_frontend_subagent_panel_hooks_are_present(self):
        html = codex_console.CONSOLE_HTML

        self.assertIn('id="agentbtn"', html)
        self.assertIn('id="agentPanel"', html)
        self.assertIn("subagent_read", html)
        self.assertIn("subagent_thread", html)
        self.assertIn("addSubagentMarker", html)

    def test_frontend_plan_is_pinned_and_updates_in_place(self):
        html = codex_console.CONSOLE_HTML

        self.assertIn('id="planDock"', html)
        self.assertIn(".plandock{position:sticky", html)
        self.assertIn("const id='plan-current'", html)
        self.assertIn("host.hidden=false", html)
        self.assertIn("return ['done','●']", html)
        self.assertIn("return ['active','◐']", html)
        self.assertIn("function togglePlanCollapsed()", html)
        self.assertIn("class=\"ptoggle\"", html)
        self.assertIn("aria-expanded=\"'+(!planCollapsed)+'\"", html)
        self.assertIn("filter(x=>planStepClass(x.status)[0]==='active')", html)
        self.assertIn("No active task", html)
        self.assertIn("(planCollapsed?'🔽':'🔼')", html)
        self.assertNotIn("(planCollapsed?'▸':'▾')", html)
        self.assertIn("const PLAN_HIDE_MS=2000", html)
        self.assertIn("function planAllCompleted(plan)", html)
        self.assertIn("function schedulePlanAutoHide(rec)", html)

    def test_streaming_math_is_batched_and_typeset_only_when_stable(self):
        html = codex_console.CONSOLE_HTML

        self.assertIn("const STREAM_RENDER_MS=50", html)
        self.assertIn("function scheduleAsstRender()", html)
        self.assertIn("function flushAsstRenders(final)", html)
        self.assertIn("if(final)typesetMath(rec.el,!replaying)", html)
        self.assertIn("flushAsstRenders(true);compacting=false", html)
        self.assertIn(".msg.asst.streaming .b{text-wrap:wrap}", html)
        self.assertNotIn(
            "b.innerHTML=md(rec.text);typesetMath(rec.el)", html)

    def test_fenced_latex_and_tex_remain_source_code(self):
        html = codex_console.CONSOLE_HTML

        self.assertIn("if(lang==='math'){", html)
        self.assertNotIn("lang==='latex'||lang==='tex'", html)
        self.assertIn("bl.push('<pre><code>'+esc(c.replace", html)

    def test_session_tabs_open_live_sessions_without_resuming(self):
        html = codex_console.CONSOLE_HTML
        socket_source = inspect.getsource(codex_console.ChatSocket.on_message)

        self.assertIn('id="sessionTabs"', html)
        self.assertIn('role="tablist"', html)
        self.assertIn("function ensureSessionTab(s,active)", html)
        self.assertIn("function closeSessionTab(id)", html)
        self.assertIn("function openLiveSession(s)", html)
        self.assertIn("sessionViewCache=new Map()", html)
        self.assertIn("function stashSessionView(id)", html)
        self.assertIn("function restoreSessionView(id)", html)
        self.assertIn("ensureSessionTab(s,false);switchSession(s.id)", html)
        self.assertIn("const req={type:'attach',id:id}", html)
        self.assertIn("if(cached)req.after_seq", html)
        self.assertIn("wsSend({type:'detach'})", html)
        self.assertNotIn("stab-project", html)
        self.assertIn('elif mt == "detach":', socket_source)
        self.assertIn("self.session.detach(self)", socket_source)

    def test_chat_dom_window_is_bounded_without_pruning_live_controls(self):
        html = codex_console.CONSOLE_HTML

        self.assertIn("CHAT_WINDOW_ITEMS=320", html)
        self.assertIn("CHAT_WINDOW_CHARS=750000", html)
        self.assertIn("function trimChatWindow(force)", html)
        self.assertIn("function windowProtected(el)", html)
        self.assertIn(".tool:not(.done)", html)
        self.assertIn(".approval:not(.done)", html)
        self.assertIn("Search full session history", html)

    def test_file_change_patch_updates_existing_change_card(self):
        session, _ = _session()

        session._on_note("item/fileChange/patchUpdated", {
            "itemId": "patch-1",
            "changes": [{"path": "a.py", "kind": {"type": "update"}, "diff": "+old"}],
        })
        session._on_note("item/fileChange/patchUpdated", {
            "itemId": "patch-1",
            "changes": [{"path": "a.py", "kind": {"type": "update"}, "diff": "+new"}],
        })

        self.assertEqual(session.log[0]["kind"], "tool_use")
        self.assertEqual(session.log[0]["tool"], "apply_patch")
        self.assertEqual(session.log[0]["input"]["file_path"], "a.py")
        self.assertEqual(session.log[1]["kind"], "tool_update")
        self.assertIn("+new", session.log[1]["input"]["diff"])

    def test_recap_transcript_includes_streamed_assistant_text(self):
        session, _ = _session()
        session.log = [
            {"kind": "user_text", "text": "upgrade the console"},
            {"kind": "assistant_delta", "itemId": "msg-1", "delta": "done"},
            {"kind": "assistant_delta", "itemId": "msg-1", "delta": " now"},
        ]

        transcript = session._recap_transcript()

        self.assertIn("User: upgrade the console", transcript)
        self.assertIn("Assistant: done now", transcript)


if __name__ == "__main__":
    unittest.main()
