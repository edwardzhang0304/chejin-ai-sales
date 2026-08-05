import React, { useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { WorkerClientBaseline } from "./src/WorkerClientBaseline";
import { workerClientMock } from "./src/mockData";
import type { WorkerClientModel, WorkerClientScreen } from "./src/types";

const screens: Array<{ id: WorkerClientScreen; label: string; group: "验收场景" | "运行过程扩展" }> = [
  { id: "bind", label: "1. 首次绑定页", group: "验收场景" },
  { id: "paused-empty", label: "2. 暂停接单 + 等待", group: "验收场景" },
  { id: "accepting-wait", label: "3. 接单中 + 等待", group: "验收场景" },
  { id: "schedule-paused", label: "4. 非接单时段", group: "验收场景" },
  { id: "running", label: "5. 加好友执行中", group: "验收场景" },
  { id: "completed", label: "6. 加好友完成", group: "验收场景" },
  { id: "paused-running", label: "7. 暂停接单 + 执行中", group: "验收场景" },
  { id: "paused-empty-2", label: "8. 暂停接单 + 结果保留", group: "验收场景" },
  { id: "offline", label: "9. 服务端离线 + 当前操作", group: "验收场景" },
  { id: "offline-empty", label: "10. 服务端离线 + 无操作", group: "验收场景" },
  { id: "automation-unavailable", label: "11. 自动化组件不可用", group: "验收场景" },
  { id: "wechat-disconnected", label: "12. 微信未连接", group: "验收场景" },
  { id: "failed", label: "13. 加好友失败", group: "验收场景" },
  { id: "settings", label: "14. 设置页", group: "验收场景" },
  { id: "schedule-settings", label: "15. 接单时段设置", group: "验收场景" },
  { id: "logs", label: "16. 本机执行日志", group: "验收场景" },
  { id: "scan-running", label: "主动扫描中", group: "运行过程扩展" },
  { id: "scan-completed", label: "主动扫描完成", group: "运行过程扩展" },
  { id: "target-read-running", label: "定向读取中", group: "运行过程扩展" },
  { id: "target-read-completed", label: "定向读取完成", group: "运行过程扩展" },
  { id: "ai-reply-running", label: "AI 回复执行中", group: "运行过程扩展" },
  { id: "ai-reply-completed", label: "AI 回复完成", group: "运行过程扩展" },
  { id: "ai-reply-failed", label: "AI 回复失败", group: "运行过程扩展" },
];

function initialScreen(): WorkerClientScreen {
  const screen = new URLSearchParams(window.location.search).get("screen");
  if (screens.some((item) => item.id === screen)) return screen as WorkerClientScreen;
  return "bind";
}

function App() {
  const [screen, setScreen] = useState<WorkerClientScreen>(initialScreen);
  const [model, setModel] = useState<WorkerClientModel>(workerClientMock);
  const activeLabel = useMemo(() => screens.find((item) => item.id === screen)?.label || "", [screen]);

  function changeScreen(next: WorkerClientScreen) {
    setScreen(next);
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("screen", next);
    window.history.replaceState({}, "", nextUrl);
  }

  return (
    <main className="preview-shell">
      <aside className="preview-panel">
        <p>React 组件基准包</p>
        <h1>Worker UI 组件化预览</h1>
        <small>
          右侧不是原静态稿，而是 `WorkerClientBaseline` 组件渲染结果。当前场景：{activeLabel}
        </small>
        <nav className="preview-nav" aria-label="页面场景">
          {["验收场景", "运行过程扩展"].map((group) => (
            <section className="preview-nav-group" key={group}>
              <strong>{group}</strong>
              {screens.filter((item) => item.group === group).map((item) => (
                <button
                  className={item.id === screen ? "active" : ""}
                  key={item.id}
                  type="button"
                  onClick={() => changeScreen(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </section>
          ))}
        </nav>
      </aside>

      <section className="preview-stage">
        <WorkerClientBaseline
          screen={screen}
          model={model}
          onScreenChange={changeScreen}
          onStartAccepting={() => changeScreen("accepting-wait")}
          onPauseAccepting={() => changeScreen("paused-empty")}
          onUpdateAcceptSchedule={(enabled, start, end) => {
            setModel((current) => ({
              ...current,
              schedule: { enabled, start, end },
            }));
          }}
        />
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
