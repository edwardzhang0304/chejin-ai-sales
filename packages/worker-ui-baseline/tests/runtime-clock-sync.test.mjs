import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {MessageChannel} from 'node:worker_threads';
const require=createRequire(new URL('../../../frontend/package.json',import.meta.url));
const {JSDOM}=require('jsdom');const {buildSync}=require('esbuild');
test('one bottom repair action replaces accepting only for confirmed local faults; recovery remains explicit',async()=>{
 const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost/'});
 const keys=['window','document','HTMLElement','MessageChannel','IS_REACT_ACT_ENVIRONMENT','ResizeObserver'];
 const old=new Map(keys.map(k=>[k,Object.getOwnPropertyDescriptor(globalThis,k)]));const channels=[];
 class TestChannel extends MessageChannel{constructor(){super();channels.push(this);}}
 Object.assign(globalThis,{window:dom.window,document:dom.window.document,HTMLElement:dom.window.HTMLElement,MessageChannel:TestChannel,IS_REACT_ACT_ENVIRONMENT:true,ResizeObserver:class{observe(){}disconnect(){}}});
 const dir=mkdtempSync(resolve(tmpdir(),'chejin-clock-sync-dom-'));
 try{
  const output=resolve(dir,'render.mjs');
  buildSync({stdin:{contents:`import React,{act} from 'react';import{createRoot}from'react-dom/client';import{WorkerClientBaseline}from'./WorkerClientBaseline';import{workerClientMock}from'./mockData';const root=createRoot(document.getElementById('root'));export let syncs=0,starts=0;export async function render({busy=false,state='idle',screen='paused-empty',notice='',repairRequired=false,retryRequired=false,canStart=true,canRecheck=false,accepting=false,updating=false,waterfall=false}={}){const model={...workerClientMock,status:{...workerClientMock.status,receiveState:accepting?'接单中':'暂停接单',acceptingIntent:accepting},hookProcessSteps:waterfall?[{title:'检查运行环境',state:'done'}]:undefined,clockSync:{busy,state,retryRequired,message:state==='verified'?'电脑时间已核验':''},startupCheck:{state:canStart?'passed':'failed',title:'',message:'',canStart,canRecheck,clockCheck:{notice,repairRequired,verification:{status:repairRequired?'warning':'verified',message:'当前电脑时间已核验'}}},update:{in_progress:updating}};await act(async()=>root.render(<WorkerClientBaseline screen={screen} model={model} onSyncComputerTime={()=>syncs++} onStartAccepting={()=>starts++}/>));}export async function click(button){await act(async()=>button.click());}export async function dispose(){await act(async()=>root.unmount());}`,resolveDir:fileURLToPath(new URL('../src/',import.meta.url)),loader:'tsx'},bundle:true,platform:'browser',format:'esm',outfile:output,nodePaths:[fileURLToPath(new URL('../../../frontend/node_modules/',import.meta.url))],logLevel:'silent'});
  const view=await import(pathToFileURL(output));
  const dock=()=>document.querySelector('.cw-dock button');
  const repairButtons=()=>[...document.querySelectorAll('button')].filter(b=>b.textContent==='同步电脑时间');
  for(const screen of ['settings','schedule-settings','bind']) {
   await view.render({screen,repairRequired:true,notice:'请设置北京时间'});
   assert.equal(repairButtons().length,0);
   assert(!document.querySelector('.cw-clock-warning button'));
  }
  for(const waterfall of [false,true]) {
   await view.render({waterfall});assert.equal(dock().textContent,'开始接单');
   await view.render({waterfall,screen:'accepting-wait',accepting:true});assert.equal(dock().textContent,'暂停接单');
   for(const screen of ['paused-empty','client-faulted','running','offline','wechat-disconnected']) {
    await view.render({screen,waterfall,repairRequired:true,canStart:false,notice:'请同步电脑时间'});
    assert.equal(repairButtons().length,1);assert.equal(dock(),repairButtons()[0]);assert(!dock().disabled);
   }
  }
  await view.click(dock());assert.equal(view.syncs,1);assert.equal(view.starts,0);
  await view.render({busy:true,state:'repairing',canStart:false});
  assert.equal(dock().textContent,'同步中');assert(dock().disabled);
  await view.click(dock());assert.equal(view.syncs,1);
  await view.render({repairRequired:true,updating:true});assert(dock().disabled);
  await view.render({state:'cancelled',repairRequired:true,canStart:false});assert(!dock().disabled);
  await view.render({state:'stop_failed',retryRequired:true,canStart:false});
  assert.equal(dock().textContent,'同步电脑时间');assert(!dock().disabled);
  for(const notice of ['后台授时异常','暂时无法核验电脑时间','时间源超时']) {
   await view.render({state:'verified',notice,canStart:false,waterfall:true});
   assert(document.querySelector('.cw-clock-warning').textContent.includes(notice));
   assert.equal(repairButtons().length,0);assert(dock().disabled);
  }
  await view.render({canRecheck:true,canStart:false,waterfall:true});
  assert.equal(dock().textContent,'重新检查连接');assert(!dock().disabled);
  await view.render({state:'verified'});
  assert.equal(dock().textContent,'开始接单');assert(!dock().disabled);assert.equal(view.starts,0);
  await view.click(dock());assert.equal(view.starts,1);assert.equal(view.syncs,1);
  await view.dispose();
 }finally{for(const c of channels){c.port1.close();c.port2.close();}dom.window.close();for(const key of keys){const descriptor=old.get(key);if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}rmSync(dir,{recursive:true,force:true});}
});
