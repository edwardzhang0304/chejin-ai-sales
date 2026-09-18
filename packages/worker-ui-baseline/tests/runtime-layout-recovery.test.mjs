import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { MessageChannel } from 'node:worker_threads';

const require = createRequire(new URL('../../../frontend/package.json', import.meta.url));
const { JSDOM } = require('jsdom');
const { buildSync } = require('esbuild');

const recoveryStates = process.env.CHEJIN_CORRECTION_UI_STATES
  ? JSON.parse(readFileSync(process.env.CHEJIN_CORRECTION_UI_STATES, 'utf8'))
  : process.env.CHEJIN_LAYOUT_UI_STATE
    ? [JSON.parse(readFileSync(process.env.CHEJIN_LAYOUT_UI_STATE, 'utf8'))] : [];

if (!recoveryStates.length) {
  test('recovery DOM needs a real Worker state artifact', { skip: true }, () => {});
}

for (const [index, state] of recoveryStates.entries()) {
test(`real React recovery state ${index}: reason and Start permission match Worker`, async () => {
  assert(['paused-empty', 'client-faulted'].includes(state.screen));
  const expectedEnabled = state.screen === 'paused-empty' || Boolean(state.model.faultRecovery?.ready && !state.model.faultRecovery.checking);
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
  const keys = ['window', 'document', 'HTMLElement', 'MessageChannel', 'IS_REACT_ACT_ENVIRONMENT', '__layoutState'];
  const previous = new Map(keys.map(k => [k, Object.getOwnPropertyDescriptor(globalThis, k)]));
  const channels = [];
  class TestMessageChannel extends MessageChannel {
    constructor() { super(); channels.push(this); }
  }
  Object.assign(globalThis, { window: dom.window, document: dom.window.document,
    HTMLElement: dom.window.HTMLElement, MessageChannel: TestMessageChannel,
    IS_REACT_ACT_ENVIRONMENT: true, __layoutState: state });
  const dir = mkdtempSync(resolve(tmpdir(), 'chejin-layout-ui-'));
  try {
    const output = resolve(dir, 'render.mjs');
    buildSync({
      stdin: { contents: `
        import React, {act} from 'react';
        import {createRoot} from 'react-dom/client';
        import {WorkerClientBaseline} from './WorkerClientBaseline';
        export let starts = 0;
        const root = createRoot(document.getElementById('root'));
        export async function render() { await act(async () => root.render(
          <WorkerClientBaseline screen={globalThis.__layoutState.screen}
            model={globalThis.__layoutState.model} onStartAccepting={() => starts++} />)); }
        export async function click(button) { await act(async () => button.click()); }
        export async function dispose() { await act(async () => root.unmount()); }
      `, resolveDir: new URL('../src/', import.meta.url).pathname, loader: 'tsx' },
      nodePaths: [resolve(new URL('../../../frontend/node_modules/', import.meta.url).pathname)],
      bundle: true, platform: 'browser', format: 'esm', outfile: output, logLevel: 'silent',
    });
    const rendered = await import(pathToFileURL(output));
    await rendered.render();
    assert(document.body.textContent.includes(state.model.faultRecovery?.reason || state.model.layoutRecovery.message));
    assert(!document.body.textContent.includes('未发现待处理客户'));
    const button = [...document.querySelectorAll('button')].find(b => b.textContent === '开始接单');
    assert(button);
    assert.equal(!button.disabled, expectedEnabled);
    await rendered.click(button);
    assert.equal(rendered.starts, expectedEnabled ? 1 : 0);
    await rendered.dispose();
  } finally {
    for (const channel of channels) { channel.port1.close(); channel.port2.close(); }
    dom.window.close();
    for (const k of keys) {
      const descriptor = previous.get(k);
      if (descriptor) Object.defineProperty(globalThis, k, descriptor); else delete globalThis[k];
    }
    rmSync(dir, { recursive: true, force: true });
  }
});
}
