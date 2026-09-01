import json
import os
import sys
import tempfile
import textwrap
import unittest
from types import SimpleNamespace
from unittest import mock

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

import codex_console
import faster_whisper_worker


class TranscriberManagerTests(unittest.TestCase):
    def test_worker_is_reused_and_returns_structured_text(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = os.path.join(directory, "worker.py")
            with open(worker, "w", encoding="utf-8") as handle:
                handle.write(textwrap.dedent("""
                    import json
                    import sys

                    for line in sys.stdin:
                        request = json.loads(line)
                        if request["type"] == "warmup":
                            print(json.dumps({
                                "id": request["id"], "ok": True,
                                "warmed": True, "elapsed": 0.1,
                            }), flush=True)
                            continue
                        print(json.dumps({
                            "id": request["id"], "ok": True,
                            "text": "hello", "language": "en", "argv": sys.argv,
                            "final": request["final"],
                            "stream_id": request["stream_id"],
                            "duration_hint": request["duration_hint"],
                        }), flush=True)
                """))
            audio = os.path.join(directory, "voice.webm")
            with open(audio, "wb") as handle:
                handle.write(b"voice")
            manager = codex_console.TranscriberManager(
                python_path=sys.executable,
                worker_path=worker,
                model="test-model",
                chinese_conversion="tw2sp",
                pause_punctuation=True,
                idle_seconds=60,
                timeout_seconds=5,
                library_path="",
            )
            try:
                warmed = manager.warmup()
                worker_pid = manager.proc.pid
                first = manager.transcribe(
                    audio, final=False, stream_id="voice-1", duration_hint=2.5)
                second = manager.transcribe(audio)
                self.assertTrue(warmed["warmed"])
                self.assertEqual(first["text"], "hello")
                self.assertIn("tw2sp", first["argv"])
                self.assertIn("--pause-punctuation", first["argv"])
                self.assertFalse(first["final"])
                self.assertEqual(first["stream_id"], "voice-1")
                self.assertEqual(first["duration_hint"], 2.5)
                self.assertEqual(second["language"], "en")
                self.assertTrue(second["final"])
                self.assertEqual(manager.proc.pid, worker_pid)
            finally:
                manager.shutdown()

    def test_pause_timestamps_add_punctuation_without_changing_words(self):
        self.assertEqual(faster_whisper_worker._COMMA_PAUSE_SECONDS, 0.5)
        self.assertEqual(faster_whisper_worker._PERIOD_PAUSE_SECONDS, 1.2)
        segment = SimpleNamespace(
            text="重新启动服务",
            words=[
                SimpleNamespace(word="重新", start=0.0, end=0.4),
                SimpleNamespace(word="启动", start=0.45, end=1.0),
                SimpleNamespace(word="服务", start=1.5, end=2.0),
                SimpleNamespace(word="现在", start=3.2, end=3.7),
            ],
        )
        text = faster_whisper_worker._join_segments(
            [segment], "zh", pause_punctuation=True)
        self.assertEqual(text, "重新启动，服务。现在。")
        self.assertEqual(
            faster_whisper_worker._join_segments(
                [segment], "zh", pause_punctuation=True, final=False),
            "重新启动，服务。现在",
        )

        below_thresholds = SimpleNamespace(
            text="甲乙丙丁",
            words=[
                SimpleNamespace(word="甲", start=0.0, end=1.0),
                SimpleNamespace(word="乙", start=1.49, end=2.0),
                SimpleNamespace(word="丙", start=3.19, end=4.0),
                SimpleNamespace(word="丁", start=5.2, end=5.5),
            ],
        )
        self.assertEqual(
            faster_whisper_worker._join_segments(
                [below_thresholds], "zh", pause_punctuation=True),
            "甲乙，丙。丁。",
        )

        punctuated = SimpleNamespace(text="已经完成。", words=[])
        self.assertEqual(
            faster_whisper_worker._join_segments(
                [punctuated], "zh", pause_punctuation=True),
            "已经完成。",
        )
        self.assertEqual(
            faster_whisper_worker._ensure_terminal_punctuation(
                "然后，", is_chinese=True),
            "然后。",
        )
        self.assertEqual(
            faster_whisper_worker._ensure_terminal_punctuation(
                "是否可以根据结束的词补充问号呢", is_chinese=True),
            "是否可以根据结束的词补充问号呢？",
        )
        self.assertEqual(
            faster_whisper_worker._ensure_terminal_punctuation(
                "我不知道为什么", is_chinese=True),
            "我不知道为什么。",
        )
        self.assertEqual(
            faster_whisper_worker._ensure_terminal_punctuation(
                "What model is this", is_chinese=False),
            "What model is this?",
        )
        self.assertEqual(
            faster_whisper_worker._ensure_terminal_punctuation(
                "If I restart the service will it interrupt the session",
                is_chinese=False),
            "If I restart the service will it interrupt the session?",
        )
        self.assertEqual(
            faster_whisper_worker._ensure_terminal_punctuation(
                "If it works, restart the service", is_chinese=False),
            "If it works, restart the service.",
        )

    def test_live_prefix_keeps_a_revisable_tail_for_word_corrections(self):
        self.assertEqual(faster_whisper_worker._LIVE_REVISABLE_CJK_CHARS, 10)
        previous = "发音正确，但词粗还需要根据后面的上下文继续确认完整内容"
        current = "发音正确，但词组还需要根据后面的上下文继续确认完整内容"
        stable = faster_whisper_worker._stable_live_prefix(
            previous, current, confirmed="", language="zh")
        self.assertEqual(stable, "发音正确，但词")
        self.assertTrue(current.startswith(stable))
        self.assertNotIn("词粗", current[len(stable):])

    def test_confirmed_words_and_punctuation_cannot_regress(self):
        previous = "已经纠正为词组，以及逗号，句号。后面的内容还在继续"
        confirmed = "已经纠正为词组，以及逗号，句号。"
        regressed = "已经纠正为词粗以及逗号。句号，后面的内容还在继续增加"
        merged = faster_whisper_worker._preserve_confirmed_prefix(
            previous, regressed, confirmed)
        self.assertTrue(merged.startswith(confirmed))
        self.assertNotIn("词粗", merged)
        self.assertIn("后面的内容还在继续增加", merged)
        advanced = faster_whisper_worker._stable_live_prefix(
            previous, merged, confirmed, language="zh")
        self.assertTrue(advanced.startswith(confirmed))
        self.assertLessEqual(len(merged) - len(advanced), 10)

    def test_early_restart_word_locks_before_a_late_regression(self):
        correct = "重启完了，测试测试，看看能不能回车发送"
        stable = faster_whisper_worker._stable_live_prefix(
            correct, correct, confirmed="", language="zh")
        self.assertTrue(stable.startswith("重启"))
        self.assertLessEqual(len(correct) - len(stable), 10)
        regressed = "充气完了，测试测试，看看能不能回车发送"
        merged = faster_whisper_worker._preserve_confirmed_prefix(
            correct, regressed, stable)
        self.assertTrue(merged.startswith("重启"))
        self.assertNotIn("充气", merged)

    def test_live_state_cache_expires_and_remains_bounded(self):
        states = {
            "stale": {"seen": 0},
            **{"voice-%d" % index: {"seen": 100 + index}
               for index in range(18)},
        }
        faster_whisper_worker._purge_live_states(states, now=400)
        self.assertNotIn("stale", states)
        self.assertLessEqual(len(states), faster_whisper_worker._LIVE_STATE_LIMIT)


class _FakeTranscriber:
    def __init__(self):
        self.path = ""
        self.body = b""
        self.calls = []
        self.warmups = 0

    def warmup(self):
        self.warmups += 1
        return {"ok": True, "warmed": True, "elapsed": 0.1}

    def transcribe(self, path, final=True, stream_id="", duration_hint=None):
        self.path = path
        with open(path, "rb") as handle:
            self.body = handle.read()
        self.calls.append({
            "final": final,
            "stream_id": stream_id,
            "duration_hint": duration_hint,
        })
        return {
            "ok": True,
            "text": "测试语音",
            "stable_text": "测试" if not final else "",
            "language": "zh",
            "duration": 1.0,
            "elapsed": 0.1,
        }


class TranscribeHandlerTests(AsyncHTTPTestCase):
    def setUp(self):
        self.model_dir = tempfile.TemporaryDirectory()
        with open(os.path.join(self.model_dir.name, "model.bin"), "wb") as handle:
            handle.write(b"model")
        self.fake = _FakeTranscriber()
        self.patchers = [
            mock.patch.object(codex_console, "AUTH", ""),
            mock.patch.object(codex_console, "TRANSCRIBE_ENABLED", True),
            mock.patch.object(codex_console, "TRANSCRIBE_PYTHON", sys.executable),
            mock.patch.object(codex_console, "TRANSCRIBE_MODEL", self.model_dir.name),
            mock.patch.object(codex_console, "TRANSCRIBE_WORKER",
                              os.path.join(os.path.dirname(codex_console.__file__),
                                           "faster_whisper_worker.py")),
            mock.patch.object(codex_console, "TRANSCRIBE_MAX_BYTES", 32),
            mock.patch.object(codex_console, "_TRANSCRIBER", self.fake),
        ]
        for patcher in self.patchers:
            patcher.start()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.model_dir.cleanup()

    def get_app(self):
        return Application([
            (r"/api/transcribe/warmup", codex_console.TranscribeWarmupHandler),
            (r"/api/transcribe", codex_console.TranscribeHandler),
        ])

    def test_capabilities_and_audio_upload(self):
        capabilities = self.fetch("/api/transcribe")
        self.assertEqual(capabilities.code, 200)
        capability_payload = json.loads(capabilities.body)
        self.assertTrue(capability_payload["available"])
        self.assertTrue(capability_payload["livePreview"])
        self.assertGreaterEqual(capability_payload["previewIntervalMs"], 1000)

        warmup = self.fetch(
            "/api/transcribe/warmup", method="POST", body=b"")
        self.assertEqual(warmup.code, 200)
        self.assertTrue(json.loads(warmup.body)["warmed"])
        self.assertEqual(self.fake.warmups, 1)

        partial = self.fetch(
            "/api/transcribe?partial=1", method="POST", body=b"voice",
            headers={
                "Content-Type": "audio/webm",
                "X-Audio-Duration-Ms": "2500",
                "X-Transcription-Stream": "voice-test!",
            })
        partial_payload = json.loads(partial.body)
        self.assertEqual(partial.code, 200)
        self.assertTrue(partial_payload["partial"])
        self.assertEqual(partial_payload["stableText"], "测试")
        self.assertEqual(self.fake.calls[-1], {
            "final": False,
            "stream_id": "voice-test",
            "duration_hint": 2.5,
        })

        response = self.fetch(
            "/api/transcribe", method="POST", body=b"voice",
            headers={
                "Content-Type": "audio/webm",
                "X-Audio-Duration-Ms": "1000",
                "X-Transcription-Stream": "voice-test",
            })
        payload = json.loads(response.body)
        self.assertEqual(response.code, 200)
        self.assertEqual(payload["text"], "测试语音")
        self.assertFalse(payload["partial"])
        self.assertTrue(self.fake.calls[-1]["final"])
        self.assertEqual(self.fake.body, b"voice")
        self.assertFalse(os.path.exists(self.fake.path))

    def test_upload_limits_and_media_type_are_enforced(self):
        oversized = self.fetch(
            "/api/transcribe", method="POST", body=b"x" * 33,
            headers={"Content-Type": "audio/webm"})
        unsupported = self.fetch(
            "/api/transcribe", method="POST", body=b"voice",
            headers={"Content-Type": "text/plain"})
        self.assertEqual(oversized.code, 413)
        self.assertEqual(unsupported.code, 415)

    def test_authentication_is_checked_before_audio_is_accepted(self):
        with mock.patch.object(codex_console, "AUTH", "user:password"):
            response = self.fetch(
                "/api/transcribe", method="POST", body=b"voice",
                headers={"Content-Type": "audio/webm"})
            warmup = self.fetch(
                "/api/transcribe/warmup", method="POST", body=b"")
        self.assertEqual(response.code, 401)
        self.assertEqual(warmup.code, 401)
        self.assertEqual(self.fake.path, "")
        self.assertEqual(self.fake.warmups, 0)


class VoiceFrontendContractTests(unittest.TestCase):
    def test_voice_input_is_editable_session_scoped_and_never_auto_sends(self):
        html = codex_console.CONSOLE_HTML
        self.assertIn('id="voiceBtn"', html)
        self.assertIn("const VOICE_MIC_ICON=", html)
        self.assertIn("/static/icons/fluent-studio-microphone.png", html)
        self.assertIn("stroke-width:2.25", html)
        self.assertIn("navigator.mediaDevices.getUserMedia", html)
        self.assertIn("new MediaRecorder", html)
        self.assertIn("fetch('api/transcribe?partial=1'", html)
        self.assertIn("fetch('api/transcribe'", html)
        self.assertIn("fetch('api/transcribe/warmup'", html)
        self.assertIn("targetSid:sid", html)
        self.assertIn("applyVoiceHypothesis(job,j.text||'',j.stableText||'',false)", html)
        self.assertIn("applyVoiceHypothesis(job,j.text||'','',true)", html)
        self.assertIn("restoreVoicePreview(job)", html)
        self.assertIn("ta.readOnly=locked", html)
        self.assertIn("voiceJob&&voiceJob.targetSid===sid", html)
        self.assertIn("for(let attempt=0;attempt<6;attempt++)", html)
        self.assertIn("r.status!==429", html)
        self.assertIn("if(!voiceJob&&job.targetSid===sid)ta.focus()", html)
        self.assertNotIn("applyVoiceHypothesis(job,j.text||'','',true);sendMsg()", html)
        start = html[html.index("async function startVoiceInput"):]
        self.assertLess(
            start.index("job.warmPromise=warmVoiceModel()"),
            start.index("navigator.mediaDevices.getUserMedia"),
        )
        self.assertIn("window.isSecureContext", html)
        self.assertIn("setTimeout(stopVoiceInput,voiceCaps.maxSeconds*1000)", html)
        self.assertIn("!e.altKey||e.ctrlKey||e.metaKey||e.shiftKey", html)
        self.assertIn("toggleVoiceInput();},true)", html)


if __name__ == "__main__":
    unittest.main()
