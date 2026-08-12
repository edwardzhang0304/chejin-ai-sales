import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SalesItem } from "./types";

const api = vi.hoisted(() => ({
  createSales: vi.fn(),
  getSales: vi.fn(),
  listSales: vi.fn(),
  updateSales: vi.fn(),
}));
const workerApi = vi.hoisted(() => ({ listWorkers: vi.fn() }));

vi.mock("./api", () => api);
vi.mock("../workers/api", () => workerApi);

import { SalesPage } from "./SalesPage";
import { CreateSalesModal } from "./components/CreateSalesModal";

const sales: SalesItem = {
  id: "sales-1",
  sales_name: "张伟",
  phone: "139****0001",
  wechat: "zhangwei",
  feishu_binding_status: "matched",
  worker_id: null,
  enabled: true,
  sort_order: 1,
  remark: null,
  lead_count: 0,
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("销售手机号与飞书绑定合同", () => {
  it("新增销售必须提交完整手机号且不包含任何飞书 ID", async () => {
    const onSubmit = vi.fn().mockResolvedValue(true);
    render(
      <CreateSalesModal
        submitting={false}
        error={null}
        workerOptions={[]}
        onClose={vi.fn()}
        onSubmit={onSubmit}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("请输入销售姓名"), {
      target: { value: "张伟" },
    });
    fireEvent.change(screen.getByPlaceholderText("请输入 11 位手机号"), {
      target: { value: "13900000001" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const payload = onSubmit.mock.calls[0][0];
    expect(payload.phone).toBe("13900000001");
    expect(payload).not.toHaveProperty("feishu_user_id");
    expect(payload).not.toHaveProperty("open_id");
  });

  it("修改其他资料不会回传后端脱敏手机号", async () => {
    api.listSales.mockResolvedValue({ items: [sales] });
    api.getSales.mockResolvedValue(sales);
    api.updateSales.mockResolvedValue({ id: sales.id });
    workerApi.listWorkers.mockResolvedValue({ items: [] });
    render(<SalesPage />);

    expect(await screen.findByText("飞书已匹配")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "编辑销售" }));
    fireEvent.change(screen.getByDisplayValue("张伟"), {
      target: { value: "张伟（华东）" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(api.updateSales).toHaveBeenCalledTimes(1));
    const payload = api.updateSales.mock.calls[0][1];
    expect(payload.sales_name).toBe("张伟（华东）");
    expect(payload).not.toHaveProperty("phone");
    expect(JSON.stringify(payload)).not.toContain("139****0001");
  });
});
