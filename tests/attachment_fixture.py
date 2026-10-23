"""Isolated real-HTTP fixture for attachment UI tests; never starts Codex."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

import tornado.httpserver
import tornado.netutil
import tornado.web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_console as c


class Page(tornado.web.RequestHandler):
    def get(self):
        self.set_header('Content-Type', 'text/html; charset=utf-8')
        self.write(c.CONSOLE_HTML.replace('__CODEX_CONSOLE_WEBFM_URL__', 'null').replace(
            '__CODEX_CONSOLE_STAMP__', 'attachment-fixture'))


class Upload(c.AuthMixin, tornado.web.RequestHandler):
    def post(self):
        if not self._ok_auth(): return
        body = json.loads(self.request.body)
        session = c.ChatSession('fixture', c.HOME, 'default', 'full-access')
        session.proc = object(); session.busy = True; session._emit = lambda _: None
        session.send_user(body.get('text', ''), body.get('images'), body.get('files'))
        if not session.queue:
            self.set_status(400); self.finish({'events': session.log}); return
        item = session.queue[0]
        inputs = session._make_input(item['text'], item['images'], item['files'])
        session._echo_user(item['text'], item['images'], item['files'], qid=item['qid'])
        core = []
        for block in inputs:
            if block['type'] == 'text': core.append({'type':'input_text', 'text':block['text']})
            else:
                core.extend([{'type':'input_text','text':'<image name=[Image #1] path="'+block['path']+'">'},
                             {'type':'input_image','image_url':'data:image/png;base64,not-replayed'}])
        history = c.parse_codex({'type':'response_item','payload':{'type':'message','role':'user','content':core}}, 1)
        self.write({'events':session.log, 'history':history, 'home':c.HOME})


class Empty(tornado.web.RequestHandler):
    def get(self): self.write({'projects':[], 'models':[], 'available':False})


async def main():
    with tempfile.TemporaryDirectory(prefix='codex-attachments-test-') as temp:
        c.HOME = str(Path(temp)/'home'); Path(c.HOME).mkdir()
        (Path(c.HOME)/'Saved files').mkdir()
        c.ATTACHMENTS = c.AttachmentStore(str(Path(temp)/'uploads'))
        c.AUTH = 'fixture:test'
        app = tornado.web.Application([
            (r'/', Page), (r'/fixture/upload', Upload),
            (r'/api/attachments/([0-9a-f]{32})', c.AttachmentHandler),
            (r'/api/attachments/([0-9a-f]{32})/save', c.AttachmentSaveHandler),
            (r'/api/directories', c.DirectoriesHandler), (r'/api/.*', Empty),
            (r'/static/(.*)', tornado.web.StaticFileHandler, {'path':str(ROOT/'static')})])
        sockets = tornado.netutil.bind_sockets(0, address='127.0.0.1')
        server = tornado.httpserver.HTTPServer(app); server.add_sockets(sockets)
        print(json.dumps({'url':'http://127.0.0.1:'+str(sockets[0].getsockname()[1])}), flush=True)
        try:
            await asyncio.to_thread(sys.stdin.read)
        finally:
            server.stop()
            await server.close_all_connections()


if __name__ == '__main__': asyncio.run(main())
