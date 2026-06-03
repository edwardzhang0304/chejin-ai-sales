import { useState } from "react";

import { EmptyWorkspace } from "./features/home/EmptyWorkspace";
import { LeadsPage } from "./features/leads/LeadsPage";
import { LogsPage } from "./features/logs/LogsPage";
import { SalesPage } from "./features/sales/SalesPage";

type ModuleKey = "leads" | "sales" | "logs";

const modules: Array<{ key: ModuleKey; label: string }> = [
  { key: "leads", label: "线索管理" },
  { key: "sales", label: "销售管理" },
  { key: "logs", label: "操作日志" },
];

export function App() {
  const [activeModule, setActiveModule] = useState<ModuleKey | null>(null);

  return (
    <main className="app-shell">
      <aside className="sidebar" aria-label="运营后台模块">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">
            车
          </div>
          <div>
            <strong>车金 AI</strong>
            <span>运营后台</span>
          </div>
        </div>

        <nav className="module-nav">
          {modules.map((item) => (
            <button
              key={item.key}
              type="button"
              className={activeModule === item.key ? "module-item active" : "module-item"}
              onClick={() => setActiveModule(item.key)}
            >
              <span>{item.label}</span>
            </button>
          ))}
        </nav>

        <div className="phase-card">
          <span>当前阶段</span>
          <strong>人工录入 / 去重 / 轮询分配</strong>
          <p>暂不开放 RPA 加好友、AI 自动回复、销售登录。</p>
        </div>
      </aside>

      <section className="workspace">
        {activeModule === "leads" ? <LeadsPage /> : null}
        {activeModule === "sales" ? <SalesPage /> : null}
        {activeModule === "logs" ? <LogsPage /> : null}
        {activeModule === null ? <EmptyWorkspace activeModule={activeModule} /> : null}
      </section>
    </main>
  );
}
