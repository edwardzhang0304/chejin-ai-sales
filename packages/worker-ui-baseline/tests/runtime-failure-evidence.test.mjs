import { test } from 'node:test';
import assert from 'node:assert/strict';
import { installRuntimeFailureEvidence } from '../src/runtimeFailureEvidence.mjs';
import { connectRuntimeBridge } from '../src/runtimeBridgeState.mjs';

test('real event dispatch queues pre-bridge errors and never sends secret messages', () => {
  const target = new EventTarget();
  const recorder = installRuntimeFailureEvidence(target);
  const event = new Event('error');
  Object.assign(event, {error: new TypeError('SECRET_TOKEN'), filename: 'file:///app/runtime.js?SECRET_TOKEN', lineno: 12, colno: 3});
  target.dispatchEvent(event);
  const records = [];
  recorder.attach({reportUiFailure: payload => records.push(JSON.parse(payload))});
  const rejection = new Event('unhandledrejection');
  rejection.reason = new Error('SECRET_TOKEN');
  target.dispatchEvent(rejection);
  assert.equal(records[0].kind, 'javascript_error');
  assert.equal(records[0].exception_type, 'TypeError');
  assert.equal(records[0].file, 'runtime.js');
  assert.equal(records[0].line, 12);
  assert.equal(records[1].kind, 'unhandled_rejection');
  assert(!JSON.stringify(records).includes('SECRET_TOKEN'));
  recorder.dispose();
  target.dispatchEvent(event);
  assert.equal(records.length, 2);
});

test('malformed real bridge snapshots leave evidence while preserving last good state', () => {
  const records = [];
  const applied = [];
  const bridge = {
    initialState(callback) { callback(JSON.stringify({screen: 'running', model: {}, revision: 2})); },
    reportUiFailure(payload) { records.push(JSON.parse(payload)); },
  };
  const connection = connectRuntimeBridge(bridge, state => applied.push(state));
  connection.applyBridgeState('SECRET_TOKEN:broken');
  connection.applyBridgeState('{}');
  assert.deepEqual(records.map(r => r.kind), ['bridge_invalid_json', 'bridge_invalid_state']);
  assert.equal(applied.length, 1);
  assert.equal(connection.getLatestRevision(), 2);
  assert(!JSON.stringify(records).includes('SECRET_TOKEN'));
});
