import os
from pathlib import Path
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

import codex_console


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        codex_console._models_cache.update(
            {"success_t": 0.0, "attempt_t": 0.0, "v": None})

    def test_probe_uses_model_slug_filters_hidden_and_follows_pages(self):
        fake_server = textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            def emit(message):
                print(json.dumps(message), flush=True)

            for line in sys.stdin:
                request = json.loads(line)
                method = request.get("method")
                if method == "initialize":
                    emit({"method": "server/notification", "params": {}})
                    emit({"id": request["id"], "result": {"ready": True}})
                elif method == "model/list":
                    cursor = request.get("params", {}).get("cursor")
                    if not cursor:
                        data = [
                            {"id": "catalog-new", "model": "gpt-new", "displayName": "GPT New",
                             "description": "new", "hidden": False, "isDefault": True,
                             "supportedReasoningEfforts": [
                                 {"reasoningEffort": "high", "description": "deep"}],
                             "defaultReasoningEffort": "high"},
                            {"id": "hidden", "model": "gpt-hidden", "displayName": "Hidden",
                             "hidden": True, "isDefault": False,
                             "supportedReasoningEfforts": [], "defaultReasoningEffort": "low"},
                        ]
                        next_cursor = "page-2"
                    else:
                        data = [
                            {"id": "duplicate", "model": "gpt-new", "displayName": "Duplicate",
                             "hidden": False, "isDefault": False,
                             "supportedReasoningEfforts": [], "defaultReasoningEffort": "low"},
                            {"id": "gpt-next", "displayName": "GPT Next", "hidden": False,
                             "isDefault": False, "supportedReasoningEfforts": [],
                             "defaultReasoningEffort": "medium"},
                        ]
                        next_cursor = None
                    emit({"id": request["id"],
                          "result": {"data": data, "nextCursor": next_cursor}})
            """)

        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp, "fake-codex")
            executable.write_text(fake_server, encoding="utf-8")
            executable.chmod(0o755)
            with mock.patch.object(codex_console, "CODEX_BIN", os.fspath(executable)):
                models = codex_console._probe_codex_models(timeout=3)

        self.assertEqual([model["id"] for model in models], ["gpt-new", "gpt-next"])
        self.assertEqual(models[0]["name"], "GPT New")
        self.assertTrue(models[0]["isDefault"])
        self.assertEqual(models[0]["reasoningEfforts"], ["high"])

    def test_fetch_models_keeps_last_good_catalog_on_refresh_failure(self):
        catalog = [{"id": "gpt-new", "name": "GPT New", "isDefault": True}]
        with mock.patch.object(
                codex_console, "_probe_codex_models",
                side_effect=[catalog, RuntimeError("offline")]):
            self.assertEqual(codex_console.fetch_models(), catalog)
            codex_console._models_cache.update({"success_t": 0.0, "attempt_t": 0.0})
            self.assertEqual(codex_console.fetch_models(), catalog)
            self.assertEqual(codex_console.fetch_models(), catalog)

    def test_concurrent_failure_is_single_flight_and_negative_cached(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def fail_probe():
            calls.append(True)
            started.set()
            release.wait(timeout=2)
            raise RuntimeError("offline")

        result = []
        with mock.patch.object(codex_console, "_probe_codex_models", side_effect=fail_probe):
            first = threading.Thread(target=lambda: result.append(codex_console.fetch_models()))
            first.start()
            self.assertTrue(started.wait(timeout=1))
            result.extend(codex_console.fetch_models() for _ in range(5))
            release.set()
            first.join(timeout=2)
            result.append(codex_console.fetch_models())

        self.assertFalse(first.is_alive())
        self.assertEqual(len(calls), 1)
        self.assertEqual(result, [None] * 7)

    def test_failed_probe_retries_after_backoff(self):
        catalog = [{"id": "gpt-recovered", "name": "Recovered", "isDefault": True}]
        with mock.patch.object(
                codex_console, "_probe_codex_models",
                side_effect=[RuntimeError("offline"), catalog]) as probe:
            self.assertIsNone(codex_console.fetch_models())
            self.assertIsNone(codex_console.fetch_models())
            self.assertEqual(probe.call_count, 1)
            codex_console._models_cache["attempt_t"] = 0.0
            self.assertEqual(codex_console.fetch_models(), catalog)
            self.assertEqual(probe.call_count, 2)

    def test_effort_validation_adapts_to_selected_model(self):
        codex_console._models_cache["v"] = [
            {"id": "gpt-sol", "isDefault": True,
             "reasoningEfforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
             "defaultReasoningEffort": "low"},
            {"id": "gpt-luna", "isDefault": False,
             "reasoningEfforts": ["low", "medium", "high", "xhigh", "max"],
             "defaultReasoningEffort": "medium"},
            {"id": "gpt-old", "isDefault": False,
             "reasoningEfforts": ["low", "medium", "high", "xhigh"],
             "defaultReasoningEffort": "medium"},
        ]

        self.assertEqual(codex_console._sanitize_effort("ultra", "gpt-sol"), "ultra")
        self.assertEqual(codex_console._sanitize_effort("max", "gpt-luna"), "max")
        self.assertEqual(codex_console._sanitize_effort("ultra", "gpt-luna"), "medium")
        self.assertEqual(codex_console._sanitize_effort("ultra", "gpt-old"), "medium")
        self.assertEqual(codex_console._sanitize_effort("", "default"), "low")
        with mock.patch.object(
                codex_console, "_configured_default_model", return_value="gpt-old"):
            self.assertEqual(
                codex_console._sanitize_effort("ultra", "default"), "medium")

    def test_switching_model_falls_back_to_new_models_default_effort(self):
        codex_console._models_cache["v"] = [
            {"id": "gpt-sol", "isDefault": True,
             "reasoningEfforts": ["low", "medium", "high", "xhigh", "max", "ultra"],
             "defaultReasoningEffort": "low"},
            {"id": "gpt-old", "isDefault": False,
             "reasoningEfforts": ["low", "medium", "high", "xhigh"],
             "defaultReasoningEffort": "medium"},
        ]
        session = codex_console.ChatSession(
            "test", os.getcwd(), "gpt-sol", "full-access", effort="ultra")

        session.set_model("gpt-old")

        self.assertEqual(session.model, "gpt-old")
        self.assertEqual(session.effort, "medium")

    def test_both_pickers_use_the_shared_dynamic_catalog(self):
        html = codex_console.CONSOLE_HTML
        self.assertIn('<option value="default">model: default</option>', html)
        self.assertIn("fetch('api/models'", html)
        self.assertIn("modelOptionsFor(currentModel)", html)
        self.assertIn("setInterval(loadModels,300000)", html)
        self.assertIn("saved; unavailable", html)
        self.assertIn("localStorage.setItem('al_model','default')", html)
        self.assertIn("effortOptionsFor(currentEffortModel())", html)
        self.assertIn("effortOptionsFor(initialEffortModel)", html)
        self.assertIn("if(!replaying)activeModel=", html)
        self.assertIn("DEFAULT_MODEL_ID=j.defaultModel", html)
        self.assertNotIn("const EFFORTS=", html)
        self.assertNotIn('<option>gpt-5.5</option>', html)


if __name__ == "__main__":
    unittest.main()
