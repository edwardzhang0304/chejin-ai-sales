import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { WorkerClientBaseline } from "./WorkerClientBaseline";
import { connectRuntimeBridge } from "./runtimeBridgeState.mjs";
import type { WorkerClientModel, WorkerClientScreen } from "./types";

interface BridgeState {
  revision?: number;
  screen: WorkerClientScreen;
  model: WorkerClientModel;
  bindError?: string;
  notice?: string;
}

interface CheJinBridge {
  initialState(callback: (payload: string) => void): void;
  changeScreen(screen: WorkerClientScreen): void;
  goBack(): void;
  bindWorker(workerId: string, workerToken: string): void;
  startAccepting(): void;
  pauseAccepting(): void;
  updateAcceptSchedule(enabled: boolean, start: string, end: string): void;
  checkForUpdates?(): void;
  exportLatestIncident?(): void;
  exportIncident?(incidentId: string): void;
  openIncidentDirectory?(): void;
  startWindowDrag?(screenX: number, screenY: number): void;
  moveWindowDrag?(screenX: number, screenY: number): void;
  endWindowDrag?(): void;
  minimizeWindow?(): void;
  closeWindow?(): void;
  stateChanged?: {
    connect(callback: (payload: string) => void): void;
  };
}

declare global {
  interface Window {
    QWebChannel?: new (transport: unknown, callback: (channel: { objects: { chejinBridge: CheJinBridge } }) => void) => void;
    qt?: { webChannelTransport: unknown };
  }
}

const screens: WorkerClientScreen[] = [
  "bind",
  "paused-empty",
  "accepting-wait",
  "schedule-paused",
  "running",
  "completed",
  "paused-running",
  "paused-empty-2",
  "offline",
  "offline-empty",
  "client-faulted",
  "automation-unavailable",
  "wechat-disconnected",
  "failed",
  "settings",
  "schedule-settings",
  "logs",
  "scan-running",
  "scan-completed",
  "target-read-running",
  "target-read-completed",
  "ai-reply-running",
  "ai-reply-completed",
  "ai-reply-failed",
];

const emptyRuntimeModel: WorkerClientModel = {
  workerId: "",
  workerToken: "",
  version: "",
  schedule: { enabled: false, start: "09:00", end: "21:00" },
  status: {
    sellerName: "未绑定",
    receiveState: "暂停接单",
    connectionState: "连接正常",
    lastHeartbeat: "暂无",
    automationState: "不可用",
    wechatState: "未连接",
  },
  task: {
    id: "-",
    title: "暂无任务",
    statusText: "暂无任务",
    customerName: "暂无客户",
    phone: "-",
    sellerName: "-",
    noteCode: "-",
  },
  runningSteps: [],
  completedSteps: [],
  failedSteps: [],
  scanRunningSteps: [],
  scanCompletedSteps: [],
  targetReadRunningSteps: [],
  targetReadCompletedSteps: [],
  replyRunningSteps: [],
  replyCompletedSteps: [],
  replyFailedSteps: [],
  logs: [],
  latestIncident: null,
  update: { state: "idle", status_text: "", in_progress: false, available: false },
};

function initialScreen(): WorkerClientScreen {
  const screen = new URLSearchParams(window.location.search).get("screen");
  return screens.includes(screen as WorkerClientScreen) ? (screen as WorkerClientScreen) : "bind";
}

function App() {
  const [bridge, setBridge] = useState<CheJinBridge | null>(null);
  const [notice, setNotice] = useState("");
  const [state, setState] = useState<BridgeState>({
    screen: initialScreen(),
    model: emptyRuntimeModel,
  });

  useEffect(() => {
    if (!window.QWebChannel || !window.qt?.webChannelTransport) return;
    new window.QWebChannel(window.qt.webChannelTransport, (channel) => {
      const nextBridge = channel.objects.chejinBridge;
      setBridge(nextBridge);
      connectRuntimeBridge(nextBridge, (nextState: BridgeState) => setState(nextState));
    });
  }, []);

  useEffect(() => {
    const message = state.notice?.trim() || "";
    if (!message) return;
    setNotice(message);
    const timeoutId = window.setTimeout(() => setNotice(""), 3000);
    return () => window.clearTimeout(timeoutId);
  }, [state.notice]);

  useEffect(() => {
    if (!bridge) return;
    const titlebar = document.querySelector(".cw-titlebar");
    if (!titlebar) return;

    let dragging = false;

    function asScreenPoint(event: MouseEvent) {
      return {
        x: Math.round(event.screenX),
        y: Math.round(event.screenY),
      };
    }

    function onMouseDown(event: Event) {
      const mouseEvent = event as MouseEvent;
      if (mouseEvent.button !== 0) return;
      const target = mouseEvent.target as Element | null;
      if (target?.closest(".cw-window-controls")) return;
      const point = asScreenPoint(mouseEvent);
      dragging = true;
      bridge.startWindowDrag?.(point.x, point.y);
      mouseEvent.preventDefault();
    }

    function onMouseMove(event: MouseEvent) {
      if (!dragging) return;
      const point = asScreenPoint(event);
      bridge.moveWindowDrag?.(point.x, point.y);
      event.preventDefault();
    }

    function endDrag() {
      if (!dragging) return;
      dragging = false;
      bridge.endWindowDrag?.();
    }

    function onControlsClick(event: Event) {
      const target = event.target as Element | null;
      const control = target?.closest(".cw-window-controls span");
      if (!control) return;
      const controls = Array.from(document.querySelectorAll(".cw-window-controls span"));
      const index = controls.indexOf(control);
      if (index === 0) bridge.minimizeWindow?.();
      if (index === 2) bridge.closeWindow?.();
      event.preventDefault();
      event.stopPropagation();
    }

    titlebar.addEventListener("mousedown", onMouseDown);
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", endDrag);
    window.addEventListener("blur", endDrag);
    titlebar.addEventListener("click", onControlsClick);

    return () => {
      titlebar.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", endDrag);
      window.removeEventListener("blur", endDrag);
      titlebar.removeEventListener("click", onControlsClick);
    };
  }, [bridge]);

  const appModel = useMemo(() => state.model, [state.model]);

  function changeScreen(next: WorkerClientScreen) {
    if (bridge) {
      bridge.changeScreen(next);
      return;
    }
    setState((current) => ({ ...current, screen: next }));
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("screen", next);
    window.history.replaceState({}, "", nextUrl);
  }

  function updateAcceptSchedule(enabled: boolean, start: string, end: string) {
    setState((current) => ({
      ...current,
      model: {
        ...current.model,
        schedule: { enabled, start, end },
      },
    }));
    bridge?.updateAcceptSchedule(enabled, start, end);
  }

  return (
    <WorkerClientBaseline
      screen={state.screen}
      model={appModel}
      bindError={state.bindError}
      notice={notice}
      onScreenChange={changeScreen}
      onBack={() => {
        const destination = state.model.workerId
          ? state.model.status.receiveState === "接单中"
            ? "accepting-wait"
            : "paused-empty"
          : "bind";
        if (bridge) {
          setState((current) => ({ ...current, screen: destination }));
          bridge.goBack();
          return;
        }
        changeScreen(destination);
      }}
      onBind={(workerId, workerToken) => bridge?.bindWorker(workerId, workerToken)}
      onStartAccepting={() => bridge?.startAccepting()}
      onPauseAccepting={() => bridge?.pauseAccepting()}
      onUpdateAcceptSchedule={updateAcceptSchedule}
      onCheckForUpdates={() => bridge?.checkForUpdates?.()}
      onExportLatestIncident={() => bridge?.exportLatestIncident?.()}
      onExportIncident={(incidentId) => bridge?.exportIncident?.(incidentId)}
      onOpenIncidentDirectory={() => bridge?.openIncidentDirectory?.()}
    />
  );
}

createRoot(document.getElementById("root")!).render(<App />);
