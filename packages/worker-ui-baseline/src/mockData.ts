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
  },
  runningSteps: [
    { state: "done", title: "任务已领取", time: "10:26:10" },
    { state: "done", title: "环境检查完成", description: "自动化组件可用，微信已连接。" },
    { state: "done", title: "打开微信桌面客户端", time: "10:26:15" },
    {
      state: "current",
      title: "正在搜索客户",
      description: "按手机号 13812346678 搜索。",
      screenshot: {
        searchText: "13812346678",
        resultText: "正在搜索",
        caption: "当前截图 · 微信搜索结果",
      },
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
      screenshot: {
        searchText: "申请说明已填写",
        resultText: "添加通讯录邀请已发送",
        caption: "截图 · 邀请发送结果",
      },
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
      description: "PHONE_NOT_FOUND / 手机号未找到客户。",
      screenshot: {
        searchText: "13812346678",
        resultText: "未找到匹配联系人",
        caption: "截图 · 搜索失败结果",
      },
      finalText: "任务执行失败",
    },
  ],
  logs: [
    { time: "10:27:05", level: "INFO", task: "TASK-1831", content: "回传执行结果。" },
    { time: "10:26:46", level: "INFO", task: "TASK-1831", content: "写入备注短码。" },
    { time: "10:26:18", level: "INFO", task: "TASK-1831", content: "搜索客户。" },
    { time: "10:21:02", level: "INFO", task: "-", content: "开始接单。" },
  ],
};

