const screenTitles = {
  bind: "首次绑定页",
  "paused-empty": "工作台首页：暂停接单 + 无任务",
  "accepting-wait": "工作台首页：接单中 + 等待任务",
  "schedule-paused": "工作台首页：非接单时段 + 无任务",
  running: "执行任务中：任务链路展示",
  completed: "任务执行完成",
  "paused-running": "暂停接单 + 有任务执行中",
  "paused-empty-2": "暂停接单 + 无任务",
  offline: "服务端不可达 / 离线状态",
  failed: "任务执行失败",
  settings: "设置页",
  "schedule-settings": "接单时段设置页",
  logs: "本机执行日志明细页",
};

const screenTitle = document.querySelector("[data-screen-title]");
const clientTitle = document.querySelector("[data-client-title]");
const appWindow = document.querySelector(".app-window");
const screens = Array.from(document.querySelectorAll("[data-screen-view]"));
const scenarioButtons = Array.from(document.querySelectorAll("[data-screen]"));
const clientBack = document.querySelector("[data-client-back]");
const navWorkbench = document.querySelector(".nav-workbench");
const navSettings = document.querySelector(".nav-settings");

function currentScreenName() {
  return new URLSearchParams(window.location.search).get("screen") || "bind";
}

function alignFocusedStep(screenName) {
  const activeScreen = document.querySelector(
    `.screen[data-screen-view="${screenName}"]`,
  );
  const viewport = activeScreen?.querySelector(".chain-viewport");
  const focusStep = activeScreen?.querySelector(".final-step, .current, .error");
  const timelineCard = activeScreen?.querySelector(".timeline-card.focus-chain");
  if (!viewport || !focusStep) return;

  const focusMarker = focusStep.querySelector(":scope > span") || focusStep;
  const cardRect = (timelineCard || viewport).getBoundingClientRect();
  const markerRect = focusMarker.getBoundingClientRect();
  const markerCenter = markerRect.top + markerRect.height / 2;
  const cardCenter = cardRect.top + cardRect.height / 2;
  const maxScrollTop = viewport.scrollHeight - viewport.clientHeight;
  const nextScrollTop = viewport.scrollTop + markerCenter - cardCenter;
  viewport.scrollTop = Math.max(0, Math.min(maxScrollTop, nextScrollTop));
}

function showScreen(screenName) {
  appWindow?.setAttribute("data-active-screen", screenName);
  screens.forEach((screen) => {
    screen.classList.toggle("active", screen.dataset.screenView === screenName);
  });

  scenarioButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.screen === screenName);
  });

  if (screenTitle) {
    screenTitle.textContent = screenTitles[screenName] || "车金 Worker 客户端";
  }

  if (clientTitle) {
    if (screenName === "settings") clientTitle.textContent = "设置";
    else if (screenName === "schedule-settings")
      clientTitle.textContent = "接单时段设置";
    else if (screenName === "logs") clientTitle.textContent = "本机执行日志";
    else clientTitle.textContent = "Worker 工作台";
  }

  const settingsActive =
    screenName === "settings" ||
    screenName === "schedule-settings" ||
    screenName === "logs";
  navWorkbench?.classList.toggle("active", !settingsActive);
  navSettings?.classList.toggle("active", settingsActive);

  const nextUrl = new URL(window.location.href);
  nextUrl.searchParams.set("screen", screenName);
  window.history.replaceState({}, "", nextUrl);

  alignFocusedStep(screenName);
  window.setTimeout(() => alignFocusedStep(screenName), 80);
}

scenarioButtons.forEach((button) => {
  button.addEventListener("click", () => showScreen(button.dataset.screen));
});

document.querySelectorAll("[data-jump]").forEach((button) => {
  button.addEventListener("click", () => showScreen(button.dataset.jump));
});

clientBack?.addEventListener("click", () => {
  const screenName = currentScreenName();
  if (screenName === "schedule-settings" || screenName === "logs") {
    showScreen("settings");
  } else {
    showScreen("paused-empty");
  }
});

navWorkbench?.addEventListener("click", () => showScreen("paused-empty"));
navSettings?.addEventListener("click", () => showScreen("settings"));

const initialScreen = new URLSearchParams(window.location.search).get("screen");
if (initialScreen && screenTitles[initialScreen]) {
  showScreen(initialScreen);
}

window.addEventListener("load", () => {
  const screenName = currentScreenName();
  if (!screenTitles[screenName]) return;
  alignFocusedStep(screenName);
  window.setTimeout(() => alignFocusedStep(screenName), 120);
});
