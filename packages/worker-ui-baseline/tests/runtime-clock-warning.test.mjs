import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL,fileURLToPath} from 'node:url';
const require=createRequire(new URL('../../../frontend/package.json',import.meta.url));
const {buildSync}=require('esbuild');
test('clock warning preserves start permission and never claims calibration when absent',async()=>{
 const dir=mkdtempSync(resolve(tmpdir(),'chejin-clock-warning-'));
 try{
  const output=resolve(dir,'render.mjs');
  buildSync({stdin:{contents:`import React from 'react';import{renderToStaticMarkup}from'react-dom/server';import{WorkerClientBaseline}from'./WorkerClientBaseline';import{workerClientMock}from'./mockData';export function render(canStart,notice){const model={...workerClientMock,status:{...workerClientMock.status,receiveState:'暂停接单',acceptingIntent:false},startupCheck:{state:canStart?'passed':'checking',title:'检查中',message:'',canStart,clockCheck:{notice}}};return renderToStaticMarkup(<WorkerClientBaseline screen="paused-empty" model={model}/>);}`,resolveDir:fileURLToPath(new URL('../src/',import.meta.url)),loader:'tsx'},bundle:true,platform:'node',format:'esm',banner:{js:"import {createRequire} from 'node:module';const require=createRequire(import.meta.url);"},outfile:output,nodePaths:[fileURLToPath(new URL('../../../frontend/node_modules/',import.meta.url))],logLevel:'silent'});
  const view=await import(pathToFileURL(output));
  const {JSDOM}=require('jsdom');
  for(const canStart of [true,false]){
   const dom=new JSDOM(view.render(canStart,'请将电脑时区设为北京时间（UTC+8，北京）'));
   const button=[...dom.window.document.querySelectorAll('button')].find(x=>x.textContent==='开始接单');
   assert(button);assert.equal(button.disabled,!canStart);
   assert.match(dom.window.document.querySelector('.cw-clock-warning').textContent,/北京时间/);
   dom.window.close();
  }
  assert(!view.render(true,'').includes('cw-clock-warning'));
 }finally{rmSync(dir,{recursive:true,force:true});}
});
