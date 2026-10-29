// Invoked by the Python lifecycle test with real diagnostic/Qt projections.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const require=createRequire(new URL('../../../frontend/package.json',import.meta.url));
const {JSDOM}=require('jsdom');
const observed=JSON.parse(readFileSync(process.argv[2],'utf8'));
const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost',runScripts:'outside-only'});
const w=dom.window;let update,revision=0,actions=0;
const model={workerId:'synthetic',workerToken:'',version:'test',schedule:{enabled:false,start:'09:00',end:'21:00'},status:{sellerName:'测试',receiveState:'暂停接单',connectionState:'连接正常',lastHeartbeat:'暂无',automationState:'可用',wechatState:'已连接'},task:{id:'-',title:'暂无任务',statusText:'',customerName:'',phone:'',sellerName:'',noteCode:''},runningSteps:[],completedSteps:[],failedSteps:[],logs:[],update:{},...observed[0]};
const payload=()=>JSON.stringify({revision:++revision,screen:'settings',model});
const bridge={initialState:cb=>cb(payload()),stateChanged:{connect:cb=>update=cb},syncComputerTime:()=>actions++,startAccepting:()=>actions++};
w.qt={webChannelTransport:{}};w.QWebChannel=function(_,cb){cb({objects:{chejinBridge:bridge}})};
w.ResizeObserver=class{observe(){}disconnect(){}};
async function until(fn){for(let i=0;i<100;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}assert(fn());}
try {
 w.eval(readFileSync(new URL('../../../worker-client/chejin_worker_client/web_assets/worker-web-app.js',import.meta.url),'utf8'));
 await until(()=>update);
 for(const item of observed){
  Object.assign(model,item);update(payload());
  const current=item.startupCheck.clockCheck.verification;
  await until(()=>w.document.querySelector('.cw-clock-warning')?.textContent.includes(current.message));
  const visible=w.document.querySelector('.cw-clock-warning').textContent;
  if(current.status!=='verified')assert(!visible.includes('已核验'),item.case);
  assert(!w.document.querySelector('.cw-clock-warning button'),item.case);
 }
 assert.equal(actions,0);console.log(JSON.stringify({cases:observed.map(x=>x.case),actions}));
}finally{w.close();}
