const { spawn } = require('child_process');
const WebSocket = require('ws');
const fs = require('fs');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT = 9702;
function httpGet(u){return new Promise((res,rej)=>{require('http').get(u,r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>res(JSON.parse(d)));}).on('error',rej);});}
(async()=>{
  const ch = spawn(CHROME,['--headless=new','--no-sandbox','--disable-gpu','--hide-scrollbars','--remote-debugging-port='+PORT,'--user-data-dir=/tmp/cdp-foot2','about:blank'],{stdio:'ignore'});
  await new Promise(r=>setTimeout(r,2200));
  const list = await httpGet('http://127.0.0.1:'+PORT+'/json/list');
  const page = list.find(t=>t.type==='page');
  const ws = new WebSocket(page.webSocketDebuggerUrl,{perMessageDeflate:false});
  let id=0; const pend=new Map(); const errs=[];
  ws.on('message',m=>{const o=JSON.parse(m); if(o.id&&pend.has(o.id)){pend.get(o.id)(o);pend.delete(o.id);}
    if(o.method==='Runtime.exceptionThrown') errs.push(o.params.exceptionDetails.text);});
  const send=(m,q)=>new Promise(r=>{const i=++id;pend.set(i,r);ws.send(JSON.stringify({id:i,method:m,params:q||{}}));});
  const ev=async e=>{const o=await send('Runtime.evaluate',{expression:e,returnByValue:true});return o.result.result&&o.result.result.value;};
  await new Promise(r=>ws.on('open',r));
  await send('Runtime.enable'); await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:2,mobile:false});
  await send('Page.navigate',{url:'http://127.0.0.1:8772/index.html#us'});
  await new Promise(r=>setTimeout(r,2500));
  console.log('脚注：'+await ev("JSON.stringify([...document.querySelectorAll('.us-foot')].map(e=>e.textContent.trim()))"));
  const box = JSON.parse(await ev("(()=>{const f=document.querySelectorAll('.us-foot');const r=f[0].getBoundingClientRect();return JSON.stringify({x:0,y:Math.round(r.top+scrollY)-40,w:1440,h:120});})()"));
  const shot = await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:box});
  fs.writeFileSync('/tmp/foot.png', Buffer.from(shot.result.data,'base64'));
  console.log('异常：'+(errs.length?errs.join(' | '):'无'));
  ws.close(); ch.kill('SIGKILL'); process.exit(0);
})().catch(e=>{console.error(e);process.exit(1);});
