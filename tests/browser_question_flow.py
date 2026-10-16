"""Optional headless Chrome regression probe for question cards.

Run from the repository root with Python, Tornado, and google-chrome installed.
Uses an isolated synthetic page and browser profile, never a live session.
"""
import argparse
import asyncio
import base64
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request

from tornado.websocket import websocket_connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_console as console


class PageHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith('/static/'):
            return super().do_GET()
        if self.path.startswith('/api/'):
            content = b'{"projects":[],"models":[],"available":false}'
            mime = 'application/json'
        else:
            content = console.CONSOLE_HTML.replace('__CODEX_CONSOLE_WEBFM_URL__', 'null').replace(
                '__CODEX_CONSOLE_STAMP__', console.PAGE_STAMP).encode()
            mime = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.end_headers()
        self.wfile.write(content)


class CDP:
    def __init__(self, ws):
        self.ws, self.seq, self.pending = ws, 0, {}
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        while (message := await self.ws.read_message()) is not None:
            data = json.loads(message)
            future = self.pending.pop(data.get('id'), None)
            if future and not future.done():
                future.set_result(data)

    async def call(self, method, **params):
        self.seq += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = future
        await self.ws.write_message(json.dumps({'id': self.seq, 'method': method, 'params': params}))
        response = await asyncio.wait_for(future, 15)
        if 'error' in response:
            raise RuntimeError(response['error'])
        return response.get('result', {})

    async def js(self, source):
        result = await self.call('Runtime.evaluate', expression=source, returnByValue=True, awaitPromise=True)
        if 'exceptionDetails' in result:
            raise RuntimeError(result['exceptionDetails'])
        return result['result'].get('value')

    async def enter(self, shift=False):
        await self.call('Input.dispatchKeyEvent', type='keyDown', key='Enter', code='Enter',
                        windowsVirtualKeyCode=13, text='\r', modifiers=8 if shift else 0)
        await self.call('Input.dispatchKeyEvent', type='keyUp', key='Enter', code='Enter',
                        windowsVirtualKeyCode=13, modifiers=8 if shift else 0)


async def verify(cdp, url, output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', source='''
      window.__sent=[];
      window.WebSocket=class {constructor(){this.readyState=1;}send(s){__sent.push(JSON.parse(s));}};
    ''')
    for width in (390, 1280):
        await cdp.call('Emulation.setDeviceMetricsOverride', width=width, height=844,
                       deviceScaleFactor=1, mobile=width == 390)
        await cdp.call('Page.navigate', url=url)
        for _ in range(100):
            if await cdp.js("document.readyState==='complete' && typeof addQuestion==='function'"):
                break
            await asyncio.sleep(.05)
        else:
            raise RuntimeError(await cdp.js("JSON.stringify({url:location.href,state:document.readyState,question:typeof addQuestion,body:document.body&&document.body.innerText.slice(0,300)})"))
        await cdp.js('''
          ready=true;replaying=false;
          window.card=id=>stream.querySelector('.question[data-aid="'+id+'"]');
          addQuestion({aid:'q1',questions:[
            {question:'Choose scope',options:[{label:'Local'},{label:'All'}],isOther:false},
            {question:'Custom scope',options:[{label:'Default'}],isOther:true},
            {question:'Describe the change',options:null,isOther:false}]});
        ''')
        assert await cdp.js("card('q1').querySelectorAll('textarea.qother').length") == 2
        assert await cdp.js("card('q1').querySelector('.qsub').disabled")
        assert not await cdp.js("card('q1').querySelector('.qdismiss').disabled")
        result = await cdp.js('''(()=>{
          const c=card('q1'),blocks=c.querySelectorAll('.qblk');
          blocks[0].querySelector('.qopt').click();
          blocks[1].querySelector('.qopt').click();
          const o=blocks[1].querySelector('.qother');o.value='Custom';o.dispatchEvent(new Event('input'));
          const customClears=!blocks[1].querySelector('.qopt.sel');
          blocks[1].querySelector('.qopt').click();
          return {customClears,optionClears:o.value==='',value:qval(blocks[1])};
        })()''')
        assert result == {'customClears': True, 'optionClears': True, 'value': 'Default'}
        await cdp.js("const o=card('q1').querySelectorAll('.qother')[1];o.value='First line';o.dispatchEvent(new Event('input'));o.focus();o.setSelectionRange(o.value.length,o.value.length);")
        await cdp.enter(shift=True)
        assert '\n' in await cdp.js("card('q1').querySelectorAll('.qother')[1].value")
        assert await cdp.js('__sent.length') == 0
        await cdp.js("ws.readyState=3")
        await cdp.enter()
        assert 'Not connected' in await cdp.js("card('q1').querySelector('.qfeedback').textContent")
        assert not await cdp.js("card('q1').querySelector('.qsub').disabled")
        await cdp.js('ws.readyState=1')
        await cdp.enter()
        await cdp.js("card('q1').querySelector('.qsub').click();card('q1').querySelector('.qdismiss').click();")
        assert await cdp.js('__sent.length') == 1
        assert await cdp.js("card('q1').querySelector('.qsub').disabled && card('q1').querySelector('.qdismiss').disabled")
        payload = await cdp.js('__sent[0]')
        assert payload == {'type': 'answer', 'aid': 'q1', 'answers': ['Local', 'Default', 'First line']}
        await cdp.js("route({kind:'question_resolved',aid:'q1',answers:['Local','Default','First line']});")
        assert await cdp.js("card('q1').querySelector('textarea').disabled")
        await cdp.js("addQuestion({aid:'q2',questions:[{question:'Skip this question?',options:[{label:'Yes'}]}]});card('q2').querySelector('.qdismiss').click();")
        assert await cdp.js('__sent[1]') == {'type': 'answer', 'aid': 'q2', 'answers': []}
        await cdp.js("route({kind:'question_resolved',aid:'q2',answers:null})")
        assert 'dismissed' in await cdp.js("card('q2').querySelector('.qdone').textContent")
        await cdp.js(r'''
          stream.innerHTML='';addQuestion({aid:'long',questions:[{question:'Describe the calculation',options:[]}]});
          const long=card('long').querySelector('.qother');long.value=('Long calculation description with multiple words. '.repeat(8)+'\n').repeat(5);
          long.dispatchEvent(new Event('input'));scroll();
        ''')
        bounds = await cdp.js("(()=>{const o=card('long').querySelector('.qother');return {height:o.getBoundingClientRect().height,scroll:o.scrollHeight>o.clientHeight,overflow:document.documentElement.scrollWidth>innerWidth};})()")
        assert 40 < bounds['height'] <= 124 and bounds['scroll'] and not bounds['overflow'], bounds
        shot = await cdp.call('Page.captureScreenshot', format='png')
        (output / f'question-flow-{width}.png').write_bytes(base64.b64decode(shot['data']))
        print(f'{width}px: isOther, mutually exclusive answers, Enter/Shift+Enter, disconnect, single submit, dismiss, wrapping PASS')


async def main(output, verify_page=verify):
    chrome_bin = shutil.which('google-chrome')
    if not chrome_bin:
        raise RuntimeError('google-chrome is required')
    output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), PageHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix='question-browser-') as profile:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        chrome = subprocess.Popen([chrome_bin, '--headless=new', '--no-sandbox', '--disable-gpu',
            f'--remote-debugging-port={port}', f'--user-data-dir={profile}', '--no-first-run', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cdp = None
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=1) as response:
                        target = next(t for t in json.load(response) if t.get('type') == 'page')
                    break
                except (OSError, StopIteration):
                    await asyncio.sleep(.1)
            else:
                raise RuntimeError('Chrome did not start')
            cdp = CDP(await websocket_connect(target['webSocketDebuggerUrl'], max_message_size=32 << 20))
            await verify_page(cdp, f'http://127.0.0.1:{server.server_port}/', output)
        finally:
            if cdp:
                cdp.ws.close();cdp.reader.cancel()
            chrome.terminate();chrome.wait(timeout=10)
            server.shutdown();server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path(tempfile.gettempdir()) / 'question-flow-check')
    asyncio.run(main(parser.parse_args().output_dir))
