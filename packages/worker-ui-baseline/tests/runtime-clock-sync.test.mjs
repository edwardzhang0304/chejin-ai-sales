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
test('settings and warning call explicit sync; busy prevents duplicate clicks and no automatic acceptance',async()=>{
 const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost/'});
 const keys=['window','document','HTMLElement','MessageChannel','IS_REACT_ACT_ENVIRONMENT','ResizeObserver'];
 const old=new Map(keys.map(k=>[k,Object.getOwnPropertyDescriptor(globalThis,k)]));const channels=[];
 class TestChannel extends MessageChannel{constructor(){super();channels.push(this);}}
 Object.assign(globalThis,{window:dom.window,document:dom.window.document,HTMLElement:dom.window.HTMLElement,MessageChannel:TestChannel,IS_REACT_ACT_ENVIRONMENT:true,ResizeObserver:class{observe(){}disconnect(){}}});
 const dir=mkdtempSync(resolve(tmpdir(),'chejin-clock-sync-dom-'));
 try{
  const output=resolve(dir,'render.mjs');
  buildSync({stdin:{contents:`import React,{act} from 'react';import{createRoot}from'react-dom/client';import{WorkerClientBaseline}from'./WorkerClientBaseline';import{workerClientMock}from'./mockData';const root=createRoot(document.getElementById('root'));export let syncs=0,starts=0;export async function render(busy=false,state='idle',screen='settings',notice='电脑时间有偏差'){const model={...workerClientMock,clockSync:{busy,state,message:state==='verified'?'电脑时间已核验':''},startupCheck:{state:'passed',title:'',message:'',canStart:true,clockCheck:{notice,verification:{status:"verified",message:"当前电脑时间已核验"}}},update:{}};await act(async()=>root.render(<WorkerClientBaseline screen={screen} model={model} onSyncComputerTime={()=>syncs++} onStartAccepting={()=>starts++}/>));}export async function click(button){await act(async()=>button.click());}export async function dispose(){await act(async()=>root.unmount());}`,resolveDir:fileURLToPath(new URL('../src/',import.meta.url)),loader:'tsx'},bundle:true,platform:'browser',format:'esm',outfile:output,nodePaths:[fileURLToPath(new URL('../../../frontend/node_modules/',import.meta.url))],logLevel:'silent'});
  const view=await import(pathToFileURL(output));await view.render();
  let button=[...document.querySelectorAll('.cw-settings-list button')].find(b=>b.textContent==='同步电脑时间');
  assert(button);await view.click(button);assert.equal(view.syncs,1);
  await view.render(true,'repairing');
  button=[...document.querySelectorAll('button')].find(b=>b.textContent==='同步中');
  assert(button.disabled);await view.click(button);assert.equal(view.syncs,1);
  await view.render(false,'idle','paused-empty');
  button=document.querySelector('.cw-clock-warning button');assert(button);
  await view.click(button);assert.equal(view.syncs,2);
  await view.render(false,'verified');assert.equal(view.starts,0);
  for(const notice of ['电脑时间有偏差','请设置北京时间','后台授时异常','暂时无法核验电脑时间']) {
   await view.render(false,'verified','paused-empty',notice);
   assert(document.querySelector('.cw-clock-warning').textContent.includes(notice));
   assert(!document.querySelector('.cw-clock-warning').textContent.includes('电脑时间已核验'));
   assert(document.querySelector('.cw-clock-warning button'));
  }
  await view.render(false,'verified','paused-empty','');
  assert(!document.querySelector('.cw-clock-warning button'));
  assert(document.querySelector('.cw-clock-warning').textContent.includes('电脑时间已核验'));
  assert.equal(view.starts,0);
  await view.dispose();
 }finally{for(const c of channels){c.port1.close();c.port2.close();}dom.window.close();for(const key of keys){const descriptor=old.get(key);if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}rmSync(dir,{recursive:true,force:true});}
});
