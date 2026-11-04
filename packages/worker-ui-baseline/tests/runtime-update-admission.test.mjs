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
test('update admission disables click, explains the blocker, and re-enables after settlement',async()=>{
 const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost/'});
 const keys=['window','document','HTMLElement','MessageChannel','IS_REACT_ACT_ENVIRONMENT','ResizeObserver'];
 const old=new Map(keys.map(k=>[k,Object.getOwnPropertyDescriptor(globalThis,k)]));const channels=[];
 class TestChannel extends MessageChannel{constructor(){super();channels.push(this);}}
 Object.assign(globalThis,{window:dom.window,document:dom.window.document,HTMLElement:dom.window.HTMLElement,MessageChannel:TestChannel,IS_REACT_ACT_ENVIRONMENT:true,ResizeObserver:class{observe(){}disconnect(){}}});
 const dir=mkdtempSync(resolve(tmpdir(),'chejin-update-admission-dom-'));
 try{
  const output=resolve(dir,'render.mjs');
  buildSync({stdin:{contents:`import React,{act} from 'react';import{createRoot}from'react-dom/client';import{WorkerClientBaseline}from'./WorkerClientBaseline';import{workerClientMock}from'./mockData';const root=createRoot(document.getElementById('root'));export let checks=0,starts=0;export async function render(update={},busy=false){const model={...workerClientMock,clockSync:{busy,state:'idle',message:''},update};await act(async()=>root.render(<WorkerClientBaseline screen="settings" model={model} onCheckForUpdates={()=>checks++} onStartAccepting={()=>starts++}/>));}export async function click(button){await act(async()=>button.click());}export async function dispose(){await act(async()=>root.unmount());}`,resolveDir:fileURLToPath(new URL('../src/',import.meta.url)),loader:'tsx'},bundle:true,platform:'browser',format:'esm',outfile:output,nodePaths:[fileURLToPath(new URL('../../../frontend/node_modules/',import.meta.url))],logLevel:'silent'});
  const view=await import(pathToFileURL(output));
  const button=()=>document.querySelector('.cw-version-settings-row button');
  await view.render();assert(button().disabled);await view.click(button());assert.equal(view.checks,0);
  for(const reason of ['微信接口仍在执行或结果未确认，请先完成恢复核对','发送结果尚未回传确认，正在等待结算','正在核对微信连接，请稍后检查更新']){
   await view.render({check_allowed:false,check_reason_text:reason,status_text:reason});
   assert(button().disabled);assert.equal(button().title,reason);
   assert.equal(document.querySelector('[role="status"]').textContent,reason);
   await view.click(button());assert.equal(view.checks,0);
  }
  await view.render({check_allowed:true,status_text:''});assert(!button().disabled);
  await view.click(button());assert.equal(view.checks,1);
  await view.render({check_allowed:false,in_progress:true,status_text:'正在下载 30%'});
  assert(button().disabled);assert.equal(button().textContent,'更新中');
  assert(document.querySelector('[role="status"]').textContent.includes('30%'));
  await view.render({check_allowed:true},true);assert(button().disabled);
  assert.equal(view.starts,0);await view.dispose();
 }finally{for(const c of channels){c.port1.close();c.port2.close();}dom.window.close();for(const key of keys){const descriptor=old.get(key);if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}rmSync(dir,{recursive:true,force:true});}
});
