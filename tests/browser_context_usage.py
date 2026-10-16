"""Isolated browser checks for the Codex context meter and fallback label."""
import argparse
import asyncio
import base64
from pathlib import Path
import tempfile

from browser_question_flow import main as run_browser


async def verify(cdp, url, output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', source='''
      localStorage.clear();window.__errors=[];
      addEventListener('error',e=>__errors.push(e.message));
      window.WebSocket=class {constructor(){this.readyState=1;}send(){}};
    ''')
    for width in (390,1280):
        await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=844,
                       deviceScaleFactor=1,mobile=width==390)
        await cdp.call('Page.navigate',url=url)
        for _ in range(100):
            if await cdp.js("document.readyState==='complete' && typeof renderCtx==='function'"):
                break
            await asyncio.sleep(.05)
        else:
            raise RuntimeError('Fixture page did not initialize')
        await cdp.js('''
          sid='demo';ensureSessionTab({id:sid,name:'Review changes',cwd:'/demo'},true);
          setCurname('Review changes');addAsst('The test suite has passed.');
          window.__core={source:'codex',totalTokens:795897,maxTokens:828400,
            percentage:96.1,autoCompactTokens:795897,autoCompactLimit:784800,autoCompactScope:'Total'};
          renderCtx(__core);
        ''')
        assert await cdp.js("$('#ctx').textContent")=='Context96%'
        title=await cdp.js("$('#ctx').title")
        assert '795897 / 828400' in title and '795897 / 784800' in title, title
        assert await cdp.js("$('#ctx').getAttribute('aria-label')===$('#ctx').title")
        assert not await cdp.js('document.documentElement.scrollWidth>innerWidth')
        for theme in ('dark','light'):
            await cdp.js(f"document.documentElement.setAttribute('data-theme','{theme}')")
            shot=await cdp.call('Page.captureScreenshot',format='png')
            (output/f'context-{theme}-{width}.png').write_bytes(base64.b64decode(shot['data']))
        await cdp.js("stashSessionView('demo');clearUI();restoreSessionView('demo')")
        assert await cdp.js("$('#ctx').textContent")=='Context96%'
        await cdp.js("renderCtx({source:'request',totalTokens:552898,maxTokens:828400,percentage:66.7})")
        assert await cdp.js("$('#ctx').textContent")=='Context~67%'
        assert 'unavailable' in await cdp.js("$('#ctx').title")
        assert 'Auto-compaction' not in await cdp.js("$('#ctx').title")
        await cdp.js("renderCtx({...__core,totalTokens:0,percentage:0,autoCompactTokens:0})")
        assert '0 / 828400' in await cdp.js("$('#ctx').title")
        await cdp.js('renderCtx(null)')
        assert await cdp.js("getComputedStyle($('#ctx')).display==='none'")
        assert await cdp.js('__errors')==[]
        print(f'{width}px: core context, threshold tooltip, fallback, zero, tab restore, reset PASS')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path(tempfile.gettempdir())/'context-display-check')
    asyncio.run(run_browser(parser.parse_args().output_dir,verify_page=verify))
