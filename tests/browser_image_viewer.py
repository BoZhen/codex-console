"""Real wheel, drag, pinch and dismissal events on the image preview."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import tempfile

from browser_question_flow import main as run_browser
from browser_inline_images import ImagePage, wait


class ViewerPage(ImagePage):
    def do_GET(self):
        if self.path=='/api/attachments/'+'b'*32:
            self.send_response(200);self.send_header('Content-Type','text/plain')
            self.end_headers();self.wfile.write(b'Fixture notes');return
        return super().do_GET()


async def frames(cdp):
    await cdp.js('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')


async def tap(cdp,x,y,mobile):
    if mobile:
        await cdp.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[{'x':x,'y':y}])
        await cdp.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
    else:
        await cdp.call('Input.dispatchMouseEvent',type='mousePressed',x=x,y=y,button='left',clickCount=1)
        await cdp.call('Input.dispatchMouseEvent',type='mouseReleased',x=x,y=y,button='left',clickCount=1)
    await asyncio.sleep(.05)


async def open_image(cdp):
    await cdp.js('openAttachment(__file)')
    await wait(cdp,"attachmentImageViewer&&$('#attachmentBody img')?.naturalWidth===900")
    await frames(cdp)


async def verify(cdp,url,output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument',source='''
      localStorage.clear();window.__sent=[];window.__errors=[];
      addEventListener('error',e=>{if(e.message)__errors.push(e.message);});
      window.WebSocket=class{constructor(){this.readyState=1;}send(s){__sent.push(JSON.parse(s));}};
    ''')
    root=ViewerPage.home/'Results with spaces';root.mkdir()
    file=root/'chart (1).png'
    for width in (320,390,1280):
        mobile=width<400
        await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=950,deviceScaleFactor=1,mobile=mobile)
        await cdp.call('Emulation.setTouchEmulationEnabled',enabled=mobile,maxTouchPoints=2)
        await cdp.call('Page.navigate',url=url)
        await wait(cdp,"document.readyState==='complete'&&typeof initAttachmentImageViewer==='function'")
        data=await cdp.js('''(()=>{
          const c=document.createElement('canvas');c.width=900;c.height=600;const x=c.getContext('2d');
          const colors=['#c7e4df','#f6dbbd','#dce5ef','#f2ced2'];
          for(let row=0;row<6;row++)for(let col=0;col<9;col++){
            x.fillStyle=colors[(row+col)%colors.length];x.fillRect(col*100,row*100,100,100);
            x.strokeStyle='#fff';x.strokeRect(col*100,row*100,100,100);
            x.fillStyle='#283038';x.font='22px sans-serif';x.fillText(col+','+row,col*100+28,row*100+56);
          }
          return c.toDataURL('image/png').split(',')[1];
        })()''')
        file.write_bytes(base64.b64decode(data))
        await cdp.js("sid='fixture';running=true;window.__file={kind:'image',name:'Zoom test.png',inlineSrc:'api/inline-image?path='+encodeURIComponent("+json.dumps(str(file))+"),inlineDownload:'api/inline-image?download=1&path='+encodeURIComponent("+json.dumps(str(file))+")}")
        await open_image(cdp)
        requests=ViewerPage.counts[str(file)]
        assert await cdp.js("attachmentImageViewer.panzoom.getScale()===1&&!$('#attachmentZoomReset').hidden")
        point=await cdp.js("(()=>{const r=$('#attachmentBody img').getBoundingClientRect();return {x:r.left+r.width*.68,y:r.top+r.height*.42};})()")
        await cdp.call('Input.dispatchMouseEvent',type='mouseWheel',deltaX=0,deltaY=-120,**point)
        await frames(cdp)
        anchor=await cdp.js("(()=>{const r=$('#attachmentBody img').getBoundingClientRect();return {x:("+str(point['x'])+"-r.left)/r.width,y:("+str(point['y'])+"-r.top)/r.height,scale:attachmentImageViewer.panzoom.getScale()};})()")
        assert anchor['scale']>1 and abs(anchor['x']-.68)<.01 and abs(anchor['y']-.42)<.01,anchor
        await cdp.js('attachmentImageViewer.panzoom.zoom(3,{animate:false})');await frames(cdp)
        center=await cdp.js("(()=>{const r=attachmentImageViewer.viewport.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()")
        before=await cdp.js('attachmentImageViewer.panzoom.getPan()')
        await cdp.call('Input.dispatchMouseEvent',type='mousePressed',button='left',clickCount=1,**center)
        await cdp.call('Input.dispatchMouseEvent',type='mouseMoved',button='left',buttons=1,x=center['x']+35,y=center['y']+20)
        await cdp.call('Input.dispatchMouseEvent',type='mouseReleased',button='left',clickCount=1,x=center['x']+35,y=center['y']+20)
        await frames(cdp)
        assert await cdp.js('attachmentImageViewer.panzoom.getPan()')!=before
        assert await cdp.js("$('#attachmentDialog').open")
        await cdp.js("$('#attachmentZoomReset').click()");await frames(cdp)
        assert await cdp.js("attachmentImageViewer.panzoom.getScale()===1")
        if mobile:
            center=await cdp.js("(()=>{const r=$('#attachmentBody img').getBoundingClientRect();return {x:r.left+r.width*.55,y:r.top+r.height*.45};})()")
            def points(distance):
                return [{'id':1,'x':center['x']-distance/2,'y':center['y']},
                        {'id':2,'x':center['x']+distance/2,'y':center['y']}]
            await cdp.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=points(50))
            for distance in (65,80,95,110,125,140,150):
                await cdp.call('Input.dispatchTouchEvent',type='touchMove',touchPoints=points(distance))
                await frames(cdp)
            await cdp.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
            await frames(cdp)
            assert await cdp.js("$('#attachmentDialog').open&&attachmentImageViewer.panzoom.getScale()>1.4")
            before=await cdp.js('attachmentImageViewer.panzoom.getPan()')
            await cdp.call('Input.dispatchTouchEvent',type='touchStart',touchPoints=[center])
            await cdp.call('Input.dispatchTouchEvent',type='touchMove',touchPoints=[{'x':center['x']+25,'y':center['y']+20}])
            await cdp.call('Input.dispatchTouchEvent',type='touchEnd',touchPoints=[])
            await frames(cdp)
            assert await cdp.js('attachmentImageViewer.panzoom.getPan()')!=before
            assert await cdp.js("$('#attachmentDialog').open")
        shot=await cdp.call('Page.captureScreenshot',format='png')
        (output/f'image-zoom-{width}.png').write_bytes(base64.b64decode(shot['data']))
        await cdp.js('attachmentImageViewer.panzoom.zoom(100,{animate:false})');await frames(cdp)
        assert await cdp.js('attachmentImageViewer.panzoom.getScale()')==8
        await cdp.js("$('#attachmentZoomReset').click()");await frames(cdp)
        await cdp.call('Input.dispatchMouseEvent',type='mousePressed',button='left',clickCount=1,**center)
        await cdp.call('Input.dispatchMouseEvent',type='mouseMoved',button='left',buttons=1,x=2,y=2)
        await cdp.call('Input.dispatchMouseEvent',type='mouseReleased',button='left',clickCount=1,x=2,y=2)
        assert await cdp.js("$('#attachmentDialog').open"), 'A drag ending on the backdrop is not a dismissal tap'
        assert ViewerPage.counts[str(file)]==requests, 'Zooming must not reload the source image'
        backdrop=await cdp.js("(()=>{const r=$('#navtoggle').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()")
        await tap(cdp,**backdrop,mobile=mobile)
        try:
            await wait(cdp,"!$('#attachmentDialog').open&&!attachmentImageViewer")
        except AssertionError:
            raise AssertionError(await cdp.js("({open:$('#attachmentDialog').open,gesture:attachmentDismissGesture?{outside:attachmentDismissGesture.outside,moved:attachmentDismissGesture.moved,pointers:[...attachmentDismissGesture.pointers]}:null})"))
        assert await cdp.js("!$('#sidebar').classList.contains('open')"), 'Dismissal must not click the navigation button behind the backdrop'
        await open_image(cdp)
        assert await cdp.js('attachmentImageViewer.panzoom.getScale()')==1
        center=await cdp.js("(()=>{const r=$('#attachmentBody img').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()")
        await tap(cdp,**center,mobile=mobile)
        assert await cdp.js("$('#attachmentDialog').open"), 'A tap on the image must not close it'
        blank=await cdp.js("(()=>{const r=attachmentImageViewer.viewport.getBoundingClientRect();return {x:r.left+3,y:r.top+3};})()")
        await tap(cdp,**blank,mobile=mobile)
        assert await cdp.js("!$('#attachmentDialog').open"), 'Blank image margins should also dismiss on a tap'
        await open_image(cdp)
        await cdp.js('attachmentImageViewer.panzoom.zoom(2,{animate:false})');await frames(cdp)
        await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=650,deviceScaleFactor=1,mobile=mobile)
        await wait(cdp,'attachmentImageViewer.panzoom.getScale()===1')
        await cdp.js("openAttachment({id:'"+"b"*32+"',kind:'text',name:'notes.txt'})")
        await wait(cdp,"$('#attachmentBody pre')?.textContent==='Fixture notes'")
        assert await cdp.js("!attachmentImageViewer&&!$('#attachmentDialog').classList.contains('image-open')&&$('#attachmentZoomReset').hidden")
        await cdp.js("openAttachment({id:'"+"a"*32+"',kind:'image',name:'Uploaded image.png'})")
        await wait(cdp,"attachmentImageViewer&&$('#attachmentBody img')?.naturalWidth===900")
        await frames(cdp)
        assert await cdp.js("!$('#attachmentSave').hidden&&attachmentImageViewer.panzoom.getScale()===1")
        await cdp.js("$('#attachmentDialog').close()")
        assert not await cdp.js("__sent.some(m=>m.type==='interrupt')")
        assert await cdp.js('__errors')==[]
        print(f'{width}px: cursor-anchored wheel zoom, drag, pinch, backdrop/margin close, gesture safety, fit/reset, resize and cleanup PASS')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path(tempfile.gettempdir())/'image-viewer-check')
    with tempfile.TemporaryDirectory(prefix='console-image-viewer-') as tmp:
        ViewerPage.home=Path(tmp)/'home';ViewerPage.home.mkdir()
        asyncio.run(run_browser(parser.parse_args().output_dir,verify_page=verify,page_handler=ViewerPage))
