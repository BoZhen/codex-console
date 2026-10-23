"""Capture the real frontend with synthetic sessions and files, without Codex.

Run in the Console Python environment; requires google-chrome on PATH.
"""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import tempfile

from browser_question_flow import PageHandler, main as run_browser


class ReadmePage(PageHandler):
    chart = b''

    def do_GET(self):
        if self.path.startswith('/api/attachments/' + 'a' * 32):
            body, mime = type(self).chart, 'image/png'
        elif self.path.startswith('/api/attachments/' + 'b' * 32):
            body, mime = b'case,baseline_ms,current_ms\nSmall,120,82\nMedium,210,145\nLarge,360,248\n', 'text/plain'
        elif self.path.startswith('/api/models'):
            body = json.dumps({'defaultModel': 'demo-model', 'models': [{
                'id': 'demo-model', 'name': 'Codex', 'isDefault': True,
                'reasoningEfforts': ['medium', 'high', 'xhigh'], 'defaultReasoningEffort': 'high',
                'defaultServiceTier': 'fast', 'serviceTiers': [{'id': 'fast', 'name': 'Fast'}]
            }]}).encode()
            mime = 'application/json'
        elif self.path.startswith('/api/transcribe'):
            body = b'{"enabled":true,"available":true,"livePreview":true}'
            mime = 'application/json'
        else:
            return super().do_GET()
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.end_headers()
        self.wfile.write(body)


async def capture(cdp, url, output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', source='''
      window.__errors=[];window.__sent=[];
      addEventListener('error',e=>__errors.push(e.message));
      window.WebSocket=class {constructor(){this.readyState=1;}send(s){__sent.push(JSON.parse(s));}};
    ''')
    await cdp.call('Emulation.setDeviceMetricsOverride', width=1440, height=960, deviceScaleFactor=1, mobile=False)
    await cdp.call('Page.navigate', url=url)
    for _ in range(100):
        if await cdp.js("document.readyState==='complete' && typeof addUser==='function' && MODELS.length>0"):
            break
        await asyncio.sleep(.05)
    else:
        raise RuntimeError('Screenshot fixture did not initialize')
    ReadmePage.chart = base64.b64decode(await cdp.js('''(()=>{
      const canvas=document.createElement('canvas');canvas.width=720;canvas.height=400;
      const c=canvas.getContext('2d');c.fillStyle='#f5f8fc';c.fillRect(0,0,720,400);
      c.fillStyle='#263746';c.font='bold 26px sans-serif';c.fillText('Latency by input size',38,44);
      c.font='18px sans-serif';c.fillText('Milliseconds',38,75);
      const colors=['#72869a','#288c85'];
      [120,210,360].forEach((v,i)=>{
        const x=110+i*200;
        for(const [j,value] of [v,[82,145,248][i]].entries()){
          c.fillStyle=colors[j];c.fillRect(x+j*48,335-value*.6,34,value*.6);
        }
        c.fillStyle='#263746';c.fillText(['Small','Medium','Large'][i],x,369);
      });
      c.font='17px sans-serif';
      colors.forEach((color,i)=>{c.fillStyle=color;c.fillRect(410+i*135,55,14,14);
        c.fillStyle='#263746';c.fillText(['Baseline','Current'][i],430+i*135,69);});
      return canvas.toDataURL('image/png').split(',')[1];
    })()'''))
    await cdp.js('''
      applyTheme('light');$('#theme').value='light';HOMEDIR='/home/demo';sid='demo-review';curCC='demo-thread';
      cwd='/home/demo/workspace/sample-app';ready=true;activeModel='demo-model';activeTier='default';
      setComposerEnabled(true);setBusy(false);setCurname('Benchmark review');setEffortPill('xhigh');
      const now=Date.now()/1000;
      projData=[
        {path:cwd,name:'sample-app',fav:true,sessions:[{cc:'past-1',title:'Error handling',mtime:now-86400}]},
        {path:'/home/demo/workspace/web-api',name:'web-api',sessions:[{cc:'past-2',title:'Request validation',mtime:now-172800}]},
        {path:'/home/demo/workspace/docs-site',name:'docs-site',fav:true,sessions:[{cc:'past-3',title:'Navigation polish',mtime:now-259200}]},
        {path:'/home/demo/workspace/cli-tools',name:'cli-tools',mtime:now-345600,sessions:[{cc:'past-4',title:'Import command',mtime:now-345600}]}
      ];
      const sessions=[
        {id:sid,cc:curCC,name:'Benchmark review',cwd,model:'demo-model',busy:false},
        {id:'demo-api',cc:'demo-api-thread',name:'API tests',cwd:'/home/demo/workspace/web-api',busy:true},
        {id:'demo-docs',cc:'demo-docs-thread',name:'Release notes',cwd,busy:false}
      ];
      projData.forEach(p=>pExp.add(p.path));
      sessions.forEach(s=>ensureSessionTab(s,s.id===sid));renderLive(sessions);
      renderCtx({source:'codex',percentage:32,totalTokens:32000,maxTokens:100000});
      renderUsage({seven_day:{utilization:24}});
      mergeSubagents([{threadId:'demo-agent',label:'reviewer',state:'completed',message:'Checked benchmark inputs.'}],true);
      upsertPlan({plan:[{step:'Inspect the uploaded results',status:'completed'},
        {step:'Run the regression checks',status:'in_progress'},{step:'Update the report',status:'pending'}]});
      addUser('Review the benchmark chart and update the report.',1,[],[
        {id:'a'.repeat(32),kind:'image',name:'latency.png',size:18432},
        {id:'b'.repeat(32),kind:'text',name:'benchmark.csv',size:78}
      ]);
      addAsst('The current run is faster in all three cases. I will check the measurements and update `report.md`.');
      for(const [i,name] of ['src/benchmark.py','tests/test_benchmark.py'].entries()){
        addTool({tool:'Read',toolId:'read-'+i,input:{file_path:name}});
        addResult({toolId:'read-'+i,status:'completed',content:'Read complete.'});
      }
      document.querySelector('.tgh').click();
      for(const [i,command] of ['python -m unittest tests.test_benchmark','git diff --check'].entries()){
        addTool({tool:'Bash',toolId:'bash-'+i,input:{command}});
        addResult({toolId:'bash-'+i,status:'completed',exitCode:0,content:i?'':'Ran 12 tests. OK'});
      }
      addAsst('All 12 checks passed. The latency reduction is consistent across the sample sizes.');
      setBusy(false);statset('ready');renderSessionChoices();syncVoiceButton();
      document.activeElement.blur();
    ''')
    for _ in range(50):
        if await cdp.js("[...document.querySelectorAll('img')].every(i=>i.complete&&i.naturalWidth>0)"):
            break
        await asyncio.sleep(.05)
    else:
        raise RuntimeError('Screenshot image assets did not load')
    await cdp.js("document.fonts.ready.then(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))))")
    assert await cdp.js("$('#speedpill').textContent==='Standard'")
    assert await cdp.js("document.documentElement.scrollWidth<=innerWidth")
    assert await cdp.js("$('#stream').scrollWidth<=$('#chat').clientWidth")
    assert await cdp.js('__errors') == []
    assert not await cdp.js("__sent.some(m=>['start','resume','user'].includes(m.type))")
    for width, height, filename in ((1440,900,'codex-console-overview.png'),(390,844,'codex-console-mobile.png')):
        await cdp.call('Emulation.setDeviceMetricsOverride',width=width,height=height,deviceScaleFactor=1,mobile=width<860)
        if width < 860:
            await cdp.js('closeSidebar();planCollapsed=true;renderCurrentPlan()')
        await cdp.js('new Promise(r=>setTimeout(r,300))')
        await cdp.js('setChatTop(0,true);new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))')
        assert await cdp.js('document.documentElement.scrollWidth<=innerWidth')
        shot=await cdp.call('Page.captureScreenshot',format='png')
        (output/filename).write_bytes(base64.b64decode(shot['data']))
    print('PASS: synthetic README screenshots; Standard default, assets loaded, no model calls or page errors')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,default=Path(tempfile.gettempdir())/'codex-readme')
    asyncio.run(run_browser(parser.parse_args().output_dir, verify_page=capture, page_handler=ReadmePage))
