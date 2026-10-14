import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL,fileURLToPath} from 'node:url';
import {MessageChannel} from 'node:worker_threads';
const require=createRequire(process.env.CHEJIN_UI_DEPS_PACKAGE || new URL('../../../frontend/package.json',import.meta.url));
const domRequire=process.env.CHEJIN_UI_DOM_PACKAGE ? createRequire(process.env.CHEJIN_UI_DOM_PACKAGE) : require;
const {JSDOM}=domRequire('jsdom');const {buildSync}=require('esbuild');
const states=process.env.CHEJIN_STARTUP_UI_STATES ? JSON.parse(readFileSync(process.env.CHEJIN_STARTUP_UI_STATES,'utf8')) : [];
if(!states.length)test('startup DOM requires actual Worker state artifacts',{skip:true},()=>{});
for(const [index,state] of states.entries())test(`actual startup state ${index}: central reason and Start gate`,async()=>{
 const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost/'});
 const keys=['window','document','HTMLElement','MessageChannel','IS_REACT_ACT_ENVIRONMENT','__startupState'];
 const old=new Map(keys.map(k=>[k,Object.getOwnPropertyDescriptor(globalThis,k)]));const channels=[];
 class TestChannel extends MessageChannel{constructor(){super();channels.push(this);}}
 Object.assign(globalThis,{window:dom.window,document:dom.window.document,HTMLElement:dom.window.HTMLElement,MessageChannel:TestChannel,IS_REACT_ACT_ENVIRONMENT:true,__startupState:state});
 const dir=mkdtempSync(resolve(tmpdir(),'chejin-startup-dom-'));
 try{
  const output=resolve(dir,'render.mjs');
  buildSync({stdin:{contents:`import React,{act} from 'react';import{createRoot}from'react-dom/client';import{WorkerClientBaseline}from'./WorkerClientBaseline';const root=createRoot(document.getElementById('root'));export let starts=0;export async function render(){await act(async()=>root.render(<WorkerClientBaseline screen={globalThis.__startupState.screen} model={globalThis.__startupState.model} onStartAccepting={()=>starts++}/>));}export async function click(button){await act(async()=>button.click());}export async function dispose(){await act(async()=>root.unmount());}`,resolveDir:fileURLToPath(new URL('../src/',import.meta.url)),loader:'tsx'},bundle:true,platform:'browser',format:'esm',outfile:output,nodePaths:[process.env.CHEJIN_UI_NODE_MODULES || fileURLToPath(new URL('../../../frontend/node_modules/',import.meta.url))],logLevel:'silent'});
  const view=await import(pathToFileURL(output));await view.render();
  const text=document.body.textContent;
  assert.equal(document.querySelector('.cw-status-bottom'),null);assert.equal(document.querySelector('.cw-status-item'),null);
  assert(text.includes(state.model.startupCheck.title));
  if(state.model.startupCheck.message)assert(text.includes(state.model.startupCheck.message));
  assert(document.querySelector('.cw-process-empty[role="status"][aria-live="polite"]'));
  const button=[...document.querySelectorAll('button')].find(b=>b.textContent==='开始接单');assert(button);
  assert.equal(button.disabled,!state.model.startupCheck.canStart);
  await view.click(button);assert.equal(view.starts,state.model.startupCheck.canStart?1:0);
  await view.dispose();
 }finally{for(const c of channels){c.port1.close();c.port2.close();}dom.window.close();for(const key of keys){const descriptor=old.get(key);if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}rmSync(dir,{recursive:true,force:true});}
});
