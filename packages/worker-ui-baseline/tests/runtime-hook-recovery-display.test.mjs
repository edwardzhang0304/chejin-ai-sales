import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {MessageChannel} from 'node:worker_threads';
const require=createRequire(new URL('../../../frontend/package.json',import.meta.url));
const {JSDOM}=require('jsdom');const {buildSync}=require('esbuild');
const states=process.env.CHEJIN_HOOK_RECOVERY_STATES ? JSON.parse(readFileSync(process.env.CHEJIN_HOOK_RECOVERY_STATES,'utf8')) : {};
if(!Object.keys(states).length)test('Hook recovery requires actual Worker model artifacts',{skip:true},()=>{});
for(const [kind,state] of Object.entries(states))test(`Hook recovery display: ${kind}`,async()=>{
 const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost/'});
 const keys=['window','document','HTMLElement','MessageChannel','IS_REACT_ACT_ENVIRONMENT','ResizeObserver'];
 const old=new Map(keys.map(k=>[k,Object.getOwnPropertyDescriptor(globalThis,k)]));const channels=[];
 class TestChannel extends MessageChannel{constructor(){super();channels.push(this);}}
 Object.assign(globalThis,{window:dom.window,document:dom.window.document,HTMLElement:dom.window.HTMLElement,MessageChannel:TestChannel,IS_REACT_ACT_ENVIRONMENT:true,ResizeObserver:class{observe(){}disconnect(){}}});
 const dir=mkdtempSync(resolve(tmpdir(),'chejin-hook-display-'));
 try{
  const output=resolve(dir,'render.mjs');
  buildSync({stdin:{contents:`import React,{act} from 'react';import{createRoot}from'react-dom/client';import{WorkerClientBaseline}from'./WorkerClientBaseline';const root=createRoot(document.getElementById('root'));export let starts=0;export async function render(state){await act(async()=>root.render(<WorkerClientBaseline screen={state.screen} model={state.model} onStartAccepting={()=>starts++}/>));}export async function click(button){await act(async()=>button.click());}export async function dispose(){await act(async()=>root.unmount());}`,resolveDir:fileURLToPath(new URL('../src/',import.meta.url)),loader:'tsx'},bundle:true,platform:'browser',format:'esm',outfile:output,nodePaths:[fileURLToPath(new URL('../../../frontend/node_modules/',import.meta.url))],logLevel:'silent'});
  const view=await import(pathToFileURL(output));
  // Old producer data must not revive the removed UI when upgrading.
  const input=structuredClone(state);input.model.hookHistorySteps=[{state:'done',title:'历史人工交接'}];
  await view.render(input);
  assert(!document.body.textContent.includes('历史人工交接'));
  assert(document.body.textContent.includes('已转人工，等待销售处理'),'current handoff remains visible');
  assert.equal(view.starts,0,'rendering never starts work');
  const button=document.querySelector('.cw-dock button');
  const titles=[...document.querySelectorAll('.cw-step-list li strong')].map(x=>x.textContent);
  assert.equal(button.disabled,kind==='blocked'||kind==='checking');
  if(kind==='ready'){
   assert(document.body.textContent.includes('可以恢复接单'));
   assert(!titles.includes('已停止接单'));
   assert(titles.includes('检查已通过，可以恢复接单'));
   assert.equal(document.querySelectorAll('.cw-step-list li.error').length,0);
   await view.click(button);assert.equal(view.starts,1);
  }else if(kind==='checking'){
   assert(!titles.includes('已停止接单'));assert(titles.includes('正在恢复接单'));
   await view.click(button);assert.equal(view.starts,0);
  }else if(kind==='blocked'){
   assert(titles.includes('已停止接单'));assert(!titles.includes('检查已通过，可以恢复接单'));
   await view.click(button);assert.equal(view.starts,0);
  }else{
   assert(!titles.includes('检查已通过，可以恢复接单'));
   assert.equal(button.textContent,kind==='running'?'暂停接单':'开始接单');
  }
  await view.dispose();
 }finally{for(const c of channels){c.port1.close();c.port2.close();}dom.window.close();for(const key of keys){const descriptor=old.get(key);if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}rmSync(dir,{recursive:true,force:true});}
});
