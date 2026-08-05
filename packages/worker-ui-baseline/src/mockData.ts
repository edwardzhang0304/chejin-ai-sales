import type { WorkerClientModel } from "./types";

export const workerClientMock: WorkerClientModel = {
  workerId: "wk_20260611_8f3a2c9b",
  workerToken: "worker-token-value",
  version: "v0.1.0",
  schedule: {
    enabled: false,
    start: "09:00",
    end: "21:00",
  },
  status: {
    sellerName: "张伟",
    receiveState: "接单中",
    connectionState: "连接正常",
    lastHeartbeat: "10:24:18",
    automationState: "可用",
    wechatState: "已连接",
  },
  task: {
    id: "TASK-1831",
    title: "添加通讯录邀请",
    statusText: "接单中",
    customerName: "王先生",
    phone: "13812346678",
    sellerName: "张伟",
    noteCode: "CJ-5739",
    type: "add_friend",
  },
  runningSteps: [
    { state: "done", title: "任务已领取", time: "10:26:10" },
    { state: "done", title: "正在检查运行环境", description: "自动化组件可用，微信已连接。" },
    {
      state: "current",
      title: "正在执行微信加好友",
      description: "Worker 正在调用自动化组件执行当前任务。",
    },
  ],
  completedSteps: [
    { state: "done", title: "任务已领取", time: "10:26:10" },
    { state: "done", title: "环境检查完成", time: "10:26:13" },
    { state: "done", title: "搜索客户", time: "10:26:18" },
    { state: "done", title: "进入添加通讯录流程", time: "10:26:31" },
    { state: "done", title: "写入备注短码与申请说明", time: "10:26:46" },
    {
      state: "done",
      title: "发送添加通讯录邀请",
      time: "10:27:02",
    },
    {
      state: "done",
      title: "回传执行结果",
      time: "10:27:05",
      description: "已发送添加通讯录邀请，不代表客户已同意好友申请。",
      finalText: "任务执行完成",
    },
  ],
  failedSteps: [
    { state: "done", title: "任务已领取", time: "10:26:10" },
    { state: "done", title: "环境检查完成", time: "10:26:13" },
    { state: "done", title: "打开微信桌面客户端", time: "10:26:15" },
    {
      state: "error",
      title: "搜索客户失败",
      description: "手机号未找到客户。",
      finalText: "任务执行失败",
    },
  ],
  scanRunningSteps: [
    { state: "current", title: "正在扫描微信会话第一屏", description: "已运行 00:18" },
  ],
  scanCompletedSteps: [
    { state: "done", title: "扫描微信会话第一屏", time: "10:31:04" },
    { state: "done", title: "扫描完成", description: "发现 12 个会话，命中 3 个目标。", finalText: "等待下一轮检查" },
  ],
  targetReadRunningSteps: [
    { state: "current", title: "正在定位并读取目标会话", description: "正在读取客户 CJ-5739 的最新消息 · 已运行 00:09" },
  ],
  targetReadCompletedSteps: [
    { state: "done", title: "定位目标会话", time: "10:32:16" },
    { state: "done", title: "读取完成", description: "发现 2 条新消息。", finalText: "消息已回传" },
  ],
  replyRunningSteps: [
    { state: "done", title: "读取客户最新消息", time: "10:34:10" },
    { state: "done", title: "等待服务端生成回复", time: "10:34:13" },
    { state: "done", title: "执行发送前复核", time: "10:34:16" },
    { state: "current", title: "正在发送微信消息", description: "Worker 正在发送服务端批准的回复 · 已运行 00:07" },
  ],
  replyCompletedSteps: [
    { state: "done", title: "读取客户最新消息", time: "10:34:10" },
    { state: "done", title: "等待服务端生成回复", time: "10:34:13" },
    { state: "done", title: "执行发送前复核", time: "10:34:16" },
    { state: "done", title: "发送微信消息", time: "10:34:22" },
    { state: "done", title: "确认并回传结果", time: "10:34:25", description: "AI 回复已发送。", finalText: "任务执行完成" },
  ],
  replyFailedSteps: [
    { state: "done", title: "读取客户最新消息", time: "10:34:10" },
    { state: "done", title: "等待服务端生成回复", time: "10:34:13" },
    { state: "done", title: "执行发送前复核", time: "10:34:16" },
    { state: "error", title: "发送微信消息失败", description: "发送结果无法确认，自动发送已终止。", finalText: "任务执行失败" },
  ],
  logs: [
    { time: "10:27:12", level: "ERROR", task: "TASK-1831", content: "微信窗口定位失败。", event: "task_failed", errorCode: "WECHAT_WINDOW_NOT_FOUND", incidentId: "INC-20260805-001", sidecarRunId: "message-20260805-001", evidencePath: "artifacts/incidents/INC-20260805-001.zip" },
    { time: "10:27:05", level: "INFO", task: "TASK-1831", content: "回传执行结果。", event: "task_result_reported", errorCode: "-", incidentId: "-", sidecarRunId: "-", evidencePath: "-" },
    { time: "10:26:46", level: "INFO", task: "TASK-1831", content: "写入备注短码。", event: "remark_written", errorCode: "-", incidentId: "-", sidecarRunId: "-", evidencePath: "-" },
    { time: "10:26:18", level: "INFO", task: "TASK-1831", content: "搜索客户。", event: "customer_search_started", errorCode: "-", incidentId: "-", sidecarRunId: "-", evidencePath: "-" },
    { time: "10:21:02", level: "INFO", task: "-", content: "开始接单。", event: "worker_started", errorCode: "-", incidentId: "-", sidecarRunId: "-", evidencePath: "-" },
  ],
};
