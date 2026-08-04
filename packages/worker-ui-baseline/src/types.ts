export type WorkerClientScreen =
  | "bind"
  | "paused-empty"
  | "accepting-wait"
  | "schedule-paused"
  | "running"
  | "completed"
  | "paused-running"
  | "paused-empty-2"
  | "offline"
  | "failed"
  | "settings"
  | "schedule-settings"
  | "logs";

export type WorkerReceiveState = "接单中" | "暂停接单" | "离线";
export type WorkerConnectionState = "连接正常" | "连接异常";
export type AutomationState = "可用" | "不可用";
export type WechatState = "已连接" | "未连接";

export type TimelineStepState = "done" | "current" | "error";

export interface WorkerStatusModel {
  sellerName: string;
  receiveState: WorkerReceiveState;
  connectionState: WorkerConnectionState;
  lastHeartbeat: string;
  automationState: AutomationState;
  wechatState: WechatState;
}

export interface WorkerTaskModel {
  id: string;
  title: string;
  statusText: string;
  customerName: string;
  phone: string;
  sellerName: string;
  noteCode: string;
  metaText?: string;
}

export interface TimelineScreenshot {
  searchText: string;
  resultText: string;
  caption: string;
}

export interface TimelineStepModel {
  state: TimelineStepState;
  title: string;
  description?: string;
  time?: string;
  screenshot?: TimelineScreenshot;
  finalText?: string;
}

export interface WorkerLogRow {
  time: string;
  level: string;
  task: string;
  content: string;
  event: string;
  errorCode: string;
  incidentId: string;
  sidecarRunId: string;
  evidencePath: string;
}

export interface WorkerClientModel {
  workerId: string;
  workerToken: string;
  status: WorkerStatusModel;
  task: WorkerTaskModel;
  runningSteps: TimelineStepModel[];
  completedSteps: TimelineStepModel[];
  failedSteps: TimelineStepModel[];
  logs: WorkerLogRow[];
  latestIncident?: {
    incident_id: string;
    evidence_path: string;
    created_at: string;
  } | null;
  schedule: {
    enabled: boolean;
    start: string;
    end: string;
  };
  version: string;
}
