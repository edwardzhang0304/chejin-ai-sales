import { useEffect } from "react";

/** Refresh visible read views without overlapping requests or reloading the page. */
export function useAutoRefresh(refresh: (signal: AbortSignal) => Promise<unknown>, enabled = true) {
  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let request: AbortController | null = null;
    const schedule = () => { timer = setTimeout(() => void run(), 5000); };
    async function run() {
      clearTimeout(timer);
      if (disposed || request) return;
      if (document.visibilityState === "hidden") { schedule(); return; }
      const controller = new AbortController();
      request = controller;
      try { await refresh(controller.signal); }
      catch { /* Background failures retain the last successful view. */ }
      finally {
        request = null;
        if (!disposed) schedule();
      }
    }
    const resume = () => { if (document.visibilityState !== "hidden") void run(); };
    const visibility = () => {
      if (document.visibilityState === "hidden") request?.abort();
      else resume();
    };
    schedule();
    window.addEventListener("focus", resume);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      disposed = true;
      clearTimeout(timer);
      request?.abort();
      window.removeEventListener("focus", resume);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [refresh, enabled]);
}
