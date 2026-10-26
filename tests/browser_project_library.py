"""Project-library UI checks against synthetic metadata and a temporary group store."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import tempfile
from urllib.parse import urlsplit, parse_qs

from browser_question_flow import PageHandler, main as run_browser
from project_library import ProjectGroups


class LibraryPage(PageHandler):
    store = None
    rows = []

    @classmethod
    def reset(cls):
        cls.rows = [{'cc': f'thread-{i:06}', 'cwd': '/demo/Folder000' if i < 135 else f'/demo/Folder{1 + (i - 135) // 25:03}',
                     'title': f'Calculation {i}', 'mtime': 3000 - i, 'available': True} for i in range(2200)]
        cls.rows[-1].update(cwd='/missing/old-work', title='Oldest archived calculation', available=False)
        cls.store.path.unlink(missing_ok=True)

    @classmethod
    def state(cls):
        folders = [{'path': path, 'name': Path(path).name, 'sub': path} for path in sorted({r['cwd'] for r in cls.rows})]
        return {'sessions': cls.rows, 'folders': folders, 'groups': cls.store.read()}

    def respond(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/api/project-library':
            return self.respond(self.state())
        if url.path == '/api/thread':
            cc = parse_qs(url.query).get('cc', [''])[0]
            return self.respond({'cc': cc, 'cwd': '/missing/old-work', 'title': 'Old calculation', 'total': 1,
                                 'atStart': True, 'atEnd': True, 'messages': [{'mid': 1, 'role': 'user', 'txt': 'Retained transcript', 'ts': ''}]})
        return super().do_GET()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        if self.path == '/fixture/reset':
            self.reset(); return self.respond({'ok': True})
        if self.path == '/fixture/add':
            self.rows.append({'cc': 'thread-future', 'cwd': '/demo/Folder000', 'title': 'Future calculation', 'mtime': 4000, 'available': True})
            return self.respond({'ok': True})
        if self.path == '/api/project-library':
            try:
                result = self.store.update(body, self.rows, {r['cwd'] for r in self.rows})
                return self.respond({**self.state(), **result})
            except ValueError as exc:
                return self.respond({'error': str(exc)}, 400)
        return self.respond({'error': 'Unknown fixture request'}, 404)


async def wait(cdp, expression):
    for _ in range(100):
        if await cdp.js(expression): return
        await asyncio.sleep(.05)
    raise AssertionError(expression)


async def verify(cdp, url, output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', source='''
      window.__sent=[];window.__errors=[];window.confirm=()=>true;
      addEventListener('error',e=>__errors.push(e.message));
      window.WebSocket=class {constructor(){this.readyState=1;}send(s){__sent.push(JSON.parse(s));}};
    ''')
    for width in (390, 1280):
        await cdp.call('Emulation.setDeviceMetricsOverride', width=width, height=900, deviceScaleFactor=1, mobile=width == 390)
        await cdp.call('Page.navigate', url=url)
        await wait(cdp, "typeof loadProjectLibrary==='function'&&document.readyState==='complete'")
        await cdp.js("fetch('/fixture/reset',{method:'POST',body:'{}'})")
        await cdp.js("localStorage.clear();HOMEDIR='/demo';libraryOpen=new Set(['all']);pExp.clear();libraryLimits.clear();loadProjectLibrary(true)")
        await wait(cdp, 'libraryState?.sessions.length===2200&&!libraryLoading')
        await cdp.js('''
          sid='current';cwd='/demo/Folder000';ready=true;ta.disabled=false;ta.value='Keep my draft';
          renderLive([{id:'current',cc:'thread-000000',cwd,root:cwd,title:'Active calculation'}]);
          document.documentElement.setAttribute('data-theme','light');
          if(innerWidth<860&&!$('#sidebar').classList.contains('open'))$('#navtoggle').click();
        ''')
        assert await cdp.js("document.querySelectorAll('[data-group=all] .pgroup').length===80&&document.querySelectorAll('[data-group=all] .srow').length===0")
        await cdp.js("document.querySelector('[data-group=all]>.library-folders>.library-more').click()")
        assert await cdp.js("document.querySelectorAll('[data-group=all] .pgroup').length>80")
        await cdp.js("document.querySelector('[data-group=all] [data-path=\"/demo/Folder000\"] .prow').click()")
        assert await cdp.js("document.querySelectorAll('[data-group=all] [data-path=\"/demo/Folder000\"] .srow').length===60")
        await cdp.js("document.querySelector('[data-group=all] [data-path=\"/demo/Folder000\"] .library-more').click()")
        assert await cdp.js("document.querySelectorAll('[data-group=all] [data-path=\"/demo/Folder000\"] .srow').length===120")
        await cdp.js("$('#projectFilter').value='Oldest archived';renderProjectLibrary()")
        assert await cdp.js("document.querySelector('[data-group=all] .pgroup').dataset.path==='/missing/old-work'")
        await cdp.js("document.querySelector('[data-group=all] .prow').click();document.querySelector('[data-group=all] .smeta').click()")
        await wait(cdp, "$('#thbody').textContent.includes('Retained transcript')")
        assert await cdp.js("$('#thopen').disabled&&!__sent.some(x=>x.type==='resume')")
        await cdp.js("closeSearch();$('#projectFilter').value='';renderProjectLibrary();$('#projectNew').click();$('#projectName').value='Project One'")
        assert await cdp.js("$('#projectDialog').open")
        await cdp.call('Input.dispatchKeyEvent', type='keyDown', key='Escape', code='Escape', windowsVirtualKeyCode=27)
        await cdp.call('Input.dispatchKeyEvent', type='keyUp', key='Escape', code='Escape', windowsVirtualKeyCode=27)
        assert not await cdp.js("$('#projectDialog').open")
        await cdp.js("$('#projectNew').click();$('#projectName').value='Project One';$('#projectForm').requestSubmit();$('#projectForm').requestSubmit()")
        await wait(cdp, "libraryState.groups.length===1&&!libraryBusy&&!$('#projectDialog').open")
        if width == 1280:
            await cdp.js("libraryOpen.delete('all');renderProjectLibrary();$('#sidebar').scrollTop=0")
            await asyncio.sleep(.3)
            await cdp.js("window.__dragEvents=[];for(const type of ['mousedown','mouseup','dragstart','dragover','drop','dragend'])document.addEventListener(type,e=>{if(__dragEvents.length<30)__dragEvents.push([type,e.target.className,e.clientX,e.clientY,e.buttons]);},true)")
            points = await cdp.js("(()=>{const a=document.querySelector('#liveList .prow').getBoundingClientRect(),b=document.querySelector('.library-group:not([data-group=all]) .library-heading').getBoundingClientRect();return {ax:a.x+a.width/2,ay:a.y+a.height/2,bx:b.x+b.width/2,by:b.y+b.height/2};})()")
            await cdp.call('Input.dispatchMouseEvent', type='mouseMoved', x=points['ax'], y=points['ay'])
            await cdp.call('Input.dispatchMouseEvent', type='mousePressed', x=points['ax'], y=points['ay'], button='left', buttons=1, clickCount=1)
            for step in range(1, 16):
                await cdp.call('Input.dispatchMouseEvent', type='mouseMoved', button='left', buttons=1,
                               x=points['ax']+(points['bx']-points['ax'])*step/15,
                               y=points['ay']+(points['by']-points['ay'])*step/15)
                await asyncio.sleep(.03)
            # Allow Chromium's native dragover delivery to settle before releasing.
            for step in range(6):
                await cdp.call('Input.dispatchMouseEvent', type='mouseMoved', button='left', buttons=1,
                               x=points['bx'] + step % 2, y=points['by'])
                await asyncio.sleep(.1)
            await cdp.call('Input.dispatchMouseEvent', type='mouseReleased', x=points['bx'], y=points['by'], button='left', clickCount=1)
            try:
                await wait(cdp, "!libraryBusy&&libraryState.groups[0].folders.includes('/demo/Folder000')")
            except AssertionError:
                print('Drag diagnostics:', points, await cdp.js("({events:__dragEvents,drag:libraryDrag,groups:libraryState.groups,feedback:$('#projectFeedback').textContent,errors:__errors})"))
                shot = await cdp.call('Page.captureScreenshot', format='png')
                (output / 'drag-failure.png').write_bytes(base64.b64decode(shot['data']))
                raise
        await cdp.js('''
          window.__group=libraryState.groups[0].id;
          window.__dragSource=document.querySelector('#liveList .prow');
          window.__transfer=new DataTransfer();
          __dragSource.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:__transfer}));
          renderSidebar();
        ''')
        assert await cdp.js('__dragSource.isConnected&&libraryDeferred')
        await cdp.js('''
          const target=document.querySelector('[data-group="'+__group+'"]');
          target.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:__transfer}));
        ''')
        assert await cdp.js("document.querySelector('[data-group=\"'+__group+'\"]').classList.contains('drop-target')")
        await cdp.js("document.querySelector('[data-group=\"'+__group+'\"]').dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:__transfer}));__dragSource.dispatchEvent(new DragEvent('dragend'))")
        await wait(cdp, "!libraryBusy&&libraryState.groups[0].folders.includes('/demo/Folder000')")
        assert await cdp.js("document.querySelector('[data-group=\"'+__group+'\"] .library-count').textContent==='135'")
        await cdp.js("fetch('/fixture/add',{method:'POST',body:'{}'}).then(()=>loadProjectLibrary(true))")
        await wait(cdp, "libraryState.sessions.length===2201&&!libraryLoading")
        assert await cdp.js("document.querySelector('[data-group=\"'+__group+'\"] .library-count').textContent==='136'")
        await cdp.js("mutateProject({op:'remove',id:__group,kind:'session',value:'thread-000000'})")
        assert await cdp.js("document.querySelector('[data-group=\"'+__group+'\"] .library-count').textContent==='135'")
        await cdp.js("chooseLibraryProject({kind:'session',value:'thread-002199'})")
        await wait(cdp, "$('#projectDialog').open")
        await cdp.js("$('#projectForm').requestSubmit()")
        await wait(cdp, "!libraryBusy&&!$('#projectDialog').open&&libraryState.groups[0].sessions.includes('thread-002199')")
        assert await cdp.js("document.querySelectorAll('[data-group=\"'+__group+'\"] .pgroup').length===2")
        await cdp.js("editLibraryProject(libraryState.groups[0]);$('#projectName').value='Project <img src=x onerror=window.__injected=true>';$('#projectForm').requestSubmit()")
        await wait(cdp, "!libraryBusy&&!$('#projectDialog').open")
        assert await cdp.js("!window.__injected&&!$('#projectList').querySelector('img')")
        await cdp.js("mutateProject({op:'rename',id:__group,name:'Project One'})")
        await cdp.js("libraryOpen.delete('all');pExp.add('library:'+__group+':/demo/Folder000');renderProjectLibrary();$('#sidebar').scrollTop=150")
        await asyncio.sleep(.2)
        assert await cdp.js("$('#sidebar').scrollWidth<=$('#sidebar').clientWidth")
        shot = await cdp.call('Page.captureScreenshot', format='png')
        (output / f'project-library-{width}.png').write_bytes(base64.b64decode(shot['data']))
        await cdp.js("document.documentElement.setAttribute('data-theme','dark')")
        shot = await cdp.call('Page.captureScreenshot', format='png')
        (output / f'project-library-dark-{width}.png').write_bytes(base64.b64decode(shot['data']))
        assert await cdp.js("ta.value==='Keep my draft'&&!__sent.some(x=>['start','resume','interrupt','end','user'].includes(x.type))")
        await cdp.js("loadProjectLibrary(true)")
        await wait(cdp, '!libraryLoading')
        assert await cdp.js("libraryState.groups[0].name==='Project One'&&libraryState.groups[0].excluded.includes('thread-000000')")
        await cdp.js("mutateProject({op:'delete',id:__group})")
        assert await cdp.js('libraryState.groups.length===0&&libraryState.sessions.length===2201')
        assert await cdp.js('__errors') == []
        print(f'{width}px: 2200 sessions, lazy rows, old/missing folder, drag, menu add, exclusions, rename, persistence and safe delete PASS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path(tempfile.gettempdir()) / 'project-library-check')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='project-library-fixture-') as directory:
        LibraryPage.store = ProjectGroups(Path(directory) / 'groups.json')
        LibraryPage.reset()
        asyncio.run(run_browser(args.output_dir, verify_page=verify, page_handler=LibraryPage))
