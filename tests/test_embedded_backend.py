import io
import os
from pathlib import Path
import tempfile
import types
import unittest
import subprocess
import sys
from unittest import mock

import tornado.testing
import tornado.web
import codex_console as c


class EmbeddedRoutesTests(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        class FixtureHandler(tornado.web.RequestHandler):
            def get(self):
                self.write({"fixture": True})
        return c.make_application([(r"/embedded-fixture", FixtureHandler)])

    def test_extra_routes_do_not_replace_the_console(self):
        response = self.fetch('/embedded-fixture')
        self.assertEqual(response.code, 200)
        self.assertIn(b'"fixture": true', response.body)
        with mock.patch.object(c, 'AUTH', ''):
            response = self.fetch('/')
        self.assertEqual(response.code, 200)
        self.assertIn(b'<title>Codex Console</title>', response.body)


class EmbeddedStartupTests(unittest.TestCase):
    def test_embedded_state_paths_can_be_isolated_without_changing_cli_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, 'HOME': tmp, 'CODEX_HOME': tmp + '/cli',
                   'CODEX_CONSOLE_STATE_DIR': tmp + '/private-state',
                   'CODEX_CONSOLE_INDEX_DB': tmp + '/private-state/history.db'}
            output = subprocess.check_output([sys.executable, '-B', '-c',
                'import codex_console as c; print(c.CODEX_ROOT); print(c.NAMES_FILE); print(c.INDEX_DB)'],
                env=env, text=True)
            self.assertEqual(output.splitlines(), [tmp + '/cli/sessions',
                tmp + '/private-state/console-names.json', tmp + '/private-state/history.db'])

    def test_custom_listener_reuses_the_normal_startup_and_shutdown_hooks(self):
        app = mock.Mock()
        listener = mock.Mock()
        with mock.patch.object(c, 'migrate_session_favs_to_projects', return_value=0), \
                mock.patch.object(c.tornado.ioloop, 'IOLoop') as loop, \
                mock.patch.object(c.tornado.ioloop, 'PeriodicCallback'), \
                mock.patch.object(c.signal, 'signal') as signals, \
                mock.patch('builtins.print'):
            c.main(app, listener)
        listener.assert_called_once_with(app)
        app.listen.assert_not_called()
        loop.current().start.assert_called_once()
        self.assertEqual(signals.call_count, 2)

    def test_compressed_history_can_use_a_bundled_decoder_without_a_system_command(self):
        data = b'{"type":"fixture"}\n'
        decoder = mock.Mock()
        decoder.stream_reader.side_effect = lambda *a, **k: io.BytesIO(data)
        module = types.SimpleNamespace(ZstdDecompressor=lambda: decoder)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(c.shutil, 'which', return_value=None), \
                mock.patch.dict('sys.modules', {'zstandard': module}):
            file = Path(tmp, 'fixture.jsonl.zst')
            file.write_bytes(b'fixture')
            with c._open_rollout(file) as source:
                self.assertEqual(source.read(), data.decode())
            with c._open_rollout(file, binary=True) as source:
                self.assertEqual(source.read(), data)
        self.assertEqual(decoder.stream_reader.call_count, 2)


if __name__ == '__main__':
    unittest.main()
