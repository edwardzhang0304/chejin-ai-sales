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
const states=process.env.CHEJIN_WATERFALL_UI_STATES ? JSON.parse(readFileSync(process.env.CHEJIN_WATERFALL_UI_STATES,'utf8')) : [];
if(!states.length)test('waterfall DOM requires actual Worker state artifacts',{skip:true},()=>{});
for(const [index,state] of states.entries())test(`actual waterfall state ${index}: preserved phases and controls`,async()=>{
 const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost/'});
 const keys=['window','document','HTMLElement','MessageChannel','IS_REACT_ACT_ENVIRONMENT','__startupState','ResizeObserver'];
 const old=new Map(keys.map(k=>[k,Object.getOwnPropertyDescriptor(globalThis,k)]));const channels=[];
 class TestChannel extends MessageChannel{constructor(){super();channels.push(this);}}
 Object.assign(globalThis,{window:dom.window,document:dom.window.document,HTMLElement:dom.window.HTMLElement,MessageChannel:TestChannel,IS_REACT_ACT_ENVIRONMENT:true,__startupState:state,ResizeObserver:class{observe(){}disconnect(){}}});
 const dir=mkdtempSync(resolve(tmpdir(),'chejin-startup-dom-'));
 try{
  const output=resolve(dir,'render.mjs');
  buildSync({stdin:{contents:`import React,{act} from 'react';import{createRoot}from'react-dom/client';import{WorkerClientBaseline}from'./WorkerClientBaseline';const root=createRoot(document.getElementById('root'));export let starts=0;export async function render(){await act(async()=>root.render(<WorkerClientBaseline screen={globalThis.__startupState.screen} model={globalThis.__startupState.model} onStartAccepting={()=>starts++}/>));}export async function click(button){await act(async()=>button.click());}export async function scroll(viewport,top){await act(async()=>{viewport.scrollTop=top;viewport.dispatchEvent(new window.Event('scroll'));});}export async function dispose(){await act(async()=>root.unmount());}`,resolveDir:fileURLToPath(new URL('../src/',import.meta.url)),loader:'tsx'},bundle:true,platform:'browser',format:'esm',outfile:output,nodePaths:[process.env.CHEJIN_UI_NODE_MODULES || fileURLToPath(new URL('../../../frontend/node_modules/',import.meta.url))],logLevel:'silent'});
  const view=await import(pathToFileURL(output));await view.render();
  const text=document.body.textContent;
  assert.equal(document.querySelector('.cw-status-bottom'),null);assert.equal(document.querySelector('.cw-status-item'),null);
  assert(document.querySelector('[data-screen-view="hook-waterfall"]'));
  const rows=[...document.querySelectorAll('.cw-step-list li strong')].map(x=>x.textContent);
  assert.deepEqual(rows,[...state.model.hookProcessSteps.map(x=>x.title),...(state.model.startupCheck.state==='failed'?[state.model.startupCheck.title]:[])]);
  assert(!document.querySelector('.cw-process-head em'));assert(!document.querySelector('.cw-progress-summary'));
  assert.equal(document.querySelectorAll('.cw-step-list li.current').length,state.model.hookProcessSteps.filter(x=>x.state==='current').length);
  assert(document.querySelector('.cw-hook-process[role="log"][aria-live="polite"]'));
  const button=document.querySelector('.cw-dock button');assert(button);
  const accepting=state.model.status.receiveState==='接单中';
  assert.equal(button.textContent,accepting?'暂停接单':'开始接单');
  if(!accepting)assert.equal(button.disabled,index===3);
  if(index===1){
    const viewport=document.querySelector('.cw-chain-viewport');
    Object.defineProperty(viewport,'clientHeight',{get:()=>220});
    const originalRect=dom.window.HTMLElement.prototype.getBoundingClientRect;
    dom.window.HTMLElement.prototype.getBoundingClientRect=function(){
      if(this===viewport)return {top:0,height:220};
      if(this.classList.contains('cw-step-marker')){
        const index=[...document.querySelectorAll('.cw-step-marker')].indexOf(this);
        return {top:index*50-viewport.scrollTop,height:18};
      }
      return originalRect.call(this);
    };
    Object.defineProperty(dom.window.HTMLElement.prototype,'offsetHeight',{get(){return this.classList.contains('cw-step-marker')?18:0;},configurable:true});
    let total=1200;Object.defineProperty(viewport,'scrollHeight',{get:()=>total});
    globalThis.__startupState=structuredClone(state);
    globalThis.__startupState.model.hookProcessSteps.at(-1).description='完成显示校验';
    await view.render();
    await view.scroll(viewport,0);
    globalThis.__startupState=structuredClone(state);
    globalThis.__startupState.model.hookProcessSteps.push({state:'current',title:'读取下一条消息',time:'14:01:00'});
    total=1300;await view.render();assert.equal(viewport.scrollTop,0,'manual review must not be pulled down');
    await view.scroll(viewport,1080);
    globalThis.__startupState=structuredClone(globalThis.__startupState);
    globalThis.__startupState.model.hookProcessSteps.push({state:'current',title:'再次处理客户消息',time:'14:01:01'});
    total=1400;await view.render();assert.equal(viewport.scrollTop,1049,'following should resume with latest running marker centered');
    assert.equal(viewport.style.getPropertyValue('--cw-timeline-center-padding'),'101px');
    await view.scroll(viewport,1049);
    globalThis.__startupState=structuredClone(globalThis.__startupState);
    for(const step of globalThis.__startupState.model.hookProcessSteps)if(step.state==='current')step.state='done';
    await view.render();assert.equal(viewport.scrollTop,1180,'completed progress follows the last result');
    assert.equal(viewport.style.getPropertyValue('--cw-timeline-center-padding'),'');
    assert.equal(document.querySelectorAll('.cw-step-list li').length,24);
  }
  await view.dispose();
 }finally{for(const c of channels){c.port1.close();c.port2.close();}dom.window.close();for(const key of keys){const descriptor=old.get(key);if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}rmSync(dir,{recursive:true,force:true});}
});
