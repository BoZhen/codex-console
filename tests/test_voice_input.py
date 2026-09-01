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
                        print(json.dumps({
                            "id": request["id"], "ok": True,
                            "text": "hello", "language": "en", "argv": sys.argv,
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
                first = manager.transcribe(audio)
                worker_pid = manager.proc.pid
                second = manager.transcribe(audio)
                self.assertEqual(first["text"], "hello")
                self.assertIn("tw2sp", first["argv"])
                self.assertIn("--pause-punctuation", first["argv"])
                self.assertEqual(second["language"], "en")
                self.assertEqual(manager.proc.pid, worker_pid)
            finally:
                manager.shutdown()

    def test_pause_timestamps_add_punctuation_without_changing_words(self):
        segment = SimpleNamespace(
            text="重新启动服务",
            words=[
                SimpleNamespace(word="重新", start=0.0, end=0.4),
                SimpleNamespace(word="启动", start=0.45, end=0.9),
                SimpleNamespace(word="服务", start=1.7, end=2.1),
                SimpleNamespace(word="现在", start=3.7, end=4.1),
            ],
        )
        text = faster_whisper_worker._join_segments(
            [segment], "zh", pause_punctuation=True)
        self.assertEqual(text, "重新启动，服务。现在。")

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


class _FakeTranscriber:
    def __init__(self):
        self.path = ""
        self.body = b""

    def transcribe(self, path):
        self.path = path
        with open(path, "rb") as handle:
            self.body = handle.read()
        return {
            "ok": True,
            "text": "测试语音",
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
        return Application([(r"/api/transcribe", codex_console.TranscribeHandler)])

    def test_capabilities_and_audio_upload(self):
        capabilities = self.fetch("/api/transcribe")
        self.assertEqual(capabilities.code, 200)
        self.assertTrue(json.loads(capabilities.body)["available"])

        response = self.fetch(
            "/api/transcribe", method="POST", body=b"voice",
            headers={"Content-Type": "audio/webm", "X-Audio-Duration-Ms": "1000"})
        payload = json.loads(response.body)
        self.assertEqual(response.code, 200)
        self.assertEqual(payload["text"], "测试语音")
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
        self.assertEqual(response.code, 401)
        self.assertEqual(self.fake.path, "")


class VoiceFrontendContractTests(unittest.TestCase):
    def test_voice_input_is_editable_session_scoped_and_never_auto_sends(self):
        html = codex_console.CONSOLE_HTML
        self.assertIn('id="voiceBtn"', html)
        self.assertIn("navigator.mediaDevices.getUserMedia", html)
        self.assertIn("new MediaRecorder", html)
        self.assertIn("fetch('api/transcribe'", html)
        self.assertIn("targetSid:sid", html)
        self.assertIn("insertVoiceDraft(job,j.text||'')", html)
        self.assertNotIn("insertVoiceDraft(job,j.text||'');sendMsg()", html)
        self.assertIn("window.isSecureContext", html)
        self.assertIn("setTimeout(stopVoiceInput,voiceCaps.maxSeconds*1000)", html)
        self.assertIn("!e.altKey||e.ctrlKey||e.metaKey||e.shiftKey", html)
        self.assertIn("toggleVoiceInput();},true)", html)


if __name__ == "__main__":
    unittest.main()
