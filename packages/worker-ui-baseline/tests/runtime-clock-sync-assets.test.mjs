import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const require=createRequire(new URL('../../../frontend/package.json',import.meta.url));
const {JSDOM}=require('jsdom');
const delay=()=>new Promise(resolve=>setTimeout(resolve,10));
async function until(fn){for(let i=0;i<100;i++){if(fn())return;await delay();}assert(fn());}
test('shipped asset retains live warning after prior sync and invokes only explicit repair',async()=>{
 const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost',runScripts:'outside-only'});
 const w=dom.window;let update,revision=0,syncs=0,starts=0;
 const model={workerId:'synthetic',workerToken:'',version:'test',schedule:{enabled:false,start:'09:00',end:'21:00'},status:{sellerName:'测试',receiveState:'暂停接单',connectionState:'连接正常',lastHeartbeat:'暂无',automationState:'可用',wechatState:'已连接'},task:{id:'-',title:'暂无任务',statusText:'',customerName:'',phone:'',sellerName:'',noteCode:''},runningSteps:[],completedSteps:[],failedSteps:[],logs:[],update:{},clockSync:{state:'verified',busy:false,message:'电脑时间已核验'},startupCheck:{state:'passed',canStart:true,clockCheck:{notice:'',verification:{status:'verified',message:'当前电脑时间已核验'}}}};
 const payload=()=>JSON.stringify({revision:++revision,screen:'paused-empty',model});
 const bridge={initialState:cb=>cb(payload()),stateChanged:{connect:cb=>update=cb},syncComputerTime:()=>syncs++,startAccepting:()=>starts++};
 w.qt={webChannelTransport:{}};w.QWebChannel=function(_,cb){cb({objects:{chejinBridge:bridge}})};
 w.ResizeObserver=class{observe(){}disconnect(){}};
 try {
  w.eval(readFileSync(new URL('../../../worker-client/chejin_worker_client/web_assets/worker-web-app.js',import.meta.url),'utf8'));
  await until(()=>w.document.querySelector('.cw-clock-warning')?.textContent.includes('电脑时间已核验'));
  for(const [notice,required] of [['电脑时间有偏差',true],['请设置北京时间',true],['后台授时异常',false],['暂时无法核验电脑时间',false]]){
   Object.assign(model.startupCheck.clockCheck,{notice,repairRequired:required});update(payload());
   await until(()=>w.document.querySelector('.cw-clock-warning')?.textContent.includes(notice));
   assert(!w.document.querySelector('.cw-clock-warning').textContent.includes('电脑时间已核验'));
   assert(!w.document.querySelector('.cw-clock-warning button'));
   const button=w.document.querySelector('.cw-dock button');assert(button);
   assert.equal(button.textContent,required?'同步电脑时间':'开始接单');
   if(required)button.click();
  }
  assert.equal(syncs,2);assert.equal(starts,0);
  model.clockSync={state:'repairing',busy:true,message:'正在同步北京时间'};update(payload());
  await until(()=>w.document.querySelector('.cw-clock-warning')?.textContent.includes('正在同步北京时间'));
  assert(!w.document.querySelector('.cw-clock-warning button'));
  const button=w.document.querySelector('.cw-dock button');assert.equal(button.textContent,'同步中');assert(button.disabled);
  button.click();assert.equal(syncs,2);assert.equal(starts,0);
 }finally{w.close();}
});
