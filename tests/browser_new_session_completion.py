"""New-session directory completion with synthetic folders and no model calls."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import tempfile

from browser_question_flow import main as run_browser


async def wait(cdp, expression):
    for _ in range(160):
        if await cdp.js(expression): return
        await asyncio.sleep(.025)
    state=await cdp.js("({value:$('#newCwd').value,query:newCwdQuery,index:newCwdIndex,items:newCwdItems,focus:document.activeElement.id,hidden:$('#newCwdSuggestions').hidden,text:$('#newCwdSuggestions').textContent,lookups:__lookups,errors:__errors})")
    raise AssertionError((expression,state))


async def path_input(cdp, value):
    await cdp.js("(()=>{const input=$('#newCwd');input.focus();input.value="+json.dumps(value)+
                 ";input.setSelectionRange(input.value.length,input.value.length);input.dispatchEvent(new Event('input',{bubbles:true}));})()")


async def key(cdp, value, shift=False):
    codes={'ArrowDown':40,'ArrowUp':38,'Tab':9,'Enter':13,'Escape':27}
    for kind in ('keyDown','keyUp'):
        await cdp.call('Input.dispatchKeyEvent',type=kind,key=value,code=value,
                       windowsVirtualKeyCode=codes[value],modifiers=8 if shift else 0)


async def choose(cdp, mobile):
    point=await cdp.js("(()=>{const r=$('#newCwdSuggestions [role=option]').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
    if mobile:
        await cdp.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[point])
        await cdp.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
    else:
        await cdp.call('Input.dispatchMouseEvent',type='mousePressed',button='left',clickCount=1,**point)
        await cdp.call('Input.dispatchMouseEvent',type='mouseReleased',button='left',clickCount=1,**point)


async def verify(cdp, url, output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument',source=r'''
      localStorage.clear();window.__sent=[];window.__errors=[];window.__lookups=[];window.__held=[];window.__hold=false;
      addEventListener('error',e=>__errors.push(e.message));
      window.WebSocket=class {constructor(){this.readyState=1;}send(raw){__sent.push(JSON.parse(raw));}};
      const realFetch=window.fetch;
      window.fetch=(url,options)=>{
        const u=new URL(url,location.href);
        if(u.pathname!=='/api/directories')return realFetch(url,options);
        const request={path:u.searchParams.get('path'),prefix:u.searchParams.get('prefix'),hidden:u.searchParams.get('hidden')==='1'};
        __lookups.push(request);
        const finish=()=>{
          if(request.path==='/denied/')return new Response(JSON.stringify({error:'Cannot read this directory'}),{status:400});
          let parent=request.path;
          if(parent==='~'||parent.startsWith('~/'))parent='/demo'+parent.slice(1);
          while(parent.length>1&&parent.endsWith('/'))parent=parent.slice(0,-1);
          const names=parent==='/demo'?['Notebook archive','Notebooks','Project Alpha','.hidden','实验 files','<img src=x onerror=window.__executed=true>']:
            parent==='/demo/Notebook archive'?['analysis','assets']:
            parent==='/demo/Notebooks'?['Drafts','Figures']:
            parent==='/demo/many'?Array.from({length:60},(_,i)=>'experiment-'+String(i).padStart(2,'0')):[];
          const folders=names.filter(n=>(request.hidden||!n.startsWith('.'))&&n.toLowerCase().startsWith((request.prefix||'').toLowerCase()))
            .map(name=>({name,path:parent+'/'+name}));
          return new Response(JSON.stringify({path:parent,home:'/demo',parent:'/demo',folders,truncated:false}),{status:200});
        };
        if(__hold){__hold=false;return new Promise(resolve=>__held.push({release:()=>resolve(finish()),signal:options?.signal}));}
        return Promise.resolve(finish());
      };
    ''')
    for width in (320,390,1280):
        await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=844,deviceScaleFactor=1,mobile=width<=390)
        await cdp.call('Emulation.setTouchEmulationEnabled',enabled=width<=390,maxTouchPoints=1)
        await cdp.call('Page.navigate',url=url)
        await wait(cdp,"document.readyState==='complete'&&typeof queryNewCwdSuggestions==='function'")
        await cdp.js("HOMEDIR='/demo';sid='existing';cwd='/demo/current';ready=true;running=true;newSession();$('#newName').value='Benchmark setup'")
        await path_input(cdp,'/demo/No')
        await wait(cdp,'newCwdItems.length===2')
        assert await cdp.js("$('#newCwdSuggestions').textContent==='Notebook archive/Notebooks/'&&$('#newCwd').getAttribute('aria-expanded')==='true'")
        await key(cdp,'ArrowDown');await key(cdp,'ArrowDown');await key(cdp,'ArrowUp')
        await wait(cdp,"newCwdIndex===0&&$('#newCwd').value==='/demo/No'&&document.getElementById($('#newCwd').getAttribute('aria-activedescendant')).getAttribute('aria-selected')==='true'")
        await key(cdp,'Tab')
        await wait(cdp,"$('#newCwd').value==='/demo/Notebook archive/'&&newCwdItems.length===2")
        assert await cdp.js("document.activeElement===$('#newCwd')&&newCwdItems[0].name==='analysis'")
        await key(cdp,'ArrowUp');await key(cdp,'Tab')
        await wait(cdp,"$('#newCwd').value==='/demo/Notebook archive/assets/'&&!newCwdAbort")
        await key(cdp,'Tab')
        assert await cdp.js("document.activeElement.id==='newBrowse'&&$('#newCwdSuggestions').hidden"), 'Tab must retain normal focus navigation when there is no match'

        await path_input(cdp,'/demo/No');await wait(cdp,'newCwdItems.length===2')
        await key(cdp,'ArrowDown');await key(cdp,'ArrowDown');await key(cdp,'Enter')
        await wait(cdp,"$('#newCwd').value==='/demo/Notebooks/'")
        assert not await cdp.js("__sent.some(m=>m.type==='start')")
        await path_input(cdp,'/demo/Pr');await key(cdp,'Tab')
        await wait(cdp,"$('#newCwd').value==='/demo/Project Alpha/'&&!newCwdAbort")
        for typed, expected in (('~/No','/demo/Notebook archive/'),('No','/demo/Notebook archive/'),
                                ('/demo/.','/demo/.hidden/'),('/demo/\u5b9e','/demo/\u5b9e\u9a8c files/')):
            await path_input(cdp,typed);await wait(cdp,'newCwdItems.length>0')
            await key(cdp,'Tab')
            await wait(cdp,"$('#newCwd').value==="+json.dumps(expected))
            assert await cdp.js("$('#newCwd').value.endsWith('/')&&!$('#newCwd').value.endsWith('//')")

        await path_input(cdp,'/demo/<');await wait(cdp,'newCwdItems.length===1')
        assert await cdp.js("!$('#newCwdSuggestions img')&&!window.__executed&&$('#newCwdSuggestions').textContent.includes('<img')")
        await path_input(cdp,'/demo/No');await wait(cdp,'newCwdItems.length===2')
        await choose(cdp,width<=390)
        await wait(cdp,"$('#newCwd').value==='/demo/Notebook archive/'")
        assert await cdp.js("document.activeElement===$('#newCwd')")

        await cdp.js('__hold=true')
        await path_input(cdp,'/demo/No');await wait(cdp,'__held.length===1')
        await path_input(cdp,'/demo/Pr');await wait(cdp,"newCwdItems.length===1&&newCwdItems[0].name==='Project Alpha'")
        await cdp.js('__held.shift().release()');await asyncio.sleep(.05)
        assert await cdp.js("$('#newCwd').value==='/demo/Pr'&&newCwdItems.length===1&&newCwdItems[0].name==='Project Alpha'")
        await path_input(cdp,'/denied/test');await wait(cdp,"$('#newCwdSuggestions').textContent==='Cannot read this directory'")
        assert await cdp.js('newCwdItems.length===0')

        await path_input(cdp,'/demo/many/');await wait(cdp,'newCwdItems.length===50')
        await key(cdp,'ArrowUp')
        assert await cdp.js("newCwdIndex===49&&$('#newCwdSuggestions').scrollTop>0&&$('#newCwdSuggestions').textContent.includes('More matches')")
        for theme in ('light','dark'):
            await path_input(cdp,'/demo/No');await wait(cdp,'newCwdItems.length===2')
            await key(cdp,'ArrowDown')
            await cdp.js("document.documentElement.setAttribute('data-theme',"+json.dumps(theme)+")")
            assert await cdp.js("(()=>{const d=$('#newSessionDialog'),r=$('#newCwdSuggestions').getBoundingClientRect(),b=d.getBoundingClientRect();return r.x>=b.x&&r.right<=b.right&&r.top>=b.top&&r.bottom<=b.bottom&&d.scrollWidth<=d.clientWidth;})()")
            shot=await cdp.call('Page.captureScreenshot',format='png')
            (output/f'folder-completion-{theme}-{width}.png').write_bytes(base64.b64decode(shot['data']))

        if width<=390:
            await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=420,deviceScaleFactor=1,mobile=True)
            await cdp.js("$('#newCwd').scrollIntoView({block:'center'});queryNewCwdSuggestions()")
            await wait(cdp,'newCwdItems.length===2')
            assert await cdp.js("(()=>{const r=$('#newCwdSuggestions').getBoundingClientRect(),d=$('#newSessionDialog').getBoundingClientRect();return r.x>=0&&r.right<=innerWidth&&r.top>=d.top&&r.bottom<=Math.min(innerHeight,d.bottom);})()"), 'A short viewport must keep suggestions within the dialog'
            shot=await cdp.call('Page.captureScreenshot',format='png')
            (output/f'folder-completion-short-{width}.png').write_bytes(base64.b64decode(shot['data']))
            await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=844,deviceScaleFactor=1,mobile=True)

        await key(cdp,'Escape')
        assert await cdp.js("$('#newSessionDialog').open&&$('#newCwdSuggestions').hidden")
        await key(cdp,'Escape')
        assert await cdp.js("!$('#newSessionDialog').open&&!__sent.some(m=>m.type==='interrupt')")
        await cdp.js("newSession();__hold=true")
        await path_input(cdp,'/demo/No');await wait(cdp,'__held.length===1')
        await cdp.js("$('#newSessionCancel').click();__held.shift().release()")
        await asyncio.sleep(.05)
        assert await cdp.js("!$('#newSessionDialog').open&&$('#newCwdSuggestions').hidden")

        await cdp.js("newSession();$('#newCwd').focus();$('#newCwd').dispatchEvent(new CompositionEvent('compositionstart'));window.__before=__lookups.length")
        await path_input(cdp,'/demo/\u5b9e')
        await asyncio.sleep(.2)
        assert await cdp.js('__lookups.length===__before')
        assert await cdp.js("!$('#newCwd').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,cancelable:true,bubbles:true}))")
        await cdp.js("$('#newCwd').dispatchEvent(new CompositionEvent('compositionend'))")
        await wait(cdp,'newCwdItems.length===1')
        await key(cdp,'Tab')
        assert await cdp.js("$('#newCwd').value.endsWith('/')")
        await cdp.js("$('#newName').focus()")
        assert await cdp.js("$('#newCwdSuggestions').hidden")
        assert not await cdp.js("__sent.some(m=>['start','resume','interrupt','end'].includes(m.type))")
        assert await cdp.js('__errors')==[]
        print(f'{width}px: prefix candidates, arrows/Tab/Enter, trailing slash, nested paths, touch, IME, stale replies and dialog lifecycle PASS')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path(tempfile.gettempdir())/'new-session-completion')
    asyncio.run(run_browser(parser.parse_args().output_dir,verify_page=verify))
