import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import type { WorkerItem } from "./types";

const api = vi.hoisted(() => ({
  createWorker: vi.fn(),
  getWorker: vi.fn(),
  listWorkers: vi.fn(),
  resetWorkerBinding: vi.fn(),
  updateWorker: vi.fn(),
}));

vi.mock("./api", () => api);

import { WorkersPage } from "./WorkersPage";


afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});


it("does not present a faulted online worker as idle", async () => {
  const worker: WorkerItem = {
    id: "worker-faulted",
    worker_name: "Windows 实机",
    device_name: "Windows UAT",
    platform: "windows",
    enabled: true,
    online_status: "online",
    run_status: "faulted",
    running_status: "idle",
    current_task: null,
    last_heartbeat_at: "2026-08-24T00:00:00+08:00",
    client_binding_state: "bound",
    remark: null,
    bound_sales_id: null,
    bound_sales_name: null,
  };
  api.listWorkers.mockResolvedValue({ items: [worker] });

  render(<WorkersPage />);

  const row = await screen.findByRole("row", { name: /Windows 实机/ });
  expect(row.textContent).toContain("客户端故障");
  expect(row.textContent).not.toContain("在线 / 空闲");
});
