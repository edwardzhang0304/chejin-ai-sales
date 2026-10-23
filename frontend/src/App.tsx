import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { BarChart3, UsersRound, CarFront, BookOpen, ContactRound, Monitor, ListTodo, type LucideIcon } from "lucide-react";

import { LoginPage } from "./features/auth/LoginPage";
import { EmptyWorkspace } from "./features/home/EmptyWorkspace";
import { LeadsPage } from "./features/leads/LeadsPage";
import { KnowledgePage } from "./features/knowledge/KnowledgePage";
import { SalesPage } from "./features/sales/SalesPage";
import { TasksPage } from "./features/tasks/TasksPage";
import { VehiclesPage } from "./features/vehicles/VehiclesPage";
import { WorkersPage } from "./features/workers/WorkersPage";
import { getAuthSession, logout } from "./shared/api/auth";
import type { AuthSession } from "./shared/api/auth";
import { ApiError, onUnauthorized, runtimeConfig } from "./shared/api/client";
import { ChevronRightIcon } from "./shared/ui/Icons";

const DashboardPage = lazy(() => import("./features/dashboard/DashboardPage").then(module => ({ default: module.DashboardPage })));

type ModuleKey = "dashboard" | "leads" | "vehicles" | "knowledge" | "sales" | "workers" | "tasks";

const modules: Array<{ key: ModuleKey; label: string; icon: LucideIcon }> = [
  { key: "dashboard", label: "数据看板", icon: BarChart3 },
  { key: "leads", label: "线索管理", icon: UsersRound },
  { key: "vehicles", label: "车辆管理", icon: CarFront },
  { key: "knowledge", label: "知识管理", icon: BookOpen },
  { key: "sales", label: "销售管理", icon: ContactRound },
  { key: "workers", label: "Worker 管理", icon: Monitor },
  { key: "tasks", label: "任务中心", icon: ListTodo },
];

type AuthState = "checking" | "authenticated" | "unauthenticated";

function initialAuditModule(): ModuleKey | null {
  if (!import.meta.env.DEV) return null;
  const params = new URLSearchParams(window.location.search);
  if (params.get("ui-audit") !== "1") return null;
  const candidate = params.get("module") as ModuleKey | null;
  return modules.some((item) => item.key === candidate) ? candidate : null;
}

export function App() {
  const [activeModule, setActiveModule] = useState<ModuleKey | null>(initialAuditModule);
  const [workerOpenIntent, setWorkerOpenIntent] = useState<{ workerId: string; nonce: number } | null>(null);
  const [salesOpenIntent, setSalesOpenIntent] = useState<{ salesId: string; editing: boolean; focusWorker: boolean; nonce: number } | null>(null);
  const [authSession, setAuthSession] = useState<AuthSession | null>(null);
  const [authState, setAuthState] = useState<AuthState>("checking");
  const [loginMessage, setLoginMessage] = useState<string | null>(null);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const accountAreaRef = useRef<HTMLDivElement | null>(null);

  function clearWorkspaceState() {
    setActiveModule(null);
    setWorkerOpenIntent(null);
    setSalesOpenIntent(null);
    setAccountMenuOpen(false);
    setLogoutError(null);
  }

  function showLogin(message: string | null) {
    clearWorkspaceState();
    setAuthSession(null);
    setLoginMessage(message);
    setAuthState("unauthenticated");
  }

  useEffect(() => {
    const controller = new AbortController();
    window.localStorage.removeItem("chejin_admin_token");
    void getAuthSession(controller.signal)
      .then((session) => {
        setAuthSession(session);
        setAuthState("authenticated");
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        showLogin(null);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => onUnauthorized(() => showLogin("登录已失效，请重新登录")), []);

  useEffect(() => {
    if (!accountMenuOpen) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (!accountAreaRef.current?.contains(event.target as Node)) setAccountMenuOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setAccountMenuOpen(false);
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [accountMenuOpen]);

  async function handleLogout() {
    if (loggingOut) return;
    setLoggingOut(true);
    setLogoutError(null);
    try {
      const result = await logout();
      if (!result.logged_out) {
        throw new Error("logout not confirmed");
      }
      showLogin(null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        showLogin(null);
        return;
      }
      setLogoutError("退出失败，请重试。");
      setAccountMenuOpen(true);
    } finally {
      setLoggingOut(false);
    }
  }

  if (authState === "checking") {
    return <main className="auth-checking" aria-label="正在验证登录状态"><span className="login-spinner" aria-hidden="true" /></main>;
  }

  if (authState === "unauthenticated" || !authSession) {
    return (
      <LoginPage
        message={loginMessage}
        onAuthenticated={(session) => {
          clearWorkspaceState();
          setLoginMessage(null);
          setAuthSession(session);
          setAuthState("authenticated");
        }}
      />
    );
  }

  const accountLabel = authSession.operator_name.trim() || "运营账号";
  const accountAvatar = accountLabel.slice(0, 1) || "运";

  return (
    <main className="app-shell glass-admin">
      <aside className="sidebar" aria-label="运营后台导航">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">
            车
          </div>
          <div>
            <strong>车金 AI</strong>
            <span>运营后台 · v{import.meta.env.VITE_PRODUCT_VERSION}{runtimeConfig.readOnly ? " · 联调只读" : ""}</span>
          </div>
        </div>

        <nav className="module-nav">
          {modules.map((item) => (
            <button
              key={item.key}
              aria-label={item.label}
              title={item.label}
              type="button"
              className={activeModule === item.key ? "module-item active" : "module-item"}
              onClick={() => setActiveModule(item.key)}
            >
              <item.icon size={21} aria-hidden="true" /><span>{item.label}</span>
            </button>
          ))}
        </nav>

        <div className="account-area" ref={accountAreaRef}>
          {accountMenuOpen ? (
            <div className="account-menu" role="menu">
              {logoutError ? <p className="account-menu-error" role="alert">{logoutError}</p> : null}
              <button type="button" role="menuitem" disabled={loggingOut} onClick={() => void handleLogout()}>
                {loggingOut ? "退出中..." : "退出登录"}
              </button>
            </div>
          ) : null}
          <button
            className="account-button"
            type="button"
            aria-expanded={accountMenuOpen}
            aria-haspopup="menu"
            onClick={() => setAccountMenuOpen((open) => !open)}
          >
            <span className="account-avatar" aria-hidden="true">{accountAvatar}</span>
            <span className="account-copy">
              <strong title={accountLabel}>{accountLabel}</strong>
              <small>{runtimeConfig.readOnly ? "只读查看" : "全部功能权限"}</small>
            </span>
            <ChevronRightIcon />
          </button>
        </div>

      </aside>

      <section className="workspace">
        {activeModule === "dashboard" ? <Suspense fallback={<div className="state-box">正在加载数据看板...</div>}><DashboardPage /></Suspense> : null}
        {activeModule === "leads" ? <LeadsPage /> : null}
        {activeModule === "vehicles" ? <VehiclesPage /> : null}
        {activeModule === "knowledge" ? <KnowledgePage /> : null}
        {activeModule === "sales" ? <SalesPage openIntent={salesOpenIntent} /> : null}
        {activeModule === "workers" ? <WorkersPage openIntent={workerOpenIntent} /> : null}
        {activeModule === "tasks" ? (
          <TasksPage
            onOpenWorker={(workerId) => {
              setWorkerOpenIntent({ workerId, nonce: Date.now() });
              setActiveModule("workers");
            }}
            onOpenSalesWorkerBinding={(salesId) => {
              setSalesOpenIntent({ salesId, editing: true, focusWorker: true, nonce: Date.now() });
              setActiveModule("sales");
            }}
          />
        ) : null}
        {activeModule === null ? <EmptyWorkspace activeModule={activeModule} /> : null}
      </section>
    </main>
  );
}
