"""Inline result images on the actual frontend, using synthetic local files."""
import argparse
import asyncio
import base64
from collections import Counter
import json
from pathlib import Path
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

from browser_question_flow import PageHandler, main as run_browser
from attachments import read_local_image


class ImagePage(PageHandler):
    home = None
    counts = Counter()
    release = threading.Event()

    def do_GET(self):
        url=urlsplit(self.path)
        if url.path=='/api/inline-image':
            path=parse_qs(url.query).get('path',[''])[0]
            type(self).counts[path]+=1
            if Path(path).name.startswith('slow-'):type(self).release.wait(8)
            try:
                meta,body=read_local_image(path,str(type(self).home))
            except (OSError,ValueError):
                self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type',meta['media_type'])
            self.send_header('Cache-Control','private, no-store');self.end_headers();self.wfile.write(body);return
        if url.path=='/api/attachments/'+'a'*32:
            self.send_response(200);self.send_header('Content-Type','image/png');self.end_headers()
            self.wfile.write((type(self).home/'Results with spaces/chart (1).png').read_bytes());return
        return super().do_GET()


async def wait(cdp, expression):
    for _ in range(150):
        if await cdp.js(expression):return
        await asyncio.sleep(.04)
    raise AssertionError(expression)


async def verify(cdp,url,output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument',source='''
      localStorage.clear();window.__errors=[];window.__sent=[];
      addEventListener('error',e=>{if(e.message)__errors.push(e.message);});
      window.WebSocket=class {constructor(){this.readyState=1;}send(raw){__sent.push(JSON.parse(raw));}};
    ''')
    root=ImagePage.home/'Results with spaces';root.mkdir()
    image=root/'chart (1).png'
    for width in (320,390,1280):
        await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=950,deviceScaleFactor=1,mobile=width<400)
        await cdp.call('Emulation.setTouchEmulationEnabled',enabled=width<400,maxTouchPoints=1)
        await cdp.call('Page.navigate',url=url)
        await wait(cdp,"document.readyState==='complete'&&typeof renderMarkdownInto==='function'")
        data=await cdp.js('''(()=>{
          const canvas=document.createElement('canvas');canvas.width=900;canvas.height=480;
          const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,900,480);
          ctx.fillStyle='#283038';ctx.font='bold 27px sans-serif';ctx.fillText('Convergence benchmark',55,48);
          ctx.font='19px sans-serif';ctx.fillText('Error',55,83);ctx.fillText('Time steps',700,448);
          ctx.strokeStyle='#d9dfe4';ctx.lineWidth=1;
          for(let y=110;y<=390;y+=70){ctx.beginPath();ctx.moveTo(80,y);ctx.lineTo(840,y);ctx.stroke();}
          ctx.strokeStyle='#46525c';ctx.beginPath();ctx.moveTo(80,100);ctx.lineTo(80,400);ctx.lineTo(845,400);ctx.stroke();
          ctx.strokeStyle='#118a7e';ctx.lineWidth=4;ctx.beginPath();
          [[90,125],[210,215],[330,288],[450,335],[570,365],[690,382],[820,390]].forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke();
          ctx.fillStyle='#118a7e';for(const [x,y] of [[90,125],[210,215],[330,288],[450,335],[570,365],[690,382],[820,390]]){ctx.beginPath();ctx.arc(x,y,5,0,Math.PI*2);ctx.fill();}
          return canvas.toDataURL('image/png').split(',')[1];
        })()''')
        image.write_bytes(base64.b64decode(data))
        special=root/'result 10% # a+b & check.png';special.write_bytes(image.read_bytes())
        (output/'example-result.png').write_bytes(image.read_bytes())
        await cdp.js("sid='demo';ready=true;running=true;cwd="+json.dumps(str(root))+";setCurname('Convergence results');window.__imagePath="+json.dumps(str(image)))
        text="Here are the results:\n\n![Convergence benchmark](<"+str(image)+">)\n\nThe image shows decreasing error as the number of time steps grows."
        await cdp.js('addAsst('+json.dumps(text)+')')
        await wait(cdp,"stream.querySelector('.inline-image.ready img')?.naturalWidth===900")
        assert await cdp.js("stream.textContent.indexOf('Here are the results')<stream.textContent.indexOf('Convergence benchmark')&&stream.textContent.indexOf('Convergence benchmark')<stream.textContent.indexOf('The image shows')")
        assert await cdp.js("stream.querySelector('.inline-image figcaption a').href.includes(encodeURIComponent(__imagePath))")
        for theme in ('light','dark'):
            await cdp.js("document.documentElement.setAttribute('data-theme',"+json.dumps(theme)+")")
            assert await cdp.js("(()=>{const image=stream.querySelector('.inline-image img').getBoundingClientRect();return image.width>100&&image.height>50&&image.x>=0&&image.right<=innerWidth&&document.documentElement.scrollWidth<=innerWidth;})()")
            shot=await cdp.call('Page.captureScreenshot',format='png')
            (output/f'inline-image-{theme}-{width}.png').write_bytes(base64.b64decode(shot['data']))
        button=await cdp.js("(()=>{const r=stream.querySelector('.inline-image-preview').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
        if width<400:
            await cdp.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[button])
            await cdp.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
        else:
            await cdp.call('Input.dispatchMouseEvent',type='mousePressed',button='left',clickCount=1,**button)
            await cdp.call('Input.dispatchMouseEvent',type='mouseReleased',button='left',clickCount=1,**button)
        await wait(cdp,"$('#attachmentDialog').open&&$('#attachmentBody img')?.naturalWidth===900")
        assert await cdp.js("$('#attachmentSave').hidden&&$('#attachmentDownload').href.includes('download=1')&&$('#attachmentFeedback').textContent===__imagePath")
        await cdp.call('Input.dispatchKeyEvent',type='keyDown',key='Escape',code='Escape',windowsVirtualKeyCode=27)
        await cdp.call('Input.dispatchKeyEvent',type='keyUp',key='Escape',code='Escape',windowsVirtualKeyCode=27)
        assert not await cdp.js("$('#attachmentDialog').open||__sent.some(m=>m.type==='interrupt')")

        await cdp.js("upsertAsst({itemId:'streamed',delta:"+json.dumps(text)+"});flushAsstRenders(false)")
        await wait(cdp,"textItems.streamed.el.querySelector('.inline-image.ready img')?.naturalWidth===900")
        await cdp.js("window.__streamImage=textItems.streamed.el.querySelector('img')")
        count=ImagePage.counts[str(image)]
        await cdp.js("for(let i=0;i<12;i++){upsertAsst({itemId:'streamed',delta:' Additional observation.'});flushAsstRenders(false);}")
        await asyncio.sleep(.1)
        assert await cdp.js("textItems.streamed.el.querySelector('img')===__streamImage&&__streamImage.naturalWidth===900")
        assert ImagePage.counts[str(image)]==count, 'Streaming must not reload a completed image'
        await cdp.js("running=false;ensureSessionTab({id:'demo',name:'Results',cwd},true);window.__originalImage=stream.querySelector('img');stashSessionView('demo');clearUI();sid='other';cwd='/demo/other';addAsst('Independent conversation');sid='demo';restoreSessionView('demo')")
        assert await cdp.js("stream.querySelector('img')===__originalImage&&__originalImage.src.includes(encodeURIComponent(__imagePath))")

        for source in ('![relative](chart%20(1).png)', '![tilde](<~/Results with spaces/chart (1).png>)',
                       '![file URI]('+image.as_uri()+')',
                       '![reserved](<'+str(special)+'>)',
                       '![attachment](/api/attachments/'+'a'*32+')',
                       '![quoted](<'+str(image)+'> "Caption title")'):
            await cdp.js("window.__sample=document.createElement('div');renderMarkdownInto(__sample,"+json.dumps(source)+")")
            assert await cdp.js("__sample.querySelector('.inline-image img')!==null")
        await cdp.js("subagentCurrent='child';subagents.child={cwd:"+json.dumps(str(root/'child'))+"};__sample.innerHTML=agentMsgHTML({role:'assistant',txt:'![child](chart.png)'})")
        assert await cdp.js("__sample.querySelector('.inline-image').dataset.imagePath.endsWith('/child/chart.png')")
        ticks=chr(96)
        for source in (ticks+'![code]('+str(image)+')'+ticks,ticks*3+'markdown\n![code]('+str(image)+')\n'+ticks*3,
                       '![remote](https://tracker.invalid/pixel.png)','![script](javascript:alert(1))',
                       '![svg](<'+str(root/'active.svg')+'>)'):
            await cdp.js("renderMarkdownInto(__sample,"+json.dumps(source)+")")
            assert await cdp.js("__sample.querySelector('img')===null")
        assert not await cdp.js("performance.getEntriesByType('resource').some(r=>r.name.startsWith('https://tracker.invalid'))")
        malicious='!["><img src=x onerror=window.__executed=true>](<'+str(image)+'>)'
        await cdp.js("renderMarkdownInto(__sample,"+json.dumps(malicious)+")")
        assert await cdp.js("__sample.querySelectorAll('img').length===1&&!window.__executed")
        await cdp.js("addAsst('![missing](<'+cwd+'/missing.png>)')")
        await wait(cdp,"!!stream.querySelector('.inline-image.failed')")
        assert await cdp.js("!stream.querySelector('.inline-image.failed .inline-image-note').hidden&&!!stream.querySelector('.inline-image.failed figcaption a')")
        await cdp.js("openAttachment({id:'"+"a"*32+"',name:'Uploaded image',kind:'image'})")
        assert await cdp.js("!$('#attachmentSave').hidden"), 'Uploaded attachments keep their Save action'
        await cdp.js("$('#attachmentDialog').close()")
        slow=root/f'slow-{width}.png';slow.write_bytes(image.read_bytes());ImagePage.release.clear()
        await cdp.js("for(let i=0;i<65;i++)addAsst('Earlier benchmark observation '+i);addAsst("+json.dumps('![Slow result](<'+str(slow)+'>)')+");scroll()")
        for _ in range(100):
            if ImagePage.counts[str(slow)]:break
            await asyncio.sleep(.03)
        assert ImagePage.counts[str(slow)], 'The delayed image request must have started'
        await cdp.js("setChatTop(60,false)")
        ImagePage.release.set()
        await wait(cdp,"stream.querySelector('figure:last-of-type')!==null&&[...stream.querySelectorAll('.inline-image')].some(f=>f.dataset.imageSrc.includes('slow-')&&f.classList.contains('ready'))")
        assert await cdp.js("Math.abs($('#chat').scrollTop-60)<=1&&!atBottom()"), 'An image finishing must not pull the reader to the bottom'
        assert await cdp.js('__errors')==[]
        print(f'{width}px: inline placement, real pixels, responsive sizing, touch preview/download, stable streaming, safe sources and fallback PASS')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path(tempfile.gettempdir())/'inline-images')
    with tempfile.TemporaryDirectory(prefix='codex-inline-images-') as tmp:
        ImagePage.home=Path(tmp)/'home';ImagePage.home.mkdir()
        try:
            asyncio.run(run_browser(parser.parse_args().output_dir,verify_page=verify,page_handler=ImagePage))
        finally:
            ImagePage.release.set()
