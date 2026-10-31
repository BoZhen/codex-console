"""Page-based rename controls, with native prompts deliberately unavailable."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import tempfile

from browser_question_flow import main as run_browser


async def wait(cdp, expression):
    for _ in range(100):
        if await cdp.js(expression):return
        await asyncio.sleep(.03)
    raise AssertionError(expression)


async def verify(cdp,url,output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument',source='''
      localStorage.clear();window.__sent=[];window.__errors=[];
      window.prompt=()=>{throw Error('prompt() is not supported.');};
      window.alert=()=>{throw Error('Native alert must not be needed.');};
      addEventListener('error',e=>__errors.push(e.message));
      window.WebSocket=class {constructor(){this.readyState=1;}send(raw){__sent.push(JSON.parse(raw));}};
    ''')
    for width in (320,390,1280):
        await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=844,deviceScaleFactor=1,mobile=width<400)
        await cdp.call('Page.navigate',url=url)
        await wait(cdp,"document.readyState==='complete'&&typeof openRename==='function'")
        await cdp.js('''
          sid='live';curCC='fixture-thread-one';cwd='/demo/project';ready=true;running=true;
          window.__session={id:sid,cc:curCC,name:'Session',title:'Original task',cwd};
          ensureSessionTab(__session,true);setCurname(__session.title);
          window.__row=liveSessRow(__session);$('#liveList').replaceChildren(__row);
          __row.querySelector('.skebab').click();[...$('#cardMenu').children].find(n=>n.textContent==='\u270e Rename').click();
        ''')
        assert await cdp.js("$('#renameDialog').open&&document.activeElement.id==='renameName'&&$('#renameName').value==='Original task'")
        for theme in ('light','dark'):
            await cdp.js("document.documentElement.setAttribute('data-theme',"+json.dumps(theme)+")")
            assert await cdp.js("(()=>{const d=$('#renameDialog'),r=d.getBoundingClientRect();return r.x>=0&&r.right<=innerWidth&&r.y>=0&&r.bottom<=innerHeight&&d.scrollWidth<=d.clientWidth;})()")
            shot=await cdp.call('Page.captureScreenshot',format='png')
            (output/f'rename-{theme}-{width}.png').write_bytes(base64.b64decode(shot['data']))
        await cdp.js("$('#renameName').value='Cancelled';$('#renameCancel').click()")
        await wait(cdp,"!$('#renameDialog').open")
        assert not await cdp.js("__sent.some(m=>m.type==='rename')")
        await cdp.js("renameSession(curCC,'Original task');ws.readyState=3;$('#renameForm').requestSubmit()")
        assert await cdp.js("$('#renameError').textContent.includes('Not connected')&&!$('#renameSave').disabled")
        await cdp.js("ws.readyState=1;window.__name='New \u540d\u79f0 <img src=x onerror=window.__executed=true>';$('#renameName').value=__name;$('#renameForm').requestSubmit();$('#renameForm').requestSubmit()")
        assert await cdp.js("__sent.filter(m=>m.type==='rename').length===1&&$('#renameSave').disabled&&$('#renameName').disabled")
        await cdp.js("onMsg({data:JSON.stringify({type:'renamed',cc:curCC,requestId:'stale',ok:true,name:'Other reply'})})")
        assert await cdp.js("$('#renameDialog').open&&renameState.pending")
        await cdp.js("onMsg({data:JSON.stringify({type:'renamed',cc:curCC,requestId:renameState.requestId,ok:false,error:'Could not save'})})")
        assert await cdp.js("$('#renameDialog').open&&!$('#renameSave').disabled&&$('#renameError').textContent==='Could not save'&&$('#renameName').value===__name")
        await cdp.js("$('#renameName').focus()")
        await cdp.enter()
        await cdp.js("$('#renameForm').requestSubmit()")
        assert await cdp.js("__sent.filter(m=>m.type==='rename').length===2")
        await cdp.js("onMsg({data:JSON.stringify({type:'renamed',cc:curCC,requestId:renameState.requestId,ok:true,name:__name,title:__name})})")
        await wait(cdp,"!$('#renameDialog').open")
        assert await cdp.js("$('#curname').textContent===__name&&sessionTabById(sid).title===__name&&!$('#sessionTabs img')&&!window.__executed")
        await cdp.js("renameSession(curCC,__name);$('#renameReset').click();$('#renameForm').requestSubmit()")
        assert await cdp.js("__sent.filter(m=>m.type==='rename').at(-1).name===''")
        await cdp.js("onMsg({data:JSON.stringify({type:'renamed',cc:curCC,requestId:renameState.requestId,ok:true,name:'',title:'Original task'})})")
        await wait(cdp,"!$('#renameDialog').open")
        assert await cdp.js("$('#curname').textContent==='Original task'&&sessionTabById(sid).title==='Original task'")
        await cdp.js("renameSession('historic-thread','Old name');$('#renameName').dispatchEvent(new CompositionEvent('compositionstart'));window.__before=__sent.length;$('#renameForm').requestSubmit()")
        assert await cdp.js("__sent.length===__before")
        await cdp.js("$('#renameName').dispatchEvent(new CompositionEvent('compositionend'));$('#renameCancel').click()")
        await wait(cdp,"!$('#renameDialog').open")
        await cdp.js("renameSession(null,'Starting session')")
        assert await cdp.js("$('#renameDialog').open&&$('#renameSave').disabled&&$('#renameError').textContent.includes('starting')")
        await cdp.js("$('#renameCancel').click()")
        await wait(cdp,"!$('#renameDialog').open")
        await cdp.js("renameProject({path:'/demo/project',name:'Project',renamed:true});$('#renameName').value='Folder label';$('#renameForm').requestSubmit()")
        assert await cdp.js("__sent.at(-1).type==='proj_rename'&&__sent.at(-1).path==='/demo/project'")
        await cdp.js("onMsg({data:JSON.stringify({type:'project_renamed',path:'/demo/project',requestId:renameState.requestId,ok:true})})")
        await wait(cdp,"!$('#renameDialog').open")
        await cdp.js("renameSession(curCC,'Original task')")
        await cdp.call('Input.dispatchKeyEvent',type='keyDown',key='Escape',code='Escape',windowsVirtualKeyCode=27)
        await cdp.call('Input.dispatchKeyEvent',type='keyUp',key='Escape',code='Escape',windowsVirtualKeyCode=27)
        assert await cdp.js("!$('#renameDialog').open&&!__sent.some(m=>['interrupt','start','resume','end'].includes(m.type))")
        assert await cdp.js('__errors')==[]
        print(f'{width}px: no native prompts, save/reset/cancel, errors, duplicate guard, correlation, safe titles, folder labels and IME PASS')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path(tempfile.gettempdir())/'rename-check')
    asyncio.run(run_browser(parser.parse_args().output_dir,verify_page=verify))
