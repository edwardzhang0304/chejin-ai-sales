import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

beforeEach(() => {
  Object.defineProperty(window, "scrollTo", {
    value: vi.fn(),
    writable: true,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("销售手机号与飞书绑定合同", () => {
  it("新增带格式空白的手机号显示并提交完整11位，不截断或修复非法字符", async () => {
    const onSubmit = vi.fn().mockResolvedValue(false);
    render(<CreateSalesModal submitting={false} error={null} workerOptions={[]} onClose={vi.fn()} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByPlaceholderText("请输入销售姓名"), { target: { value: "格式测试" } });
    const phone = screen.getByPlaceholderText("请输入 11 位手机号") as HTMLInputElement;
    fireEvent.change(phone, { target: { value: "139\u30000000\u00a00001" } });
    expect(phone.value).toBe("13900000001");
    expect(phone.maxLength).toBe(-1);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0].phone).toBe("13900000001");
    fireEvent.change(phone, { target: { value: "139x00000001" } });
    expect(phone.value).toBe("139x00000001");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("编辑手机号去格式空白并提交，不误用脱敏号码", async () => {
    api.listSales.mockResolvedValue({ items: [sales] });
    api.getSales.mockResolvedValue(sales);
    api.updateSales.mockResolvedValue({ id: sales.id });
    workerApi.listWorkers.mockResolvedValue({ items: [] });
    render(<SalesPage />);
    fireEvent.click(await screen.findByRole("row", { name: /张伟/ }));
    fireEvent.click(await screen.findByRole("button", { name: "编辑销售" }));
    const phone = screen.getByPlaceholderText("不修改请留空；修改请输入完整手机号") as HTMLInputElement;
    fireEvent.change(phone, { target: { value: "138 0000 0002" } });
    expect(phone.value).toBe("13800000002");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.updateSales).toHaveBeenCalledTimes(1));
    expect(api.updateSales.mock.calls[0][1].phone).toBe("13800000002");
  });

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

    fireEvent.click(await screen.findByRole("row", { name: /张伟/ }));
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
