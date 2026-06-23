import React, { useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { WorkerClientBaseline } from "./src/WorkerClientBaseline";
import { workerClientMock } from "./src/mockData";
import type { WorkerClientModel, WorkerClientScreen } from "./src/types";

const screens: Array<{ id: WorkerClientScreen; label: string }> = [
  { id: "bind", label: "1. 首次绑定页" },
  { id: "paused-empty", label: "2. 暂停接单 + 无任务" },
  { id: "accepting-wait", label: "3. 接单中 + 等待任务" },
  { id: "schedule-paused", label: "4. 非接单时段 + 无任务" },
  { id: "running", label: "5. 执行任务中" },
  { id: "completed", label: "6. 任务执行完成" },
  { id: "paused-running", label: "7. 暂停接单 + 有任务执行中" },
  { id: "paused-empty-2", label: "8. 暂停接单 + 无任务" },
  { id: "offline", label: "9. 服务端不可达 / 离线" },
  { id: "failed", label: "10. 任务执行失败" },
  { id: "settings", label: "11. 设置页" },
  { id: "schedule-settings", label: "12. 接单时段设置页" },
  { id: "logs", label: "13. 本机执行日志明细页" },
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
          {screens.map((item) => (
            <button
              className={item.id === screen ? "active" : ""}
              key={item.id}
              type="button"
              onClick={() => changeScreen(item.id)}
            >
              {item.label}
            </button>
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
