/** Report only error identity and source location, never state/tokens/messages. */
export function installRuntimeFailureEvidence(target) {
  let bridge = null;
  const pending = [];
  function report(kind, error, location = {}) {
    const record = {
      kind,
      exception_type: /^[A-Za-z][A-Za-z0-9]*Error$/.test(error?.name) ? error.name : "Error",
      file: String(location.filename || "").split(/[\\/]/).pop().split(/[?#]/)[0].slice(0, 100),
      line: Number(location.lineno) || 0,
      column: Number(location.colno) || 0,
    };
    if (!bridge?.reportUiFailure) {
      if (pending.length < 10) pending.push(record);
      return;
    }
    try { bridge.reportUiFailure(JSON.stringify(record)); } catch { /* Qt crash is recorded by the host. */ }
  }
  const onError = (event) => report("javascript_error", event.error, event);
  const onRejection = (event) => report("unhandled_rejection", event.reason);
  target.addEventListener("error", onError);
  target.addEventListener("unhandledrejection", onRejection);
  return {
    report,
    attach(nextBridge) {
      bridge = nextBridge;
      for (const item of pending.splice(0)) report(item.kind, { name: item.exception_type }, {
        filename: item.file, lineno: item.line, colno: item.column,
      });
    },
    dispose() {
      target.removeEventListener("error", onError);
      target.removeEventListener("unhandledrejection", onRejection);
    },
  };
}
