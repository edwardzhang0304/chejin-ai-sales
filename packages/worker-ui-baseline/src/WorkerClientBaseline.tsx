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
  if (screen === "bind") return "绑定 Worker";
  if (screen === "settings") return "设置";
  if (screen === "schedule-settings") return "接单时段设置";
  if (screen === "logs") return "本机执行日志";
  return "Worker 工作台";
}

function chipClass(text: string) {
  if (text.includes("失败") || text.includes("离线")) return "cw-chip-offline cw-chip-failed";
  if (text.includes("完成") || text.includes("接单中") || text.includes("处理中")) return "cw-chip-accepting cw-chip-completed";
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
          style={{ visibility: isSubPage || screen === "bind" ? "hidden" : "visible" }}
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
          <em className={`cw-status-pill${automationDanger ? " is-danger" : ""}`}>{model.status.automationState}</em>
        </div>
        <div className="cw-status-item">
          <span>微信状态</span>
          <i className={`cw-dot dot ${wechatDanger ? "cw-dot-danger" : "online"}`} />
          <em className={`cw-status-pill${wechatDanger ? " is-danger" : ""}`}>{model.status.wechatState}</em>
        </div>
      </div>
    </section>
  );
}

function maskClientPhone(phone: string) {
  const normalized = phone.replace(/\s+/g, "");
  if (normalized.length < 7) return normalized;
  return `${normalized.slice(0, 3)}****${normalized.slice(-4)}`;
}

function ProcessTaskSummary({
  task,
  statusText,
}: {
  task: WorkerClientModel["task"];
  statusText: string;
}) {
  return (
    <div className="cw-process-task-summary">
      <div>
        <strong>{task.title}</strong>
        <p>{task.customerName} · {maskClientPhone(task.phone)}</p>
      </div>
      <span className={`cw-chip chip ${chipClass(statusText)}`}>{statusText}</span>
    </div>
  );
}

function StepScreenshot({ step }: { step: TimelineStepModel }) {
  if (!step.screenshot?.imageUrl) return null;
  const identifiers = [
    step.screenshot.incidentId ? `故障编号：${step.screenshot.incidentId}` : "",
    step.screenshot.sidecarRunId ? `运行编号：${step.screenshot.sidecarRunId}` : "",
  ].filter(Boolean);
  return (
    <figure className="cw-step-shot step-shot" aria-label="真实执行截图">
      <img className="cw-shot-window shot-window" src={step.screenshot.imageUrl} alt={step.screenshot.caption} />
      <figcaption>{[step.screenshot.caption, ...identifiers].join("·")}</figcaption>
    </figure>
  );
}

function friendlyText(value: string | undefined, fallback = "操作未完成，请稍后重试。") {
  const text = value?.trim();
  if (!text) return fallback;
  const withoutCode = text.replace(/^[A-Z][A-Z0-9_]+\s*[·:：-]\s*/, "");
  if (/^[A-Z][A-Z0-9_]+$/.test(withoutCode)) return fallback;
  return withoutCode;
}

const logEventLabels: Record<string, string> = {
  task_result_reported: "回传任务结果",
  task_failed: "任务执行失败",
  remark_written: "填写客户备注",
  customer_search_started: "开始查找客户",
  worker_started: "开始接单",
  worker_bound: "绑定 Worker",
  worker_bind_failed: "绑定 Worker 失败",
  accept_schedule_changed: "调整自动接单时段",
  client_notice: "客户端提醒",
  incident_exported: "导出故障证据",
  incident_export_failed: "导出故障证据失败",
};

const workerErrorLabels: Record<string, string> = {
  INCIDENT_EXPORT_FAILED: "故障证据导出失败",
  INCIDENT_DIRECTORY_OPEN_FAILED: "无法打开故障证据目录",
  PHONE_NOT_FOUND: "手机号未找到客户",
  WECHAT_WINDOW_NOT_FOUND: "未找到微信窗口",
  RPA_COMPONENT_NOT_READY: "自动化组件不可用",
  SEND_RESULT_UNKNOWN: "发送结果无法确认",
  OTHER: "任务执行异常",
};

function logEventLabel(value: string) {
  return logEventLabels[value] || "本机执行记录";
}

function logLevelLabel(value: string) {
  if (value === "ERROR") return "错误";
  if (value === "WARN" || value === "WARNING") return "提醒";
  return "信息";
}

function workerErrorLabel(value: string) {
  if (!value || value === "-") return "";
  return workerErrorLabels[value] || "执行未完成，请查看操作说明";
}

function formatElapsed(seconds: number) {
  const minutes = Math.floor(seconds / 60).toString().padStart(2, "0");
  const remainder = (seconds % 60).toString().padStart(2, "0");
  return `已运行 ${minutes}:${remainder}`;
}

function useElapsed(active: boolean) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setSeconds((current) => current + 1), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return formatElapsed(seconds);
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
        {step.description ? <p>{friendlyText(step.description, "当前步骤未完成，请稍后重试。")}</p> : null}
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
    const viewportRect = viewport.getBoundingClientRect();
    const markerRect = focusMarker.getBoundingClientRect();
    const nextScrollTop = viewport.scrollTop + markerRect.top + markerRect.height / 2 - (viewportRect.top + viewportRect.height / 2);
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

function CurrentProcess({
  steps,
  state,
  message,
  duration,
  task,
  statusText,
}: {
  steps?: TimelineStepModel[];
  state?: "neutral" | "success" | "error";
  message?: string;
  duration?: string;
  task?: WorkerClientModel["task"];
  statusText?: string;
}) {
  return (
    <section className={`cw-process${task ? " cw-process-with-task" : ""}${state ? ` is-${state}` : ""}`} aria-label="当前运行过程">
      <header className="cw-process-head">
        <span>当前运行过程</span>
        {duration ? <em>{duration}</em> : null}
      </header>
      {task && statusText ? <ProcessTaskSummary task={task} statusText={statusText} /> : null}
      {steps?.length ? (
        <TaskTimeline steps={steps} />
      ) : (
        <div className="cw-process-empty">
          <strong>{message || "等待下一轮检查"}</strong>
        </div>
      )}
    </section>
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
  processText,
  stateText,
  dockState,
  disabled,
  onStartAccepting,
  onPauseAccepting,
}: {
  screen: WorkerClientScreen;
  model: WorkerClientModel;
  processText: string;
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
        <CurrentProcess message={processText} />
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
  const running = statusText === "处理中" || statusText === "接单中" || statusText === "暂停接单";
  const elapsed = useElapsed(running);
  return (
    <section className="cw-screen cw-screen-task screen active" data-screen-view={screen}>
      <div className="cw-workspace workspace">
        <header className="cw-workspace-head workspace-head">
          <ConnectionLine model={model} />
        </header>
        <section className="cw-task-layout task-layout">
          <StatusSummary model={model} stateText={dockState} />
          <CurrentProcess
            steps={steps}
            state={statusText === "失败" ? "error" : statusText === "已完成" ? "success" : "neutral"}
            duration={running ? elapsed : undefined}
            task={model.task}
            statusText={statusText}
          />
        </section>
        <Dock state={dockState} onStartAccepting={onStartAccepting} onPauseAccepting={onPauseAccepting} />
      </div>
    </section>
  );
}

function BackgroundProcessScreen({
  screen,
  model,
  steps,
  dockState = "接单中",
  onStartAccepting,
  onPauseAccepting,
}: {
  screen: WorkerClientScreen;
  model: WorkerClientModel;
  steps: TimelineStepModel[];
  dockState?: WorkerReceiveState;
  onStartAccepting?: () => void;
  onPauseAccepting?: () => void;
}) {
  const active = steps.some((step) => step.state === "current");
  const elapsed = useElapsed(active);
  return (
    <section className="cw-screen cw-screen-task screen active" data-screen-view={screen}>
      <div className="cw-workspace workspace">
        <header className="cw-workspace-head workspace-head">
          <ConnectionLine model={model} />
        </header>
        <section className="cw-task-layout task-layout">
          <StatusSummary model={model} stateText={dockState} />
          <CurrentProcess steps={steps} state="neutral" duration={active ? elapsed : undefined} />
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
  const [showToken, setShowToken] = useState(false);
  const [binding, setBinding] = useState(false);

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
            setBinding(true);
            onBind?.(workerId.trim(), workerToken.trim());
            window.setTimeout(() => setBinding(false), 700);
          }}
        >
          <div>
            <h2>绑定 Worker</h2>
          </div>
          <label>
            <span>Worker ID</span>
            <input className="cw-input" value={workerId} onChange={(event) => setWorkerId(event.target.value)} />
          </label>
          <label>
            <span>Worker Token</span>
            <div className="cw-token-input">
              <input className="cw-input" type={showToken ? "text" : "password"} value={workerToken} onChange={(event) => setWorkerToken(event.target.value)} />
              <button type="button" onClick={() => setShowToken((current) => !current)}>{showToken ? "隐藏" : "显示"}</button>
            </div>
          </label>
          <button className="cw-button cw-button-primary" type="submit" disabled={binding}>{binding ? "绑定中..." : "绑定 Worker"}</button>
          {bindError ? <p className="cw-helper cw-bind-error">{friendlyText(bindError, "绑定失败，请检查 Worker ID、Worker Token 和网络连接后重试。")}</p> : null}
        </form>
      </div>
    </section>
  );
}

function OfflineScreen({ model, hasCurrentOperation = true }: { model: WorkerClientModel; hasCurrentOperation?: boolean }) {
  const offlineModel = {
    ...model,
    status: { ...model.status, connectionState: "连接异常" as const },
  };
  return (
    <section className="cw-screen cw-screen-task screen active" data-screen-view="offline">
      <div className="cw-workspace workspace">
        <header className="cw-workspace-head workspace-head">
          <ConnectionLine model={offlineModel} danger />
        </header>
        {hasCurrentOperation ? (
          <section className="cw-task-layout task-layout">
            <StatusSummary model={offlineModel} />
            <CurrentProcess
              steps={[
                { state: "done", title: "正在执行微信加好友", description: "本机操作保持原状态。" },
                { state: "current", title: "执行已完成，等待恢复后回传", description: "连接恢复后自动回传执行结果。" },
              ]}
              state="error"
              duration="等待连接"
              task={model.task}
              statusText="处理中"
            />
          </section>
        ) : (
          <section className="cw-task-layout task-layout">
            <StatusSummary model={offlineModel} />
            <CurrentProcess message="服务端连接中断，正在尝试恢复" state="error" />
          </section>
        )}
        <Dock state="离线" disabled />
      </div>
    </section>
  );
}

function EnvironmentIssueScreen({
  model,
  type,
}: {
  model: WorkerClientModel;
  type: "automation" | "wechat";
}) {
  const issueModel = {
    ...model,
    status: {
      ...model.status,
      receiveState: "暂停接单" as const,
      automationState: type === "automation" ? "不可用" as const : model.status.automationState,
      wechatState: type === "wechat" ? "未连接" as const : model.status.wechatState,
    },
  };
  return (
    <section className="cw-screen screen active" data-screen-view={`${type}-unavailable`}>
      <div className="cw-workspace workspace">
        <header className="cw-workspace-head workspace-head"><ConnectionLine model={issueModel} /></header>
        <StatusSummary model={issueModel} stateText="暂停接单" />
        <CurrentProcess
          message={type === "automation" ? "自动化组件不可用，暂不领取任务" : "微信未连接，请打开或登录微信"}
          state="error"
        />
        <Dock state="暂停接单" disabled />
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
          <div className="cw-log-head"><span>时间</span><span>结果</span><span>任务</span><span>操作记录 / 故障证据</span></div>
          {model.logs.map((row, index) => (
            <div className="cw-log-row" key={`${row.time}-${index}`}>
              <span>{row.time}</span>
              <strong>{logLevelLabel(row.level)}</strong>
              <span>{row.task}</span>
              <p>
                <b>{logEventLabel(row.event)}</b>
                <span>{friendlyText(row.content, "本机已记录本次操作。")}</span>
                {row.errorCode !== "-" ? <small>error_code：{row.errorCode}（{workerErrorLabel(row.errorCode) || "未归类故障"}）</small> : null}
                {row.incidentId !== "-" ? <small>incident_id：{row.incidentId}</small> : null}
                {row.sidecarRunId !== "-" ? <small>sidecar_run_id：{row.sidecarRunId}</small> : null}
                {row.evidencePath !== "-" ? <small>evidence_path：{row.evidencePath}</small> : null}
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
        processText="已暂停接单"
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
        processText="等待下一轮检查"
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
        processText="当前不在接单时段"
        stateText="暂停接单"
        dockState="暂停接单"
        disabled
      />
    );
  }
  if (screen === "running") return <TaskScreen screen={screen} model={model} steps={model.runningSteps} statusText="处理中" dockState={model.status.receiveState} onStartAccepting={onStartAccepting} onPauseAccepting={onPauseAccepting} />;
  if (screen === "completed") return <TaskScreen screen={screen} model={model} steps={model.completedSteps} statusText="已完成" dockState={model.status.receiveState} onStartAccepting={onStartAccepting} onPauseAccepting={onPauseAccepting} />;
  if (screen === "paused-running") return <TaskScreen screen={screen} model={model} steps={model.runningSteps} statusText="处理中" dockState="暂停接单" onStartAccepting={onStartAccepting} />;
  if (screen === "paused-empty-2") {
    return <TaskScreen screen={screen} model={model} steps={model.completedSteps} statusText="已完成" dockState="暂停接单" onStartAccepting={onStartAccepting} />;
  }
  if (screen === "offline") return <OfflineScreen model={model} />;
  if (screen === "offline-empty") return <OfflineScreen model={model} hasCurrentOperation={false} />;
  if (screen === "automation-unavailable") return <EnvironmentIssueScreen model={model} type="automation" />;
  if (screen === "wechat-disconnected") return <EnvironmentIssueScreen model={model} type="wechat" />;
  if (screen === "failed") return <TaskScreen screen={screen} model={model} steps={model.failedSteps} statusText="失败" dockState={model.status.receiveState} onStartAccepting={onStartAccepting} onPauseAccepting={onPauseAccepting} />;
  if (screen === "scan-running") return <BackgroundProcessScreen screen={screen} model={model} steps={model.scanRunningSteps} dockState={model.status.receiveState} onPauseAccepting={onPauseAccepting} />;
  if (screen === "scan-completed") return <BackgroundProcessScreen screen={screen} model={model} steps={model.scanCompletedSteps} dockState={model.status.receiveState} onPauseAccepting={onPauseAccepting} />;
  if (screen === "target-read-running") return <BackgroundProcessScreen screen={screen} model={model} steps={model.targetReadRunningSteps} dockState={model.status.receiveState} onPauseAccepting={onPauseAccepting} />;
  if (screen === "target-read-completed") return <BackgroundProcessScreen screen={screen} model={model} steps={model.targetReadCompletedSteps} dockState={model.status.receiveState} onPauseAccepting={onPauseAccepting} />;
  if (screen === "ai-reply-running") {
    const replyModel = { ...model, task: { ...model.task, id: "TASK-1842", title: "AI 回复", type: "chat_reply" as const, statusText: "处理中", metaText: "王先生 · 张伟 · 客户咨询续保价格" } };
    return <TaskScreen screen={screen} model={replyModel} steps={model.replyRunningSteps} statusText="处理中" dockState={model.status.receiveState} onPauseAccepting={onPauseAccepting} />;
  }
  if (screen === "ai-reply-completed") {
    const replyModel = { ...model, task: { ...model.task, id: "TASK-1842", title: "AI 回复", type: "chat_reply" as const, statusText: "已完成", metaText: "王先生 · 张伟 · AI 回复已发送" } };
    return <TaskScreen screen={screen} model={replyModel} steps={model.replyCompletedSteps} statusText="已完成" dockState={model.status.receiveState} onPauseAccepting={onPauseAccepting} />;
  }
  if (screen === "ai-reply-failed") {
    const replyModel = { ...model, task: { ...model.task, id: "TASK-1842", title: "AI 回复", type: "chat_reply" as const, statusText: "失败", metaText: "王先生 · 张伟 · 自动发送已终止" } };
    return <TaskScreen screen={screen} model={replyModel} steps={model.replyFailedSteps} statusText="失败" dockState={model.status.receiveState} onPauseAccepting={onPauseAccepting} />;
  }
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
