/**
 * Apply QWebChannel state snapshots in revision order.
 *
 * This module intentionally has no React or Qt dependency so the same logic
 * used by the real page can be exercised with a real event sequence in Node.
 */
export function connectRuntimeBridge(bridge, onState) {
  let latestRevision = -1;
  let currentState = null;

  const applyBridgeState = (payload) => {
    let parsed;
    try {
      parsed = JSON.parse(payload);
    } catch {
      return;
    }
    if (!parsed || typeof parsed.screen !== "string" || !parsed.model) return;
    const revision = Number.isFinite(parsed.revision) ? Number(parsed.revision) : 0;
    if (revision < latestRevision) return;
    latestRevision = revision;
    currentState = parsed;
    onState(parsed);
  };

  // Subscribe first. A binding event may arrive while initialState is in flight.
  bridge.stateChanged?.connect(applyBridgeState);
  bridge.initialState(applyBridgeState);

  return {
    applyBridgeState,
    getCurrentState: () => currentState,
    getLatestRevision: () => latestRevision,
  };
}

export function runtimePageKind(screen) {
  return screen === "bind" ? "bind" : "workbench";
}
