import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { VehicleImportPreview, VehicleImportResult } from "../types";

const api = vi.hoisted(() => ({
  previewVehicleImport: vi.fn(),
  confirmVehicleImport: vi.fn(),
  downloadVehicleTemplate: vi.fn(),
}));

vi.mock("../api", () => api);

import { VehicleImportModal } from "./VehicleImportModal";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function uploadXlsx(container: HTMLElement) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(["xlsx"], "车辆导入.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
  fireEvent.change(input, { target: { files: [file] } });
}

function preview(overrides: Partial<VehicleImportPreview> = {}): VehicleImportPreview {
  return {
    preview_id: "preview-001",
    status: "pending",
    expires_at: "2026-08-05T22:00:00Z",
    total_rows: 1,
    create_count: 1,
    update_count: 0,
    error_count: 0,
    can_confirm: true,
    rows: [{ row_number: 2, vehicle_code: "CJ-PREVIEW-001", action: "create", data: { display_name: "导入车辆" }, errors: [] }],
    ...overrides,
  };
}

describe("车辆 Excel 三步导入", () => {
  it("逐条展示位置、问题和解决办法，并阻止错误数据确认", async () => {
    api.previewVehicleImport.mockResolvedValue(preview({
      create_count: 0,
      error_count: 1,
      can_confirm: false,
      rows: [{ row_number: 12, vehicle_code: "CJ-NOT-EXISTS", action: "update", data: {}, errors: ["车辆编号不存在；如需新增请清空车辆编号，由系统生成"] }],
    }));
    const { container } = render(<VehicleImportModal open onClose={vi.fn()} onImported={vi.fn()} />);

    uploadXlsx(container);

    expect(await screen.findByText("第 12 行 · 车辆 CJ-NOT-EXISTS")).toBeTruthy();
    expect(screen.getByText("车辆编号不存在；如需新增请清空车辆编号，由系统生成")).toBeTruthy();
    expect(screen.getByText("修正该行内容后重新上传并校验")).toBeTruthy();
    expect((screen.getByRole("button", { name: "确认导入" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("校验通过后确认整批导入并展示新增、更新结果", async () => {
    const validPreview = preview({ total_rows: 2, create_count: 1, update_count: 1 });
    const result: VehicleImportResult = { ...validPreview, status: "confirmed", duplicated: false, imported_count: 2 };
    api.previewVehicleImport.mockResolvedValue(validPreview);
    api.confirmVehicleImport.mockResolvedValue(result);
    const onImported = vi.fn();
    const { container } = render(<VehicleImportModal open onClose={vi.fn()} onImported={onImported} />);

    uploadXlsx(container);
    const confirm = await screen.findByRole("button", { name: "确认导入" });
    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(confirm);

    expect(await screen.findByText("车辆导入完成")).toBeTruthy();
    expect(screen.getByText("新增 1 辆，更新 1 辆。")).toBeTruthy();
    await waitFor(() => expect(onImported).toHaveBeenCalledTimes(1));
  });

  it("导入写入成功但列表刷新失败时仍展示导入完成", async () => {
    const validPreview = preview();
    api.previewVehicleImport.mockResolvedValue(validPreview);
    api.confirmVehicleImport.mockResolvedValue({ ...validPreview, status: "confirmed", duplicated: false, imported_count: 1 });
    const onImported = vi.fn().mockRejectedValue(new Error("refresh failed"));
    const { container } = render(<VehicleImportModal open onClose={vi.fn()} onImported={onImported} />);

    uploadXlsx(container);
    fireEvent.click(await screen.findByRole("button", { name: "确认导入" }));

    expect(await screen.findByText("车辆导入完成")).toBeTruthy();
    expect(await screen.findByText("操作已经成功，但页面刷新失败，请手动刷新。")).toBeTruthy();
    expect(screen.queryByText("整批导入失败")).toBeNull();
  });
});
