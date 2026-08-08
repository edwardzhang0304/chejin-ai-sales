import assert from "node:assert/strict";
import test from "node:test";

import {
  connectRuntimeBridge,
  runtimePageKind,
} from "../src/runtimeBridgeState.mjs";

function payload(revision, screen, workerId = "") {
  return JSON.stringify({
    revision,
    screen,
    model: { workerId },
    bindError: "",
    notice: "",
  });
}

function signal() {
  let callback = null;
  return {
    connect(nextCallback) {
      callback = nextCallback;
    },
    emit(value) {
      assert.ok(callback, "stateChanged must be connected before an event is emitted");
      callback(value);
    },
  };
}

test("existing binding starts on the workbench instead of the binding page", () => {
  const stateChanged = signal();
  const observed = [];
  connectRuntimeBridge(
    {
      stateChanged,
      initialState(callback) {
        callback(payload(1, "paused-empty", "worker-existing"));
      },
    },
    (state) => observed.push(state),
  );

  assert.equal(observed.at(-1).screen, "paused-empty");
  assert.equal(runtimePageKind(observed.at(-1).screen), "workbench");
});

test("binding success event immediately leaves the binding page", () => {
  const stateChanged = signal();
  let current = null;
  connectRuntimeBridge(
    {
      stateChanged,
      initialState(callback) {
        callback(payload(1, "bind"));
      },
    },
    (state) => {
      current = state;
    },
  );

  assert.equal(runtimePageKind(current.screen), "bind");
  stateChanged.emit(payload(2, "paused-empty", "worker-new"));
  assert.equal(current.model.workerId, "worker-new");
  assert.equal(runtimePageKind(current.screen), "workbench");
});

test("a stale initial snapshot cannot overwrite a newer binding event", () => {
  const stateChanged = signal();
  let current = null;
  const controller = connectRuntimeBridge(
    {
      stateChanged,
      initialState(callback) {
        stateChanged.emit(payload(2, "paused-empty", "worker-new"));
        callback(payload(1, "bind"));
      },
    },
    (state) => {
      current = state;
    },
  );

  assert.equal(controller.getLatestRevision(), 2);
  assert.equal(current.screen, "paused-empty");
  assert.equal(current.model.workerId, "worker-new");
});

test("the page selector used by the UI no longer renders BindScreen after binding", () => {
  const stateChanged = signal();
  let renderedPage = "";
  connectRuntimeBridge(
    {
      stateChanged,
      initialState(callback) {
        callback(payload(1, "bind"));
      },
    },
    (state) => {
      renderedPage = runtimePageKind(state.screen);
    },
  );

  assert.equal(renderedPage, "bind");
  stateChanged.emit(payload(2, "accepting-wait", "worker-new"));
  assert.equal(renderedPage, "workbench");
});
