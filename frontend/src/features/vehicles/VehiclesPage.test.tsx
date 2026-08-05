import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { VehicleItem } from "./types";

const vehicles = vi.hoisted(() => [
  {
    vehicle_code: "CJ-001",
    display_name: "第一辆",
    brand: "测试品牌",
    series: "A",
    model: null,
    public_price: 100000,
    first_registration: "2025-01",
    mileage_km: 1000,
    exterior_color: null,
    interior_color: null,
    location: null,
    customer_description: null,
    vin: null,
    plate_number: null,
    purchase_price: null,
    internal_notes: null,
    listing_status: "unlisted",
    images: [],
    main_image: null,
    created_at: "2026-08-05T00:00:00Z",
    updated_at: "2026-08-05T00:00:00Z",
  },
  {
    vehicle_code: "CJ-002",
    display_name: "第二辆",
    brand: "测试品牌",
    series: "B",
    model: null,
    public_price: 120000,
    first_registration: "2025-02",
    mileage_km: 2000,
    exterior_color: null,
    interior_color: null,
    location: null,
    customer_description: null,
    vin: null,
    plate_number: null,
    purchase_price: null,
    internal_notes: null,
    listing_status: "unlisted",
    images: [],
    main_image: null,
    created_at: "2026-08-05T00:00:00Z",
    updated_at: "2026-08-05T00:00:00Z",
  },
] as VehicleItem[]);
const vehicleApiState = vi.hoisted(() => ({ empty: false }));

vi.mock("./api", () => ({
  createVehicle: vi.fn(),
  listVehicles: vi.fn(async () => ({ items: vehicleApiState.empty ? [] : vehicles, page: 1, page_size: 20, total: vehicleApiState.empty ? 0 : vehicles.length })),
  getVehicle: vi.fn(async (code: string) => vehicles.find((vehicle) => vehicle.vehicle_code === code)),
}));

vi.mock("./components/AuthenticatedVehicleImage", () => ({
  AuthenticatedVehicleImage: () => null,
}));

vi.mock("./components/VehicleDetailDrawer", () => ({
  VehicleDetailDrawer: ({ vehicle, onDirtyChange, onClose }: {
    vehicle: VehicleItem | null;
    onDirtyChange: (dirty: boolean) => void;
    onClose: () => void;
  }) => (
    <aside aria-label="车辆详情">
      <span>{vehicle?.vehicle_code}</span>
      <button type="button" onClick={() => onDirtyChange(true)}>模拟未保存修改</button>
      <button type="button" onClick={onClose}>关闭车辆详情</button>
    </aside>
  ),
}));

vi.mock("./components/CreateVehicleModal", () => ({
  CreateVehicleModal: ({ open }: { open: boolean }) => open ? <section role="dialog" aria-label="新增车辆弹窗" /> : null,
}));

vi.mock("./components/VehicleImportModal", () => ({
  VehicleImportModal: ({ open }: { open: boolean }) => open ? <section role="dialog" aria-label="导入车辆弹窗" /> : null,
}));

import { VehiclesPage } from "./VehiclesPage";

afterEach(() => {
  cleanup();
  vehicleApiState.empty = false;
});

async function openDirtyVehicle() {
  render(<VehiclesPage />);
  const firstVehicle = await screen.findByTitle("第一辆");
  fireEvent.click(firstVehicle.closest("tr") as HTMLTableRowElement);
  await screen.findByText("CJ-001");
  fireEvent.click(screen.getByRole("button", { name: "模拟未保存修改" }));
}

describe("车辆详情未保存拦截", () => {
  it("关闭详情前必须确认放弃修改", async () => {
    await openDirtyVehicle();

    fireEvent.click(screen.getByRole("button", { name: "关闭车辆详情" }));
    expect(screen.getByRole("alertdialog", { name: "放弃未保存的修改？" })).toBeTruthy();
    expect(screen.getByRole("complementary", { name: "车辆详情" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "放弃并关闭" }));
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "车辆详情" })).toBeNull());
  });

  it("切换车辆前必须确认放弃修改", async () => {
    await openDirtyVehicle();

    fireEvent.click(screen.getByTitle("第二辆").closest("tr") as HTMLTableRowElement);
    expect(screen.getByRole("button", { name: "放弃并切换" })).toBeTruthy();
    expect(screen.getByRole("complementary", { name: "车辆详情" }).textContent).toContain("CJ-001");

    fireEvent.click(screen.getByRole("button", { name: "放弃并切换" }));
    await waitFor(() => expect(screen.getByRole("complementary", { name: "车辆详情" }).textContent).toContain("CJ-002"));
  });

  it("新增车辆前必须确认放弃修改", async () => {
    await openDirtyVehicle();

    fireEvent.click(screen.getByRole("button", { name: "新增车辆" }));
    expect(screen.queryByRole("dialog", { name: "新增车辆弹窗" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "放弃并新增" }));
    expect(await screen.findByRole("dialog", { name: "新增车辆弹窗" })).toBeTruthy();
  });

  it("导入车辆前必须确认放弃修改", async () => {
    await openDirtyVehicle();

    fireEvent.click(screen.getByRole("button", { name: "导入车辆" }));
    expect(screen.queryByRole("dialog", { name: "导入车辆弹窗" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "放弃并导入" }));
    expect(await screen.findByRole("dialog", { name: "导入车辆弹窗" })).toBeTruthy();
  });
});

describe("车辆空数据状态", () => {
  it("展示空状态以及新增和导入入口", async () => {
    vehicleApiState.empty = true;
    render(<VehiclesPage />);
    expect(await screen.findByText("暂无车辆")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "新增车辆" }).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "导入车辆" }).length).toBeGreaterThan(0);
  });
});
