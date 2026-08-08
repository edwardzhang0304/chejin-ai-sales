export interface RuntimeBridgeEnvelope<TModel = unknown> {
  revision: number;
  screen: string;
  model: TModel;
  bindError?: string;
  notice?: string;
}

export interface RuntimeBridgeSignal {
  connect(callback: (payload: string) => void): void;
}

export interface RuntimeBridge {
  stateChanged?: RuntimeBridgeSignal;
  initialState(callback: (payload: string) => void): void;
}

export interface RuntimeBridgeController<TModel = unknown> {
  applyBridgeState(payload: string): void;
  getCurrentState(): RuntimeBridgeEnvelope<TModel> | null;
  getLatestRevision(): number;
}

export function connectRuntimeBridge<TModel = unknown>(
  bridge: RuntimeBridge,
  onState: (state: RuntimeBridgeEnvelope<TModel>) => void,
): RuntimeBridgeController<TModel>;

export function runtimePageKind(screen: string): "bind" | "workbench";
