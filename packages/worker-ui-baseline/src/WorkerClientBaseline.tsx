import React, { useEffect, useRef, useState } from "react";
import type {
  TimelineStepModel,
  WorkerClientModel,
  WorkerClientScreen,
  WorkerReceiveState,
} from "./types";

interface WorkerClientBaselineProps {
  screen: WorkerClientScreen;
  model: WorkerClientModel;
  onScreenChange?: (screen: WorkerClientScreen) => void;
  onStartAccepting?: () => void;
  onPauseAccepting?: () => void;
  onUpdateAcceptSchedule?: (enabled: boolean, start: string, end: string) => void;
  onExportLatestIncident?: () => void;
  onExportIncident?: (incidentId: string) => void;
  onOpenIncidentDirectory?: () => void;
  onBind?: (workerId: string, workerToken: string) => void;
  bindError?: string;
  onBack?: () => void;
}

function Icon({ name }: { name: "min" | "max" | "close" | "gear" | "back" | "check" | "search" | "x" | "chevron" }) {
  if (name === "min") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14" /></svg>;
  if (name === "max") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h14v14H5z" /></svg>;
  if (name === "close") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>;
  if (name === "gear") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915" />
        <circle cx="12" cy="12" r="3" />
      </svg>
    );
  }
  if (name === "back") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 19-7-7 7-7" /><path d="M19 12H5" /></svg>;
  if (name === "check") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>;
  if (name === "search") return <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="8" /><path d="m21 21-4.3-4.3" /></svg>;
  if (name === "x") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m18 6-12 12" /><path d="m6 6 12 12" /></svg>;
  return <svg className="cw-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>;
}

function clientTitle(screen: WorkerClientScreen) {
  if (screen === "settings") return "设置";
  if (screen === "schedule-settings") return "接单时段设置";
  if (screen === "logs") return "本机执行日志";
  return "Worker 工作台";
}

function chipClass(text: string) {
  if (text.includes("失败") || text.includes("离线")) return "cw-chip-offline cw-chip-failed";
  if (text.includes("完成") || text.includes("接单中")) return "cw-chip-accepting cw-chip-completed";
  return "cw-chip-paused";
}

function Header({ screen, onScreenChange, onBack }: Pick<WorkerClientBaselineProps, "screen" | "onScreenChange" | "onBack">) {
  const isSubPage = screen === "settings" || screen === "schedule-settings" || screen === "logs";
  return (
    <>
      <header className="cw-titlebar titlebar">
        <div className="cw-titlebar-brand">
          <span className="cw-brand-mark">车</span>
          <span>车金 Worker 客户端</span>
        </div>
        <div className="cw-window-controls" aria-hidden="true">
          <span><Icon name="min" /></span>
          <span><Icon name="max" /></span>
          <span><Icon name="close" /></span>
        </div>
      </header>
      <header className="cw-client-bar client-bar">
        <button
          className="cw-icon-button"
          type="button"
          aria-label="返回"
          style={{ visibility: isSubPage ? "visible" : "hidden" }}
          onClick={() => {
            if (onBack) {
              onBack();
              return;
            }
            onScreenChange?.("settings");
          }}
        >
          <Icon name="back" />
        </button>
        <strong>{clientTitle(screen)}</strong>
        <button
          className="cw-icon-button"
          type="button"
          aria-label="打开设置"
          style={{ visibility: isSubPage ? "hidden" : "visible" }}
          onClick={() => onScreenChange?.("settings")}
        >
          <Icon name="gear" />
        </button>
      </header>
    </>
  );
}

function ConnectionLine({ model, danger = false }: { model: WorkerClientModel; danger?: boolean }) {
  return (
    <p className={`cw-connection-line connection-line${danger ? " cw-connection-line-danger danger" : ""}`}>
      {danger ? "连接异常" : model.status.connectionState} · 最近心跳 {model.status.lastHeartbeat}
    </p>
  );
}

function StatusSummary({ model, stateText }: { model: WorkerClientModel; stateText?: WorkerReceiveState }) {
  const automationDanger = model.status.automationState !== "可用";
  const wechatDanger = model.status.wechatState !== "已连接";
  return (
    <section className="cw-status-card status-grid" aria-label="Worker 状态摘要">
      <div className="cw-status-top">
        <strong className="cw-status-seller">{model.status.sellerName}</strong>
        <span className={`cw-chip chip ${chipClass(stateText || model.status.receiveState)}`}>{stateText || model.status.receiveState}</span>
      </div>
      <div className="cw-status-bottom">
        <div className="cw-status-item">
          <span>自动化组件</span>
          <i className={`cw-dot dot ${automationDanger ? "cw-dot-danger" : "online"}`} />
          <em className="cw-status-pill">{model.status.automationState}</em>
        </div>
        <div className="cw-status-item">
          <span>微信状态</span>
          <i className={`cw-dot dot ${wechatDanger ? "cw-dot-danger" : "online"}`} />
          <em className="cw-status-pill">{model.status.wechatState}</em>
        </div>
      </div>
    </section>
  );
}

function TaskSummary({ model, statusText }: { model: WorkerClientModel; statusText?: string }) {
  const task = model.task;
  const meta = task.metaText || `${task.customerName} · ${task.phone} · ${task.sellerName} · 备注短码：${task.noteCode}`;
  return (
    <article className="cw-task-card task-summary task-summary-compact">
      <div className="cw-task-title-row task-title-row">
        <div>
          <p className="cw-task-id task-id">{task.id}</p>
          <h3 className="cw-task-title">{task.title}</h3>
        </div>
        <span className={`cw-chip chip ${chipClass(statusText || task.statusText)}`}>{statusText || task.statusText}</span>
      </div>
      <p className="cw-task-meta task-mini-meta">{meta}</p>
    </article>
  );
}

function StepScreenshot({ step }: { step: TimelineStepModel }) {
  if (!step.screenshot) return null;
  return (
    <figure className="cw-step-shot step-shot" aria-label="视觉 RPA 截图">
      <div className="cw-shot-window shot-window">
        <div className="cw-shot-search shot-search">{step.screenshot.searchText}</div>
        <div className="cw-shot-empty shot-empty">{step.screenshot.resultText}</div>
      </div>
      <figcaption>{step.screenshot.caption}</figcaption>
    </figure>
  );
}

function TimelineStep({ step }: { step: TimelineStepModel }) {
  const final = Boolean(step.finalText);
  const className = [
    "cw-step",
    step.state === "done" ? "done" : "",
    step.state === "current" ? "current cw-step-current" : "",
    step.state === "error" ? "error cw-step-error" : "",
    final ? "final-step cw-step-final" : "",
  ].filter(Boolean).join(" ");
  return (
    <li className={className}>
      <span className="cw-step-marker">
        <Icon name={step.state === "error" ? "x" : step.state === "current" ? "search" : "check"} />
      </span>
      <div>
        <strong>{step.title}</strong>
        {step.time ? <p>{step.time}</p> : null}
        {step.description ? <p>{step.description}</p> : null}
        <StepScreenshot step={step} />
        {step.finalText ? <em className="cw-step-final-text">{step.finalText}</em> : null}
      </div>
    </li>
  );
}

function TaskTimeline({ steps }: { steps: TimelineStepModel[] }) {
  const viewportRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const focusStep = viewport.querySelector(".final-step, .current, .error") as HTMLElement | null;
    const focusMarker = focusStep?.querySelector(":scope > span") as HTMLElement | null;
    if (!focusStep || !focusMarker) return;
    const cardRect = viewport.parentElement?.getBoundingClientRect() || viewport.getBoundingClientRect();
    const markerRect = focusMarker.getBoundingClientRect();
    const nextScrollTop = viewport.scrollTop + markerRect.top + markerRect.height / 2 - (cardRect.top + cardRect.height / 2) - 10;
    const maxScrollTop = viewport.scrollHeight - viewport.clientHeight;
    viewport.scrollTop = Math.max(0, Math.min(maxScrollTop, nextScrollTop));
  }, [steps]);

  return (
    <article className="cw-timeline-card timeline-card focus-chain">
      <div className="cw-chain-viewport chain-viewport" ref={viewportRef}>
        <ol className="cw-step-list step-list">
          {steps.map((step, index) => (
            <TimelineStep key={`${step.title}-${index}`} step={step} />
          ))}
        </ol>
      </div>
    </article>
  );
}

function Dock({
  state,
  disabled = false,
  onStartAccepting,
  onPauseAccepting,
}: {
  state: WorkerReceiveState;
  disabled?: boolean;
  onStartAccepting?: () => void;
  onPauseAccepting?: () => void;
}) {
  const accepting = state === "接单中";
  return (
    <footer className="cw-dock dock-action">
      <span>{state}</span>
      <button
        className={`cw-button${accepting ? "" : " cw-button-primary"}`}
        type="button"
        disabled={disabled}
        onClick={accepting ? onPauseAccepting : onStartAccepting}
      >
        {accepting ? "暂停接单" : "开始接单"}
      </button>
    </footer>
  );
}

function EmptyWorkbench({
  screen,
  model,
  title,
  description,
  stateText,
  dockState,
  disabled,
  onStartAccepting,
  onPauseAccepting,
}: {
  screen: WorkerClientScreen;
  model: WorkerClientModel;
  title: string;
  description: string;
  stateText: WorkerReceiveState;
  dockState: WorkerReceiveState;
  disabled?: boolean;
  onStartAccepting?: () => void;
  onPauseAccepting?: () => void;
}) {
  return (
    <section className="cw-screen screen active" data-screen-view={screen}>
      <div className="cw-workspace workspace">
        <header className="cw-workspace-head workspace-head">
          <ConnectionLine model={model} />
        </header>
        <StatusSummary model={model} stateText={stateText} />
        <section className="cw-empty-card empty-card">
          <div>
            <h3>{title}</h3>
            <p>{description}</p>
          </div>
        </section>
        <Dock state={dockState} disabled={disabled} onStartAccepting={onStartAccepting} onPauseAccepting={onPauseAccepting} />
      </div>
    </section>
  );
}

function TaskScreen({
  screen,
  model,
  steps,
  statusText,
  dockState = "接单中",
  onStartAccepting,
  onPauseAccepting,
}: {
  screen: WorkerClientScreen;
  model: WorkerClientModel;
  steps: TimelineStepModel[];
  statusText: string;
  dockState?: WorkerReceiveState;
  onStartAccepting?: () => void;
  onPauseAccepting?: () => void;
}) {
  return (
    <section className="cw-screen cw-screen-task screen active" data-screen-view={screen}>
      <div className="cw-workspace workspace">
        <header className="cw-workspace-head workspace-head">
          <ConnectionLine model={model} />
        </header>
        <section className="cw-task-layout task-layout">
          <TaskSummary model={model} statusText={statusText} />
          <TaskTimeline steps={steps} />
        </section>
        <Dock state={dockState} onStartAccepting={onStartAccepting} onPauseAccepting={onPauseAccepting} />
      </div>
    </section>
  );
}

function BindScreen({
  model,
  onBind,
  bindError,
}: {
  model: WorkerClientModel;
  onBind?: (workerId: string, workerToken: string) => void;
  bindError?: string;
}) {
  const [workerId, setWorkerId] = useState(model.workerId);
  const [workerToken, setWorkerToken] = useState(model.workerToken);

  useEffect(() => {
    setWorkerId(model.workerId);
    setWorkerToken(model.workerToken);
  }, [model.workerId, model.workerToken]);

  return (
    <section className="cw-screen screen active" data-screen-view="bind">
      <div className="cw-bind-layout">
        <form
          className="cw-form-panel"
          onSubmit={(event) => {
            event.preventDefault();
            onBind?.(workerId.trim(), workerToken.trim());
          }}
        >
          <div>
            <h2>绑定本机 Worker</h2>
            <p className="cw-helper">输入后台生成的 Worker ID 和 Token。</p>
          </div>
          <label>
            <span>Worker ID</span>
            <input className="cw-input" value={workerId} onChange={(event) => setWorkerId(event.target.value)} />
          </label>
          <label>
            <span>Worker Token</span>
            <input className="cw-input" type="password" value={workerToken} onChange={(event) => setWorkerToken(event.target.value)} />
          </label>
          <button className="cw-button cw-button-primary" type="submit">绑定 Worker</button>
          {bindError ? <p className="cw-helper cw-bind-error">{bindError}</p> : null}
        </form>
      </div>
    </section>
  );
}

function OfflineScreen({ model }: { model: WorkerClientModel }) {
  return (
    <section className="cw-screen screen active" data-screen-view="offline">
      <div className="cw-workspace workspace">
        <header className="cw-workspace-head workspace-head">
          <ConnectionLine model={model} danger />
          <span className="cw-chip chip cw-chip-offline">离线</span>
        </header>
        <TaskSummary model={model} statusText="离线" />
        <StatusSummary model={model} stateText="离线" />
        <Dock state="离线" disabled />
      </div>
    </section>
  );
}

function SettingsScreen({ model, onScreenChange }: Pick<WorkerClientBaselineProps, "model" | "onScreenChange">) {
  return (
    <section className="cw-screen screen active" data-screen-view="settings">
      <div className="cw-settings-page">
        <header className="cw-workspace-head workspace-head">
          <div>
            <p className="cw-eyebrow">设置</p>
            <h2>客户端设置</h2>
          </div>
        </header>
        <section className="cw-settings-list settings-list">
          <button className="cw-settings-row settings-row" type="button" onClick={() => onScreenChange?.("schedule-settings")}>
            <span><strong>自动接单时段</strong><em>{model.schedule.enabled ? "开启" : "关闭"} · {model.schedule.start} 至 {model.schedule.end}</em></span>
            <Icon name="chevron" />
          </button>
          <button className="cw-settings-row settings-row" type="button" onClick={() => onScreenChange?.("logs")}>
            <span><strong>本机执行日志</strong><em>查看最近 30 天，最多 1000 条本机日志</em></span>
            <Icon name="chevron" />
          </button>
          <div className="cw-settings-row settings-row">
            <span><strong>客户端版本号</strong><em>{model.version}</em></span>
          </div>
        </section>
      </div>
    </section>
  );
}

function ScheduleSettingsScreen({
  model,
  onUpdateAcceptSchedule,
}: Pick<WorkerClientBaselineProps, "model" | "onUpdateAcceptSchedule">) {
  const updateSchedule = (patch: Partial<WorkerClientModel["schedule"]>) => {
    const next = { ...model.schedule, ...patch };
    onUpdateAcceptSchedule?.(next.enabled, next.start, next.end);
  };

  return (
    <section className="cw-screen screen active" data-screen-view="schedule-settings">
      <div className="cw-settings-page">
        <header className="cw-workspace-head workspace-head">
          <div>
            <p className="cw-eyebrow">设置 / 接单时段设置</p>
            <h2>接单时段</h2>
          </div>
        </header>
        <section className="cw-settings-list settings-list cw-schedule-list">
          <div className="cw-settings-row cw-schedule-row">
            <span><strong>自动接单</strong><em>仅在设定时间内领取新任务</em></span>
            <button
              className="cw-switch"
              type="button"
              aria-pressed={model.schedule.enabled}
              onClick={() => updateSchedule({ enabled: !model.schedule.enabled })}
            >
              <span>{model.schedule.enabled ? "开启" : "关闭"}</span>
              <i aria-hidden="true" />
            </button>
          </div>
          <div className="cw-settings-row cw-schedule-row">
            <span><strong>接单时间</strong><em>每天一个时间段，支持跨天</em></span>
            <div className="cw-time-range">
              <label><span>开始</span><input type="time" value={model.schedule.start} onChange={(event) => updateSchedule({ start: event.currentTarget.value })} /></label>
              <b>至</b>
              <label><span>结束</span><input type="time" value={model.schedule.end} onChange={(event) => updateSchedule({ end: event.currentTarget.value })} /></label>
            </div>
          </div>
          <p className="cw-settings-note">非接单时段保持连接，但不领取新任务；执行中的任务会先完成。</p>
        </section>
      </div>
    </section>
  );
}

function LogsScreen({
  model,
  onExportLatestIncident,
  onExportIncident,
  onOpenIncidentDirectory,
}: Pick<
  WorkerClientBaselineProps,
  "model" | "onExportLatestIncident" | "onExportIncident" | "onOpenIncidentDirectory"
>) {
  return (
    <section className="cw-screen screen active" data-screen-view="logs">
      <div className="cw-logs-page">
        <header className="cw-workspace-head workspace-head">
          <div>
            <p className="cw-eyebrow">设置 / 本机执行日志</p>
            <h2>本机执行日志明细</h2>
          </div>
        </header>
        <div className="cw-incident-actions">
          <button type="button" onClick={onExportLatestIncident}>导出最近故障</button>
          <button type="button" onClick={onOpenIncidentDirectory}>打开证据目录</button>
        </div>
        {model.latestIncident?.incident_id ? (
          <p className="cw-incident-latest">最近故障：{model.latestIncident.incident_id}</p>
        ) : null}
        <section className="cw-log-table log-table">
          <div className="cw-log-head"><span>时间</span><span>级别</span><span>任务</span><span>事件 / 故障证据</span></div>
          {model.logs.map((row, index) => (
            <div className="cw-log-row" key={`${row.time}-${index}`}>
              <span>{row.time}</span>
              <strong>{row.level}</strong>
              <span>{row.task}</span>
              <p>
                <b>{row.event}</b>
                <span>{row.content}</span>
                <small>error_code: {row.errorCode}</small>
                <small>incident_id: {row.incidentId}</small>
                <small>sidecar_run_id: {row.sidecarRunId}</small>
                <small>evidence: {row.evidencePath}</small>
                {row.incidentId !== "-" ? (
                  <button
                    className="cw-log-incident-export"
                    type="button"
                    onClick={() => onExportIncident?.(row.incidentId)}
                  >
                    导出此故障
                  </button>
                ) : null}
              </p>
            </div>
          ))}
        </section>
      </div>
    </section>
  );
}

function renderScreen(props: WorkerClientBaselineProps) {
  const { screen, model, onScreenChange, onStartAccepting, onPauseAccepting, onUpdateAcceptSchedule } = props;
  if (screen === "bind") return <BindScreen model={model} onBind={props.onBind} bindError={props.bindError} />;
  if (screen === "paused-empty") {
    return (
      <EmptyWorkbench
        screen={screen}
        model={model}
        title="暂无可领取任务"
        description="暂停接单后不会领取新的任务。"
        stateText="暂停接单"
        dockState="暂停接单"
        onStartAccepting={onStartAccepting}
      />
    );
  }
  if (screen === "accepting-wait") {
    return (
      <EmptyWorkbench
        screen={screen}
        model={model}
        title="接单中，等待服务端分配任务"
        description="有可执行任务时，Worker 会领取并进入任务链路页面。"
        stateText="接单中"
        dockState="接单中"
        onPauseAccepting={onPauseAccepting}
      />
    );
  }
  if (screen === "schedule-paused") {
    return (
      <EmptyWorkbench
        screen={screen}
        model={model}
        title="当前不领取新任务"
        description="非接单时段客户端保持连接，但不会领取新的任务。"
        stateText="暂停接单"
        dockState="暂停接单"
        disabled
      />
    );
  }
  if (screen === "running") return <TaskScreen screen={screen} model={model} steps={model.runningSteps} statusText="接单中" dockState={model.status.receiveState} onStartAccepting={onStartAccepting} onPauseAccepting={onPauseAccepting} />;
  if (screen === "completed") return <TaskScreen screen={screen} model={model} steps={model.completedSteps} statusText="已完成" dockState={model.status.receiveState} onStartAccepting={onStartAccepting} onPauseAccepting={onPauseAccepting} />;
  if (screen === "paused-running") return <TaskScreen screen={screen} model={model} steps={model.runningSteps} statusText="暂停接单" dockState="暂停接单" onStartAccepting={onStartAccepting} />;
  if (screen === "paused-empty-2") {
    return (
      <EmptyWorkbench
        screen={screen}
        model={model}
        title="暂无可领取任务"
        description="上一条任务已结束，当前暂停接单，不会继续领取下一条任务。"
        stateText="暂停接单"
        dockState="暂停接单"
        onStartAccepting={onStartAccepting}
      />
    );
  }
  if (screen === "offline") return <OfflineScreen model={model} />;
  if (screen === "failed") return <TaskScreen screen={screen} model={model} steps={model.failedSteps} statusText="失败" dockState={model.status.receiveState} onStartAccepting={onStartAccepting} onPauseAccepting={onPauseAccepting} />;
  if (screen === "settings") return <SettingsScreen model={model} onScreenChange={onScreenChange} />;
  if (screen === "schedule-settings") return <ScheduleSettingsScreen model={model} onUpdateAcceptSchedule={onUpdateAcceptSchedule} />;
  return (
    <LogsScreen
      model={model}
      onExportLatestIncident={props.onExportLatestIncident}
      onExportIncident={props.onExportIncident}
      onOpenIncidentDirectory={props.onOpenIncidentDirectory}
    />
  );
}

export function WorkerClientBaseline(props: WorkerClientBaselineProps) {
  return (
    <section className="cw-window app-window" data-active-screen={props.screen} aria-label="车金 Worker 客户端">
      <Header screen={props.screen} onScreenChange={props.onScreenChange} onBack={props.onBack} />
      <div className="cw-app-body">
        {renderScreen(props)}
      </div>
    </section>
  );
}
