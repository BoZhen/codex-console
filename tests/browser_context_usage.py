"""Isolated browser checks for compact context and rate-limit rings."""
import argparse
import asyncio
import base64
from pathlib import Path
import tempfile

from browser_question_flow import main as run_browser


async def tap(cdp, selector, mobile):
    point=await cdp.js("(()=>{const r=document.querySelector("+repr(selector)+").getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
    if mobile:
        await cdp.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[point])
        await cdp.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
    else:
        await cdp.call('Input.dispatchMouseEvent',type='mousePressed',button='left',clickCount=1,**point)
        await cdp.call('Input.dispatchMouseEvent',type='mouseReleased',button='left',clickCount=1,**point)
    await asyncio.sleep(.05)


async def verify(cdp, url, output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', source='''
      localStorage.clear();window.__errors=[];window.__sent=[];
      addEventListener('error',e=>__errors.push(e.message));
      window.WebSocket=class {constructor(){this.readyState=1;}send(raw){__sent.push(JSON.parse(raw));}};
    ''')
    for width in (320,360,390,768,1280):
        await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=844,
                       deviceScaleFactor=1,mobile=width<=390)
        await cdp.call('Emulation.setTouchEmulationEnabled',enabled=width<=390,maxTouchPoints=1)
        await cdp.call('Page.navigate',url=url)
        for _ in range(100):
            if await cdp.js("document.readyState==='complete' && typeof renderCtx==='function'"):
                break
            await asyncio.sleep(.05)
        else:
            raise RuntimeError('Fixture page did not initialize')
        await cdp.js('''
          sid='demo';ensureSessionTab({id:sid,name:'Review changes',cwd:'/demo'},true);
          setCurname('Review changes and benchmark results');addAsst('The test suite has passed.');
          window.__core={source:'codex',totalTokens:795897,maxTokens:828400,
            percentage:96.1,autoCompactTokens:795897,autoCompactLimit:784800,autoCompactScope:'Total'};
          renderCtx(__core);
          renderUsage({five_hour:{utilization:12},seven_day:{utilization:45,resets_at:new Date(Date.now()+3600000).toISOString()}});
          mergeSubagents([{threadId:'demo-child',state:'completed',active:false}],true);
        ''')
        assert await cdp.js("$('#ctx').textContent")=='ctx'
        title=await cdp.js("$('#ctx').title")
        assert '795897 / 828400' in title and '795897 / 784800' in title, title
        assert await cdp.js("$('#ctx').getAttribute('aria-label')===$('#ctx').title")
        assert await cdp.js("$('#ctx').tagName==='BUTTON'&&$('#ctx').getAttribute('aria-haspopup')==='dialog'")
        assert await cdp.js("getComputedStyle($('#usage')).display!=='none'&&$('#usage .useg-week').textContent==='7d'")
        assert await cdp.js("!document.querySelector('header .meter-pct')&&!document.querySelector('header').textContent.includes('%')")
        assert await cdp.js("$('#usage .useg-week').title.includes('45% used')&&$('#usage .useg-week').title.includes('resets in')")
        assert await cdp.js("getComputedStyle($('#usage .useg-five')).display==='none'") == (width<=680)
        assert await cdp.js("$('#usage .useg-week .meter-ring').style.getPropertyValue('--used')==='45%'")
        assert not await cdp.js('document.documentElement.scrollWidth>innerWidth')
        assert await cdp.js('''(()=>{
          const header=document.querySelector('header').getBoundingClientRect();
          const controls=[...document.querySelectorAll('header>*')].filter(e=>getComputedStyle(e).display!=='none');
          return controls.every((e,i)=>{const b=e.getBoundingClientRect();return b.top>=header.top&&b.bottom<=header.bottom&&b.right<=innerWidth&&(!i||b.left>=controls[i-1].getBoundingClientRect().right);});
        })()'''), 'Header must remain one row without clipped or overlapping controls'
        for theme in ('dark','light'):
            await cdp.js(f"document.documentElement.setAttribute('data-theme','{theme}')")
            assert await cdp.js("getComputedStyle($('#ctx .meter-ring')).backgroundImage.includes('conic-gradient')&&getComputedStyle($('#ctx .meter-ring')).maskImage.includes('radial-gradient')")
            shot=await cdp.call('Page.captureScreenshot',format='png')
            (output/f'context-{theme}-{width}.png').write_bytes(base64.b64decode(shot['data']))
            await tap(cdp,'#ctx .meter-ring',width<=390)
            assert await cdp.js("$('#meterDialog').open&&$('#meterTitle').textContent==='Context usage'&&$('#meterDetails').textContent.includes('795897 / 828400')&&$('#meterDetails').textContent.includes('96%')&&$('#ctx').getAttribute('aria-expanded')==='true'")
            assert await cdp.js("(()=>{const d=$('#meterDialog').getBoundingClientRect(),t=$('#meterDetails');return d.x>=0&&d.right<=innerWidth&&d.y>=0&&d.bottom<=innerHeight&&t.scrollWidth<=t.clientWidth;})()")
            shot=await cdp.call('Page.captureScreenshot',format='png')
            (output/f'context-details-{theme}-{width}.png').write_bytes(base64.b64decode(shot['data']))
            await cdp.js("renderCtx({...__core,percentage:50,totalTokens:414200})")
            assert await cdp.js("$('#meterDialog').open&&$('#meterDetails').textContent.includes('414200 / 828400')&&$('#meterDetails').textContent.includes('50%')")
            await tap(cdp,'#meterClose',width<=390)
            assert await cdp.js("!$('#meterDialog').open&&$('#ctx').getAttribute('aria-expanded')==='false'")
            await cdp.js('renderCtx(__core)')
            await tap(cdp,'#usageWeek .meter-ring',width<=390)
            assert await cdp.js("$('#meterDialog').open&&$('#meterTitle').textContent==='Weekly usage'&&$('#meterDetails').textContent.includes('45% used')&&$('#meterDetails').textContent.includes('Reset:')")
            await cdp.js("window.__weekButton=$('#usageWeek');renderUsage({five_hour:{utilization:12},seven_day:{utilization:46}})")
            assert await cdp.js("$('#usageWeek')===__weekButton&&$('#meterDialog').open&&$('#meterDetails').textContent.includes('46% used')")
            await tap(cdp,'#navtoggle',width<=390)
            assert await cdp.js("!$('#meterDialog').open&&!$('#sidebar').classList.contains('open')"), 'Backdrop tap dismisses details without activating the underlying page'
            await cdp.js("renderUsage({five_hour:{utilization:12},seven_day:{utilization:45,resets_at:new Date(Date.now()+3600000).toISOString()}})")
        await cdp.js("running=true;openSubagents();$('#ctx').focus()")
        await cdp.enter()
        assert await cdp.js("$('#meterDialog').open")
        await cdp.call('Input.dispatchKeyEvent',type='keyDown',key='Escape',code='Escape',windowsVirtualKeyCode=27)
        await cdp.call('Input.dispatchKeyEvent',type='keyUp',key='Escape',code='Escape',windowsVirtualKeyCode=27)
        assert await cdp.js("!$('#meterDialog').open&&agentPanelOpen()&&!__sent.some(m=>m.type==='interrupt')"), 'Escape only closes details, not the running task or subagent panel'
        await cdp.js("running=false;closeSubagents();$('#ctx').click();$('#meterDialog').close()")
        await asyncio.sleep(.05)
        assert await cdp.js("!$('#meterDialog').open&&$('#ctx').getAttribute('aria-expanded')==='false'"), 'Android Back can use its existing generic dialog-close path'
        if width<=390:
            await cdp.js('''
              window.__largeType=document.createElement('style');
              __largeType.textContent='.ctx,.usage,header button{font-size:20px!important}#agentbtn{display:none!important}';
              document.head.appendChild(__largeType);
              renderCtx({...__core,source:'request',percentage:100});renderUsage({seven_day:{utilization:100}});
            ''')
            assert await cdp.js("(()=>{const h=document.querySelector('header').getBoundingClientRect(),c=$('#ctx').getBoundingClientRect(),u=$('#usage').getBoundingClientRect();return c.right<=u.left&&u.right<=innerWidth&&c.bottom<=h.bottom&&u.top===c.top&&document.documentElement.scrollWidth<=innerWidth;})()"), 'Large Android interface text must retain both meters on one row'
            await cdp.js('__largeType.remove();renderCtx(__core)')
        await cdp.js("stashSessionView('demo');$('#ctx').click();clearUI();restoreSessionView('demo')")
        assert await cdp.js("$('#ctx').textContent==='ctx'&&!$('#meterDialog').open")
        await cdp.js("renderCtx({source:'request',totalTokens:552898,maxTokens:828400,percentage:66.7})")
        assert await cdp.js("$('#ctx').textContent")=='ctx'
        assert await cdp.js("$('#ctx').getAttribute('aria-label').includes('67%')")
        assert 'unavailable' in await cdp.js("$('#ctx').title")
        assert 'Auto-compaction' not in await cdp.js("$('#ctx').title")
        await cdp.js("renderCtx({...__core,totalTokens:0,percentage:0,autoCompactTokens:0})")
        assert '0 / 828400' in await cdp.js("$('#ctx').title")
        dimensions=[]
        for percentage,level in ((0,'g'),(25,'g'),(50,'y'),(60,'o'),(75,'o'),(80,'o'),(96,'r'),(100,'r')):
            await cdp.js(f"renderCtx({{...__core,percentage:{percentage}}});renderUsage({{seven_day:{{utilization:{percentage}}}}})")
            assert await cdp.js(f"[...document.querySelectorAll('header .meter-ring')].every(r=>r.style.getPropertyValue('--used')==='{percentage}%'&&r.classList.contains('lv-{level}'))")
            dimensions.append(await cdp.js("[...document.querySelectorAll('header .meter-ring')].map(r=>[r.offsetWidth,r.offsetHeight])"))
        assert all(value==[[18,18],[18,18]] for value in dimensions), dimensions
        await cdp.js("renderCtx({...__core,percentage:125});renderUsage({seven_day:{utilization:-5}})")
        assert await cdp.js("$('#ctx .meter-ring').style.getPropertyValue('--used')==='100%'&&$('#usage .meter-ring').style.getPropertyValue('--used')==='0%'")
        await cdp.js("renderUsage({five_hour:{utilization:55}})")
        assert await cdp.js("!$('#usage').classList.contains('has-weekly')&&getComputedStyle($('#usage .useg-five')).display!=='none'&&!$('#usage .useg-week')")
        await cdp.js("$('#usageFive').click();renderUsage(null)")
        assert await cdp.js("!$('#meterDialog').open"), 'Missing data must dismiss stale details'
        for invalid in ('null', '{}', '{seven_day:{utilization:null}}', "{seven_day:{utilization:'bad'}}"):
            await cdp.js(f'renderUsage({invalid})')
            assert await cdp.js("getComputedStyle($('#usage')).display==='none'")
        await cdp.js('renderCtx(null)')
        assert await cdp.js("getComputedStyle($('#ctx')).display==='none'")
        assert await cdp.js('__errors')==[]
        print(f'{width}px: ring-only header, touch/click details, live refresh, keyboard/Back dismissal, no interrupt, proportions/colors and tab restore PASS')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path(tempfile.gettempdir())/'context-display-check')
    asyncio.run(run_browser(parser.parse_args().output_dir,verify_page=verify))
