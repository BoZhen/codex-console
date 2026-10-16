"""Synthetic new-session and directory-browser checks; no live Codex processes."""
import argparse
import asyncio
import base64
from pathlib import Path
import tempfile

from browser_question_flow import main as run_browser


async def settle(cdp):
    await cdp.js('new Promise(r=>setTimeout(r,80))')


async def escape(cdp):
    await cdp.call('Input.dispatchKeyEvent',type='keyDown',key='Escape',code='Escape',windowsVirtualKeyCode=27)
    await cdp.call('Input.dispatchKeyEvent',type='keyUp',key='Escape',code='Escape',windowsVirtualKeyCode=27)
    await settle(cdp)


async def verify(cdp,url,output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument',source='''
      localStorage.clear();window.__sent=[];window.__errors=[];window.__hold=false;window.__imports=[];window.__refreshes=0;
      window.__mkdir=[];window.__created=[];
      addEventListener('error',e=>__errors.push(e.message));
      window.WebSocket=class {constructor(){this.readyState=1;}send(s){__sent.push(JSON.parse(s));}};
      const realFetch=window.fetch;
      window.fetch=(url,options)=>{
        const u=new URL(url,location.href);
        if(u.pathname==='/api/models'&&window.__catalog){
          if(u.searchParams.has('fresh'))__refreshes++;
          return Promise.resolve(new Response(JSON.stringify(window.__catalogFail?{error:'Unavailable'}:
            {models:__catalog,defaultModel:'test-model'}),{status:window.__catalogFail?503:200}));}
        if(u.pathname==='/api/import'){
          __imports.push({cwd:options.body.get('cwd'),name:options.body.get('file').name});
          return Promise.resolve(new Response(JSON.stringify({ok:true}),{status:200}));}
        if(u.pathname!='/api/directories')return realFetch(url,options);
        if(options?.method==='POST'){
          const request=JSON.parse(options.body);__mkdir.push(request);
          const finish=()=>{
            if(request.name==='Existing')return new Response(JSON.stringify({error:'A file or folder with that name already exists'}),{status:409});
            if(request.name==='Denied')return new Response(JSON.stringify({error:'No permission to create a folder here'}),{status:403});
            if(request.name.includes('/'))return new Response(JSON.stringify({error:'Enter a single folder name'}),{status:400});
            const path=request.path+'/'+request.name;__created.push(path);return new Response(JSON.stringify({path}),{status:201});};
          if(window.__holdCreate){window.__holdCreate=false;return new Promise(resolve=>window.__releaseCreate=()=>resolve(finish()));}
          return Promise.resolve(finish());
        }
        const path=u.searchParams.get('path'),hidden=u.searchParams.get('hidden')==='1';
        const reply=()=>{let data;
          if(path==='/missing')return new Response(JSON.stringify({error:'Choose an existing directory'}),{status:400});
          if(path==='/denied')return new Response(JSON.stringify({error:'Cannot read this directory'}),{status:400});
          const p=path==='~'?'/demo':path;
          const folders=p==='/demo'?[{name:'Project Alpha',path:'/demo/Project Alpha'},
            {name:'Long project name with several words for responsive layout checks',path:'/demo/Long project'},
            ...(hidden?[{name:'.hidden',path:'/demo/.hidden'}]:[])]:[];
          const parent=p==='/demo'?p:(p.slice(0,p.lastIndexOf('/'))||'/demo');
          data={path:p,parent,home:'/demo',folders,truncated:false};
          return new Response(JSON.stringify(data),{status:200});};
        if(window.__hold){window.__hold=false;return new Promise(resolve=>{window.__release=()=>resolve(reply());});}
        return Promise.resolve(reply());
      };
    ''')
    for width in (390,1280):
        await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=844,deviceScaleFactor=1,mobile=width==390)
        await cdp.call('Page.navigate',url=url)
        for _ in range(100):
            if await cdp.js("document.readyState==='complete' && typeof submitNewSession==='function'"):
                break
            await asyncio.sleep(.05)
        else:
            raise RuntimeError('Fixture did not initialize')
        assert await cdp.js("!$('#project')&&!$('#cwd')&&!$('#cwdwrap')&&!$('#model')&&!$('#mode')")
        assert await cdp.js("$('#newSessionDialog').contains($('#mrefresh'))&&!$('#sidebar').querySelector('select:not(#theme)')")
        await cdp.js('''
          HOMEDIR='/demo';sid='existing';cwd='/demo/current';ready=true;running=true;curEffort='xhigh';
          MODELS=[{id:'test-model',name:'Test model',reasoningEfforts:['high','xhigh'],defaultReasoningEffort:'high'},
            {id:'small-model',name:'Small model',reasoningEfforts:['low','medium'],defaultReasoningEffort:'medium'}];
          DEFAULT_MODEL_ID='test-model';rebuildModelOptions();
          preferredModel='test-model';localStorage.setItem('al_model',preferredModel);activeModel='test-model';
          projData=[{path:'/demo/Project Alpha',name:'Project Alpha',sessions:[]}];
          ensureSessionTab({id:sid,name:'Existing session',cwd},true);setCurname('Existing session');
          addAsst('Current work stays open.');ta.disabled=false;ta.value='Unsent draft';
          window.starts=()=>__sent.filter(m=>m.type==='start');
          window.reply=m=>onMsg({data:JSON.stringify(m)});
          $('#newbtn').click();
        ''')
        assert await cdp.js("$('#newSessionDialog').open && $('#newCwd').value==='/demo/current'")
        assert await cdp.js("$('#newEffort').value==='xhigh'")
        assert await cdp.js('starts().length')==0
        await escape(cdp)
        assert not await cdp.js("$('#newSessionDialog').open")
        assert not await cdp.js("__sent.some(m=>m.type==='interrupt')")
        assert await cdp.js("sid==='existing'&&ta.value==='Unsent draft'&&stream.textContent.includes('Current work')")

        await cdp.js("newSessionIn('/demo/Project Alpha')")
        assert await cdp.js("$('#newCwd').value==='/demo/Project Alpha'")
        for theme in ('light','dark'):
            await cdp.js(f"document.documentElement.setAttribute('data-theme','{theme}')")
            await settle(cdp)
            assert await cdp.js('''(()=>{const r=$('#newSessionDialog').getBoundingClientRect();
              return r.x>=0&&r.right<=innerWidth&&r.y>=0&&r.bottom<=innerHeight&&
                $('#newSessionDialog').scrollWidth<=$('#newSessionDialog').clientWidth;})()''')
            shot=await cdp.call('Page.captureScreenshot',format='png')
            (output/f'new-session-{theme}-{width}.png').write_bytes(base64.b64decode(shot['data']))
        await cdp.js("$('#newModel').value='small-model';$('#newModel').dispatchEvent(new Event('change'))")
        assert await cdp.js("$('#newEffort').value==='medium'&&curEffort==='xhigh'")
        await cdp.js("__catalog=[...MODELS,{id:'new-model',name:'New model',reasoningEfforts:['high'],defaultReasoningEffort:'high'}];$('#mrefresh').click();$('#mrefresh').click()")
        await settle(cdp)
        assert await cdp.js("__refreshes===1&&$('#newModel').value==='small-model'&&$('#newEffort').value==='medium'")
        assert await cdp.js("[...$('#newModel').options].some(o=>o.value==='new-model')&&activeModel==='test-model'&&curEffort==='xhigh'")
        assert await cdp.js("preferredModel==='test-model'&&localStorage.getItem('al_model')==='test-model'")
        await cdp.js("__catalogFail=true;$('#mrefresh').click()")
        await settle(cdp)
        assert await cdp.js("!$('#mrefresh').disabled&&$('#newSessionError').textContent.includes('Could not refresh')&&$('#newModel').value==='small-model'")
        await cdp.js('__catalogFail=false')
        await cdp.js("$('#newBrowse').click()")
        await settle(cdp)
        assert await cdp.js("$('#folderList').textContent==='No subfolders'&&!$('#folderSelect').disabled")
        await cdp.js("$('#folderHome').click()")
        await settle(cdp)
        assert await cdp.js("$('#folderUp').disabled&&$('#folderList').children.length===2")
        await cdp.js("$('#folderHidden').checked=true;$('#folderHidden').dispatchEvent(new Event('change'))")
        await settle(cdp)
        assert await cdp.js("$('#folderList').children.length===3")
        assert await cdp.js("$('#folderDialog').scrollWidth<=$('#folderDialog').clientWidth")
        shot=await cdp.call('Page.captureScreenshot',format='png')
        (output/f'folder-browser-{width}.png').write_bytes(base64.b64decode(shot['data']))

        await cdp.js("$('#folderNew').click();$('#folderName').value='New experiment'")
        assert await cdp.js("!$('#folderCreateForm').hidden&&document.activeElement.id==='folderName'")
        assert await cdp.js("$('#folderDialog').scrollWidth<=$('#folderDialog').clientWidth")
        shot=await cdp.call('Page.captureScreenshot',format='png')
        (output/f'folder-create-{width}.png').write_bytes(base64.b64decode(shot['data']))
        await escape(cdp)
        assert await cdp.js("$('#folderDialog').open&&$('#folderCreateForm').hidden&&__mkdir.length===0")
        await cdp.js("$('#folderNew').click();$('#folderName').value='Cancelled';$('#folderCreateCancel').click()")
        assert await cdp.js('__mkdir.length===0')
        for name,error in (('Existing','already exists'),('Denied','No permission'),('../escape','single folder')):
            await cdp.js(f"$('#folderNew').click();$('#folderName').value='{name}';$('#folderCreateForm').requestSubmit()")
            await settle(cdp)
            assert error in await cdp.js("$('#folderCreateError').textContent")
            assert await cdp.js("!$('#folderCreateSubmit').disabled&&folderState.path==='/demo'")
        count=await cdp.js('__mkdir.length')
        await cdp.js("$('#folderName').value='New experiment';__holdCreate=true;$('#folderCreateForm').requestSubmit();$('#folderCreateForm').requestSubmit()")
        await settle(cdp)
        assert await cdp.js('__mkdir.length')==count+1
        assert await cdp.js("$('#folderClose').disabled&&$('#folderHome').disabled&&$('#folderPath').disabled&&$('#folderSelect').disabled")
        await escape(cdp)
        await cdp.js("loadFolder('/demo/other')")
        assert await cdp.js("$('#folderDialog').open&&folderState.path==='/demo'")
        await cdp.js('__releaseCreate()')
        await settle(cdp)
        assert await cdp.js("folderState.path==='/demo/New experiment'&&$('#folderCreateForm').hidden&&!$('#folderSelect').disabled")
        assert await cdp.js("starts().length===0&&sid==='existing'&&ta.value==='Unsent draft'")
        await cdp.js("$('#folderSelect').click()")
        await settle(cdp)
        assert await cdp.js("$('#newCwd').value==='/demo/New experiment'")
        await cdp.js("$('#newBrowse').click()")
        await settle(cdp)

        # A slow listing cannot overwrite a later navigation or typed path.
        await cdp.js("__hold=true;loadFolder('/slow');loadFolder('/demo/Project Alpha')")
        await settle(cdp)
        await cdp.js('__release()')
        await settle(cdp)
        assert await cdp.js("$('#folderPath').value==='/demo/Project Alpha'")
        await cdp.js("__hold=true;loadFolder('/slow');$('#folderPath').value='/typed';$('#folderPath').dispatchEvent(new Event('input'));__release()")
        await settle(cdp)
        assert await cdp.js("$('#folderPath').value==='/typed'&&$('#folderSelect').disabled")
        await cdp.js("loadFolder('/denied')")
        await settle(cdp)
        assert await cdp.js("$('#folderSelect').disabled&&$('#folderError').textContent.includes('Cannot read')")
        await cdp.js("loadFolder('/demo/Project Alpha')")
        await settle(cdp)
        await cdp.js("$('#folderSelect').click()")
        await settle(cdp)
        assert await cdp.js("!$('#folderDialog').open&&$('#newCwd').value==='/demo/Project Alpha'")
        await cdp.js("$('#newBrowse').click()")
        await settle(cdp)
        await escape(cdp)
        modal_state=await cdp.js("({folder:$('#folderDialog').open,session:$('#newSessionDialog').open,focus:document.activeElement.id})")
        assert modal_state['session'] and not modal_state['folder'],modal_state
        assert not await cdp.js("__sent.some(m=>m.type==='interrupt')")

        await cdp.js("$('#newCwd').value='/missing';$('#newSessionForm').requestSubmit()")
        await settle(cdp)
        assert await cdp.js("starts().length===0&&$('#newSessionError').textContent.includes('existing')&&sid==='existing'")
        await cdp.js("ws.readyState=3;$('#newCwd').value='/demo';$('#newSessionForm').requestSubmit()")
        assert await cdp.js("starts().length===0&&$('#newSessionError').textContent.includes('Not connected')")
        await cdp.js("ws.readyState=1;__hold=true;$('#newSessionForm').requestSubmit();$('#newSessionCancel').click();__release()")
        await settle(cdp)
        assert await cdp.js("starts().length===0&&!$('#newSessionDialog').open&&ta.value==='Unsent draft'")

        await cdp.js("newSession('/demo/Project Alpha');$('#newSessionForm').requestSubmit();$('#newSessionForm').requestSubmit()")
        await settle(cdp)
        assert await cdp.js("starts().length===1&&sid==='existing'&&$('#newSessionSubmit').disabled&&$('#newSessionClose').disabled")
        await escape(cdp)
        assert await cdp.js("$('#newSessionDialog').open")
        await cdp.js("reply({type:'error',requestId:starts()[0].requestId,error:'Start failed'})")
        assert await cdp.js("sid==='existing'&&!$('#newSessionSubmit').disabled&&$('#newSessionError').textContent==='Start failed'")
        await cdp.js("$('#newModel').value='small-model';$('#newModel').dispatchEvent(new Event('change'));$('#newMode').value='read-only';$('#newSessionForm').requestSubmit()")
        await settle(cdp)
        message=await cdp.js('starts()[1]')
        assert message['cwd']=='/demo/Project Alpha' and message['model']=='small-model'
        assert message['effort']=='medium' and message['mode']=='read-only'
        await cdp.js("reply({type:'started',requestId:starts()[1].requestId,id:'created',cwd:'/demo/Project Alpha',model:'small-model',name:'Project Alpha',effort:'medium',subagents:[]})")
        assert await cdp.js("sid==='created'&&!$('#newSessionDialog').open&&drafts.existing.text==='Unsent draft'&&sessionViewCache.has('existing')")
        assert await cdp.js("preferredModel==='small-model'&&preferredMode==='read-only'&&localStorage.getItem('al_mode')==='read-only'")
        assert not await cdp.js("__sent.some(m=>['end','detach','interrupt','resume'].includes(m.type))")

        # Import chooses a target explicitly, independent of subsequent tab changes.
        await cdp.js("window.__filePicks=0;$('#impfile').click=()=>__filePicks++;$('#impbtn').click()")
        await settle(cdp)
        assert await cdp.js("$('#folderDialog').open&&!$('#newSessionDialog').open")
        await escape(cdp)
        assert await cdp.js('__filePicks===0&&__imports.length===0')
        await cdp.js("$('#impbtn').click()")
        await settle(cdp)
        await cdp.js("loadFolder('/demo/import target')")
        await settle(cdp)
        await cdp.js("$('#folderSelect').click()")
        await settle(cdp)
        assert await cdp.js("__filePicks===1&&currentFolder()==='/demo/Project Alpha'")
        await cdp.js('''
          cwd='/demo/other';bindProject(cwd);
          const files=new DataTransfer();files.items.add(new File(['{}\\n'],'session.jsonl',{type:'application/x-ndjson'}));
          $('#impfile').files=files.files;$('#impfile').dispatchEvent(new Event('change'));
        ''')
        await settle(cdp)
        assert await cdp.js('__imports')==[{'cwd':'/demo/import target','name':'session.jsonl'}]
        assert await cdp.js("currentFolder()==='/demo/other'&&sid==='created'")
        await cdp.js("newSession()")
        assert await cdp.js("$('#newCwd').value==='/demo/other'")
        assert await cdp.js("$('#newModel').value==='small-model'&&$('#newMode').value==='read-only'")
        await cdp.js("$('#newModel').value='test-model';$('#newModel').dispatchEvent(new Event('change'));$('#newMode').value='full-access'")
        await escape(cdp)
        assert await cdp.js("preferredModel==='small-model'&&preferredMode==='read-only'&&activeModel==='small-model'&&curEffort==='medium'")
        if width==390:
            await cdp.js("$('#navtoggle').click()")
        await cdp.js("document.documentElement.setAttribute('data-theme','light')")
        await settle(cdp)
        shot=await cdp.call('Page.captureScreenshot',format='png')
        (output/f'sidebar-{width}.png').write_bytes(base64.b64decode(shot['data']))
        await cdp.js("openConfigure({id:'created',model:'small-model',mode:'read-only',effort:'medium'},$('#modelpill'))")
        assert await cdp.js("$('#cardMenu').querySelectorAll('select')[1].value==='read-only'")
        await cdp.js("closeCardMenu();resumeSession({cc:'saved-thread',cwd:'/demo/history'})")
        resumed=await cdp.js("__sent.filter(m=>m.type==='resume').at(-1)")
        assert resumed['model']=='small-model' and resumed['mode']=='read-only'
        assert await cdp.js('__errors')==[]
        print(f'{width}px: dialog, folder creation, conflicts, duplicate guard, refresh, defaults, cancellation, import PASS')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path(tempfile.gettempdir())/'new-session-check')
    asyncio.run(run_browser(parser.parse_args().output_dir,verify_page=verify))
