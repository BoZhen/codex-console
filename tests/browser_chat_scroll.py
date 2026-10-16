"""Isolated Chrome checks for jump-to-latest and per-tab reading positions."""
import argparse
import asyncio
import base64
from pathlib import Path
import tempfile

from browser_question_flow import main as run_browser


async def settle(cdp):
    await cdp.js('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(r))))')


async def state(cdp):
    return await cdp.js('''(()=>{const c=$('#chat');return {
      hidden:$('#jumpBottom').hidden,follow,top:c.scrollTop,
      gap:c.scrollHeight-c.clientHeight-c.scrollTop,
      overflow:document.documentElement.scrollWidth>innerWidth};})()''')


async def click_jump(cdp):
    point = await cdp.js('''(()=>{const r=$('#jumpBottom').getBoundingClientRect();
      return {x:r.x+r.width/2,y:r.y+r.height/2};})()''')
    if await cdp.js('innerWidth<=860'):
        await cdp.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[point])
        await cdp.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
    else:
        await cdp.call('Input.dispatchMouseEvent', type='mousePressed', button='left', clickCount=1, **point)
        await cdp.call('Input.dispatchMouseEvent', type='mouseReleased', button='left', clickCount=1, **point)
    await settle(cdp)
    s = await state(cdp)
    assert s['hidden'] and s['follow'] and s['gap'] <= 2, s


async def verify(cdp, url, output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', source='''
      localStorage.clear();window.__errors=[];
      addEventListener('error',e=>__errors.push(e.message));
      window.WebSocket=class {constructor(){this.readyState=1;}send(){}};
    ''')
    for width in (390, 1280):
        await cdp.call('Emulation.setDeviceMetricsOverride', width=width, height=844,
                       deviceScaleFactor=1, mobile=width == 390)
        await cdp.call('Emulation.setTouchEmulationEnabled', enabled=width == 390)
        await cdp.call('Page.navigate', url=url)
        for _ in range(100):
            if await cdp.js("document.readyState==='complete' && typeof syncJumpBottom==='function'"):
                break
            await asyncio.sleep(.05)
        else:
            raise RuntimeError('Fixture page did not initialize')
        await cdp.js('''
          ready=true;sid='demo-a';ensureSessionTab({id:sid,name:'Review changes',cwd:'/demo'},true);
          setCurname('Review changes');
        ''')
        await settle(cdp)
        assert (await state(cdp))['hidden']
        await cdp.js('''
          replaying=true;
          for(let i=0;i<80;i++)addAsst('Message '+(i+1)+': Review the implementation and verify the results.');
          replaying=false;scroll();
        ''')
        await settle(cdp)
        assert (await state(cdp))['hidden']
        # A real wheel gesture must leave follow mode and expose the button.
        point = await cdp.js('''(()=>{const r=$('#chat').getBoundingClientRect();
          return {x:r.x+r.width/2,y:r.y+r.height/2};})()''')
        await cdp.call('Input.dispatchMouseEvent', type='mouseWheel', deltaY=-900, deltaX=0, **point)
        for _ in range(40):
            if not (await state(cdp))['hidden']:
                break
            await asyncio.sleep(.025)
        s = await state(cdp)
        assert not s['hidden'] and not s['follow'] and not s['overflow'], s
        before = s['top']
        await cdp.js("addAsst('A new response arrives while reading history.');")
        await settle(cdp)
        s = await state(cdp)
        assert not s['hidden'] and abs(s['top'] - before) < 2, s
        bounds = await cdp.js('''(()=>{const b=$('#jumpBottom'),r=b.getBoundingClientRect(),
          c=$('#chat').getBoundingClientRect(),p=$('#composer').getBoundingClientRect();
          return {visible:b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)),
            inside:r.x>=c.x&&r.right<=c.right&&r.y>=c.y&&r.bottom<=c.bottom&&r.bottom<=p.y,
            size:r.width,icon:b.querySelector('svg').getBoundingClientRect().width};})()''')
        assert bounds['visible'] and bounds['inside'] and bounds['icon'] == 20, bounds
        assert bounds['size'] == (44 if width == 390 else 40), bounds
        for theme in ('light', 'dark'):
            await cdp.js(f"document.documentElement.setAttribute('data-theme','{theme}')")
            await settle(cdp)
            shot = await cdp.call('Page.captureScreenshot', format='png')
            (output / f'jump-latest-{theme}-{width}.png').write_bytes(base64.b64decode(shot['data']))
        await click_jump(cdp)
        await cdp.js("addAsst('Streaming continues at the latest message.');")
        await settle(cdp)
        s = await state(cdp)
        assert s['hidden'] and s['follow'] and s['gap'] <= 2, s

        if width == 390:
            await cdp.call('Input.dispatchTouchEvent', type='touchStart', touchPoints=[{'x':190,'y':260}])
            for y in range(280, 601, 40):
                await cdp.call('Input.dispatchTouchEvent', type='touchMove', touchPoints=[{'x':190,'y':y}])
                await asyncio.sleep(.03)
            await asyncio.sleep(.1)
            await cdp.call('Input.dispatchTouchEvent', type='touchEnd', touchPoints=[])
            await asyncio.sleep(.2)
            s = await state(cdp)
            assert not s['hidden'] and not s['follow'], s
            await click_jump(cdp)

        # Keyboard activation works, without a composer submission.
        await cdp.js("$('#chat').scrollTop=240")
        await settle(cdp)
        assert not (await state(cdp))['hidden']
        await cdp.js("$('#jumpBottom').focus()")
        await cdp.enter()
        await settle(cdp)
        assert (await state(cdp))['hidden'] and (await state(cdp))['follow']

        # The real tab cache restores both scroll position and button state.
        await cdp.js("$('#chat').scrollTop=240")
        await settle(cdp)
        before = (await state(cdp))['top']
        await cdp.js('''
          ensureSessionTab({id:'demo-b',name:'Short conversation',cwd:'/demo'},false);
          switchSession('demo-b');ready=true;addAsst('Only one short message.');scroll();
        ''')
        await settle(cdp)
        assert (await state(cdp))['hidden']
        await cdp.js("switchSession('demo-a')")
        await settle(cdp)
        s = await state(cdp)
        assert not s['hidden'] and not s['follow'] and abs(s['top']-before) < 2, s
        await click_jump(cdp)
        await cdp.js("switchSession('demo-b')")
        await settle(cdp)
        await cdp.js("switchSession('demo-a')")
        await settle(cdp)
        assert (await state(cdp))['hidden'] and (await state(cdp))['follow']

        # DOM-window trimming still leaves the last message reachable.
        await cdp.js('''
          replaying=true;for(let i=0;i<360;i++)addAsst('Windowed message '+i);
          replaying=false;scroll();trimChatWindow(true);
        ''')
        await settle(cdp)
        assert await cdp.js('windowHidden>0')
        await cdp.js("$('#chat').scrollTop=400")
        await settle(cdp)
        await click_jump(cdp)
        assert await cdp.js("stream.lastElementChild.textContent==='Windowed message 359'")

        # ResizeObserver handles content growth/collapse with no scroll event.
        await cdp.js("clearUI();addAsst('Short content.');scroll()")
        await settle(cdp)
        assert (await state(cdp))['hidden']
        await cdp.js("stream.lastElementChild.style.height='2400px'")
        await settle(cdp)
        assert not (await state(cdp))['hidden']
        await cdp.js("stream.lastElementChild.style.height=''")
        await settle(cdp)
        assert (await state(cdp))['hidden']
        await cdp.js('clearUI()')
        await settle(cdp)
        assert (await state(cdp))['hidden']
        assert await cdp.js('__errors') == []
        print(f'{width}px: wheel, jump, keyboard, streaming, tab cache, DOM window, resize, empty PASS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path(tempfile.gettempdir()) / 'chat-scroll-check')
    asyncio.run(run_browser(parser.parse_args().output_dir, verify_page=verify))
