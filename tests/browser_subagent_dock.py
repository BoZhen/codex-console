"""Subagent side-panel checks with synthetic sessions and read-only replies."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import tempfile

from browser_question_flow import PageHandler, main as run_browser


class SubagentPage(PageHandler):
    def do_GET(self):
        if self.path.startswith('/api/project-library'):
            body = json.dumps({'sessions': [], 'folders': [], 'groups': []}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.end_headers(); self.wfile.write(body)
            return
        return super().do_GET()


async def wait(cdp, expression):
    for _ in range(120):
        if await cdp.js(expression): return
        await asyncio.sleep(.05)
    raise AssertionError(expression)


async def verify(cdp, url, output):
    await cdp.call('Page.enable')
    await cdp.call('Page.addScriptToEvaluateOnNewDocument', source='''
      localStorage.clear();window.__sent=[];window.__errors=[];window.__parents={};window.__held=[];window.__holdParent='';
      addEventListener('error',e=>__errors.push(e.message));
      window.__messages=(tid)=>Array.from({length:24},(_,i)=>({itemId:tid+'-'+i,turnId:'turn-'+tid,
        role:i%3===0?'tool':'assistant',kind:i%3===0?'commandExecution':'agentMessage',status:'completed',
        txt:i%3===0?('Read analysis_'+i+'.py\\n'+'Sample output line\\n'.repeat(50)):(tid+' result '+i+'\\n'+'Convergence check passed. '.repeat(30))}));
      window.WebSocket=class {
        constructor(){this.readyState=1;}
        send(raw){const m=JSON.parse(raw);__sent.push(m);const parent=__parents[m.sessionId||m.id];if(!parent)return;
          let result;
          if(m.type==='attach')result={type:'attached',id:m.id,cc:'thread-'+m.id,cwd:'/demo/'+m.id,name:parent.name,
            title:parent.name,events:[],events_delta:m.after_seq!==undefined,event_seq:0,busy:false,ended:false,subagents:parent.agents};
          else if(m.type==='subagents_read')result={type:'subagents_snapshot',sessionId:m.sessionId,requestId:m.requestId,ok:true,subagents:parent.agents};
          else if(m.type==='subagent_read'){
            const agent=parent.agents.find(a=>a.threadId===m.threadId);
            result={type:'subagent_thread',sessionId:m.sessionId,requestId:m.requestId,threadId:m.threadId,ok:true,
              subagent:agent,readRevision:agent?.revision,messages:__messages(m.threadId)};
          }
          if(result){if(__holdParent===m.sessionId)__held.push(result);else setTimeout(()=>onMsg({data:JSON.stringify(result)}),15);}
        }
      };
    ''')
    for width in (390, 1100, 1600):
        await cdp.call('Emulation.setDeviceMetricsOverride', width=width, height=900, deviceScaleFactor=1, mobile=width == 390)
        await cdp.call('Page.navigate', url=url)
        await wait(cdp, "document.readyState==='complete'&&typeof refreshSubagentPanel==='function'")
        assert await cdp.js("$('#agentbtn').parentElement.tagName==='HEADER'&&!!$('#agentbtn svg')&&$('#agentN').hidden")
        assert await cdp.js("!$('#agentbtn').textContent.includes('Agent')")
        assert await cdp.js("getComputedStyle($('#terminalbtn')).display==='none'") == (width <= 860)
        await cdp.js('''
          __parents.A={name:'Benchmark implementation',agents:Array.from({length:10},(_,i)=>({
            threadId:'A-agent-'+i,label:['planner','implementation','reviewer','benchmark','tests','docs','memory','numerics','validation','report'][i],
            state:i===8?'completed':i===9?'idle':'running',active:i<8,createdAt:100+i,updatedAt:200+i,revision:1,
            prompt:['Check the finite-chain boundary conditions','Implement the folded contraction','Review the tensor truncation'][i%3],
            message:'Checking benchmark inputs and convergence.',
            currentTool:{itemId:'tool-'+i,name:i%2?'Bash':'Read',summary:i%2?'Bash python benchmark.py --size 24':'Read src/contraction.py',status:i<8?'inProgress':'completed'}}))};
          __parents.B={name:'Documentation review',agents:[{threadId:'B-agent',label:'documentation reviewer',state:'running',active:true,createdAt:1,revision:1,prompt:'Check the reference section'}]};
          sid='A';curCC='thread-A';cwd='/demo/A';ready=true;setBusy(false);setComposerEnabled(true);setCurname(__parents.A.name);
          liveSessions=[{id:'A',cc:'thread-A',cwd:'/demo/A',title:__parents.A.name},{id:'B',cc:'thread-B',cwd:'/demo/B',title:__parents.B.name}];
          ensureSessionTab(liveSessions[0],true);ensureSessionTab(liveSessions[1],false);
          mergeSubagents(__parents.A.agents,true);renderCtx({percentage:10,totalTokens:25840,maxTokens:258400,source:'codex'});openSubagents();
        ''')
        await wait(cdp, '!subagentSummaryPending')
        assert await cdp.js("!$('#agentN').hidden&&$('#agentN').textContent==='8'&&$('#agentbtn').getAttribute('aria-label').includes('8 active / 10 total')&&agentRows().length===8")
        bounds = await cdp.js("(()=>{const b=$('#agentbtn').getBoundingClientRect(),n=$('#agentN').getBoundingClientRect();return n.x+n.width/2>b.x+b.width/2&&n.y<b.y+b.height/2&&n.right<=innerWidth;})()")
        assert bounds, 'Count badge must stay at the robot icon upper right'
        assert await cdp.js("$('#agentList').querySelectorAll('.agrow').length===10&&$('#agentMeta').textContent==='8 active · 10 total'")
        assert await cdp.js("$('#agentPanel').parentElement.id==='shell'&&$('#agentDetail').hidden")
        if width >= 1100:
            assert await cdp.js("$('#agentPanel').classList.contains('docked')")
            assert await cdp.js("$('#mainCol').getBoundingClientRect().right<=$('#agentPanel').getBoundingClientRect().left+1")
        else:
            assert not await cdp.js("$('#agentPanel').classList.contains('docked')")
        before = await cdp.js("$('#agentPanel').getBoundingClientRect().top")
        await cdp.js('''
          for(let i=0;i<180;i++)addAsst('Main conversation result '+i);
          route({kind:'subagent_activity',subagent:__parents.A.agents[0]});
          ta.value='Continue the main conversation';ta.focus();ta.click();
        ''')
        assert await cdp.js('agentPanelOpen()&&stream.querySelectorAll(".aginline").length===0')
        assert abs(before - await cdp.js("$('#agentPanel').getBoundingClientRect().top")) < 1
        await cdp.js("$('#send').click()")
        assert await cdp.js("agentPanelOpen()&&__sent.filter(m=>m.type==='user').length===1&&__sent.find(m=>m.type==='user').text==='Continue the main conversation'")
        await cdp.js("window.__row=$('#agentList').children[2];__row.focus();__parents.A.agents[3]={...__parents.A.agents[3],state:'completed',active:false,revision:2,updatedAt:9999};mergeSubagents([__parents.A.agents[3]])")
        assert await cdp.js("$('#agentList').children[2]===__row&&document.activeElement===__row&&$('#agentMeta').textContent==='7 active · 10 total'")
        assert await cdp.js("$('#agentN').textContent==='7'&&agentRows().length===7"), 'Completing a child immediately decrements both web and Android 0.6.5 badges'
        await cdp.js("$('#agentFilterActive').click()")
        assert await cdp.js("$('#agentList').children.length===7")
        await cdp.js("$('#agentFilterAll').click();$('#agentList').children[0].click()")
        await wait(cdp, "$('#agentMessages').textContent.includes('A-agent-0 result')")
        await cdp.js("$('#agentList').children[1].click()")
        await wait(cdp, "$('#agentMessages').textContent.includes('A-agent-1 result')")
        assert await cdp.js("agentPanelOpen()&&subagentCurrent==='A-agent-1'&&$('#agentDetailTitle').textContent==='implementation'")
        await cdp.js("$('#agentList').children[0].click()")
        await wait(cdp, "$('#agentMessages').dataset.tid==='A-agent-0'")
        await cdp.js("$('#agentMessages details').open=true;$('#agentMessages details pre').scrollTop=100;$('#agentMessages').scrollTop=180;saveSubagentScroll()")
        await cdp.js("window.__originalMessages=__messages;__messages=tid=>[...__originalMessages(tid),{itemId:'extra',role:'assistant',txt:'A new progress update'}];loadSubagentThread(subagentCurrent,true)")
        await wait(cdp, "!subagentPending.has('A-agent-0')")
        assert await cdp.js("$('#agentMessages details').open&&$('#agentMessages details pre').scrollTop>=99&&Math.abs($('#agentMessages').scrollTop-180)<=1")
        await cdp.js("__holdParent='A';refreshSubagentPanel(true)")
        await wait(cdp, '__held.length>0')
        await cdp.js("ta.value='Parent A draft';saveDraft();switchSession('B')")
        await wait(cdp, "sid==='B'&&$('#agentList').textContent.includes('documentation reviewer')")
        assert await cdp.js("agentPanelOpen()&&!$('#agentList').textContent.includes('planner')&&$('#agentSession').textContent==='Documentation review'")
        assert await cdp.js("$('#agentN').textContent==='1'&&!$('#agentN').hidden")
        await cdp.js("const held=__held.splice(0);__holdParent='';held.forEach(m=>onMsg({data:JSON.stringify(m)}));ta.value='Parent B draft';saveDraft()")
        assert await cdp.js("$('#agentList').children.length===1&&!$('#agentMessages').textContent.includes('A-agent')")
        await cdp.js("switchSession('A')")
        await wait(cdp, "sid==='A'&&subagentCurrent==='A-agent-0'&&$('#agentMessages').textContent.includes('A-agent-0 result')&&!subagentSummaryPending")
        assert await cdp.js("ta.value==='Parent A draft'&&subagentDetailOpen&&Math.abs($('#agentMessages').scrollTop-180)<=1")
        assert await cdp.js("$('#agentN').textContent==='7'")
        await cdp.js("window.__readsBefore=__sent.filter(m=>['subagent_read','subagents_read'].includes(m.type)).length;closeSubagents();refreshSubagentPanel(true)")
        assert await cdp.js("__sent.filter(m=>['subagent_read','subagents_read'].includes(m.type)).length===__readsBefore")
        await cdp.js("openSubagents();$('#agentOverview').click();__parents.A.agents.forEach(a=>{a.state='completed';a.active=false;a.revision=10});mergeSubagents(__parents.A.agents)")
        await asyncio.sleep(.05)
        assert await cdp.js("!subagentDetailOpen&&$('#agentDetail').hidden"), 'An attach reply cannot undo a newer overview selection'
        assert await cdp.js("$('#agentMeta').textContent==='0 active · 10 total'&&agentPanelOpen()")
        assert await cdp.js("$('#agentN').hidden&&agentRows().length===0&&$('#agentList').children.length===10&&!$('#agentbtn').classList.contains('on')"), 'Inactive children remain in the overview but never keep the badge visible'
        await cdp.js("mergeSubagents([{threadId:'A-agent-0',state:'running',active:true,revision:1}])")
        assert await cdp.js("!agentActive(subagents['A-agent-0'])")
        await cdp.js("__parents.A.agents.forEach((a,i)=>{a.state=i<7?'running':'completed';a.active=i<7;a.revision=11;if(a.currentTool)a.currentTool.status=i<7?'inProgress':'completed'});mergeSubagents(__parents.A.agents)")
        if width >= 1100:
            await cdp.js("$('#agresize').focus()")
            old_width = await cdp.js("$('#agentPanel').getBoundingClientRect().width")
            await cdp.call('Input.dispatchKeyEvent', type='keyDown', key='ArrowRight', code='ArrowRight', windowsVirtualKeyCode=39)
            await cdp.call('Input.dispatchKeyEvent', type='keyUp', key='ArrowRight', code='ArrowRight', windowsVirtualKeyCode=39)
            assert await cdp.js("$('#agentPanel').getBoundingClientRect().width") <= old_width
        await cdp.js("document.activeElement.blur();$('#agentList').scrollTop=0")
        await cdp.js("renderCtx({percentage:10,totalTokens:25840,maxTokens:258400,source:'codex'})")
        assert await cdp.js("(()=>{const h=document.querySelector('header').getBoundingClientRect(),b=$('#agentbtn').getBoundingClientRect(),c=$('#ctx').getBoundingClientRect();return b.top>=h.top&&b.bottom<=h.bottom&&c.right<=b.left&&b.right<=innerWidth;})()"), 'Context and robot button must not overlap'
        for theme in ('light', 'dark'):
            await cdp.js("document.documentElement.setAttribute('data-theme','" + theme + "')")
            await asyncio.sleep(.1)
            bounds = await cdp.js("(()=>{const p=$('#agentPanel').getBoundingClientRect(),b=$('#agentClose').getBoundingClientRect();return {fits:p.x>=0&&p.right<=innerWidth+1&&p.bottom<=innerHeight+1,close:b.right<=p.right&&b.x>=p.x,overflow:document.documentElement.scrollWidth>innerWidth};})()")
            assert bounds == {'fits': True, 'close': True, 'overflow': False}, bounds
            shot = await cdp.call('Page.captureScreenshot', format='png')
            (output / f'subagents-{theme}-{width}.png').write_bytes(base64.b64decode(shot['data']))
        assert not await cdp.js("__sent.some(m=>['start','interrupt','end','turn/start','thread/resume'].includes(m.type))")
        assert await cdp.js("__sent.filter(m=>m.type==='user').length") == 1
        assert await cdp.js('__errors') == []
        await cdp.js("mergeSubagents([],true)")
        assert await cdp.js("$('#agentN').hidden&&$('#agentbtn svg')&&$('#agentbtn').getAttribute('aria-label')==='Subagents'")
        await cdp.js("mergeSubagents(Array.from({length:110},(_,i)=>({threadId:'many-'+i,state:'running',active:true})),true)")
        assert await cdp.js("$('#agentN').textContent==='99+'&&$('#agentbtn').getAttribute('aria-label').includes('110 total')")
        await cdp.js("mergeSubagents(['completed','idle','notLoaded','systemError','errored','interrupted','shutdown','notFound'].map((state,i)=>({threadId:'many-'+i,state,active:true})))")
        assert await cdp.js("$('#agentN').dataset.activeCount==='102'&&agentRows().length===102"), 'Terminal/idle states override a stale active flag'
        await cdp.js("mergeSubagents([{threadId:'many-8',state:'running',active:false}])")
        assert await cdp.js("$('#agentN').dataset.activeCount==='101'&&agentRows().length===101"), 'An explicit inactive flag must also clear a running child'
        print(f'{width}px: pinned 10-agent overview, tools, stable rows, detail/scroll updates, session isolation, stale replies and responsive panel PASS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path(tempfile.gettempdir()) / 'subagent-dock-check')
    asyncio.run(run_browser(parser.parse_args().output_dir, verify_page=verify, page_handler=SubagentPage))
