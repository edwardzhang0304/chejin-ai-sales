import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { deleteVehicleImage, getVehicle, listVehicles, setVehicleListed, updateVehicle, uploadVehicleImages } from "../api";
import type { VehicleImage, VehicleItem } from "../types";
import { VehiclesPage } from "../VehiclesPage";

vi.mock("../api", () => ({
  createVehicle: vi.fn(),
  listVehicles: vi.fn(),
  getVehicle: vi.fn(),
  updateVehicle: vi.fn(),
  setVehicleListed: vi.fn(),
  uploadVehicleImages: vi.fn(),
  deleteVehicleImage: vi.fn(),
  reorderVehicleImages: vi.fn(),
  downloadVehicleTemplate: vi.fn(),
  previewVehicleImport: vi.fn(),
  confirmVehicleImport: vi.fn(),
}));

vi.mock("./AuthenticatedVehicleImage", () => ({
  AuthenticatedVehicleImage: ({ alt }: { alt: string }) => <img alt={alt} />,
}));

// Synthetic API fixtures; the page, drawer and upload interactions are real components.
const image: VehicleImage = {
  id: "test-image-1", url: "/api/vehicles/images/test-image-1", original_filename: "car.png",
  content_type: "image/png", size_bytes: 3, sha256: "test-image-sha", sort_order: 0,
  is_main: true, created_at: "2026-09-08T05:47:00Z",
};
let storedVehicle: VehicleItem;

beforeEach(() => {
  vi.resetAllMocks();
  storedVehicle = {
    vehicle_code: "TEST-CAR-1", display_name: "测试车辆", brand: "测试品牌", series: null, model: null, energy_type: null, displacement: null, battery_capacity_kwh: null, drive_type: null,
    public_price: 12.8, first_registration: null, mileage_km: null, exterior_color: null,
    interior_color: null, location: null, customer_description: null, vin: null, plate_number: null,
    purchase_price: null, internal_notes: null, listing_status: "unlisted", images: [], main_image: null,
    created_at: "2026-09-08T05:44:00Z", updated_at: "2026-09-08T05:46:00Z",
  };
  vi.mocked(listVehicles).mockImplementation(async () => ({ items: [storedVehicle], total: 1, page: 1, page_size: 20 }));
  vi.mocked(getVehicle).mockImplementation(async () => storedVehicle);
  vi.mocked(uploadVehicleImages).mockImplementation(async () => {
    storedVehicle = { ...storedVehicle, images: [image], main_image: image };
    return { items: [{ filename: image.original_filename, ok: true, image }], succeeded: 1, failed: 0 };
  });
  vi.mocked(updateVehicle).mockImplementation(async (_code, patch) => {
    storedVehicle = { ...storedVehicle, ...patch };
    return storedVehicle;
  });
  vi.mocked(setVehicleListed).mockImplementation(async (_code, listed) => {
    storedVehicle = { ...storedVehicle, listing_status: listed ? "listed" : "unlisted" };
    return storedVehicle;
  });
  vi.mocked(deleteVehicleImage).mockImplementation(async () => {
    storedVehicle = { ...storedVehicle, images: [], main_image: null };
    return { image_id: image.id, deleted: true };
  });
});

afterEach(cleanup);

async function openVehicle() {
  render(<VehiclesPage />);
  fireEvent.click((await screen.findByTitle("测试车辆")).closest("tr")!);
  await screen.findByRole("button", { name: "编辑车辆" });
}

async function uploadImage() {
  const input = document.querySelector<HTMLInputElement>('input[type="file"][accept="image/jpeg,image/png,image/webp"]')!;
  fireEvent.change(input, { target: { files: [new File(["png"], "car.png", { type: "image/png" })] } });
  await screen.findByRole("button", { name: "选择车辆图片 1" });
}

describe("车辆图片已保存后的编辑与上架状态", () => {
  it("先上架失败再上传图片，会清除过期的缺图提示", async () => {
    await openVehicle();
    fireEvent.click(screen.getByRole("button", { name: "上架" }));
    expect(screen.getByText("至少一张有效车辆图片")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "编辑车辆" }));
    await uploadImage();
    expect(screen.queryByText("至少一张有效车辆图片")).toBeNull();
    expect(setVehicleListed).not.toHaveBeenCalled();
  });

  it("只上传图片也可以点击保存结束编辑，不重复提交车辆或图片", async () => {
    await openVehicle();
    fireEvent.click(screen.getByRole("button", { name: "编辑车辆" }));
    await uploadImage();
    const save = screen.getByRole<HTMLButtonElement>("button", { name: "保存" });
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await screen.findByRole("button", { name: "编辑车辆" });
    expect(uploadVehicleImages).toHaveBeenCalledTimes(1);
    expect(updateVehicle).not.toHaveBeenCalled();
    expect(storedVehicle.images).toHaveLength(1);
    expect(setVehicleListed).not.toHaveBeenCalled();
  });

  it("补图后仍校验缺失价格，保存价格后才允许确认上架", async () => {
    storedVehicle.public_price = null;
    await openVehicle();
    fireEvent.click(screen.getByRole("button", { name: "上架" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑车辆" }));
    fireEvent.change(screen.getByLabelText("公开售价"), { target: { value: "12.8" } });
    await uploadImage();
    expect(screen.queryByText("至少一张有效车辆图片")).toBeNull();
    expect(screen.getByText("大于 0 的公开售价")).toBeTruthy();
    expect((screen.getByLabelText("公开售价") as HTMLInputElement).value).toBe("12.8");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("button", { name: "编辑车辆" });
    expect(updateVehicle).toHaveBeenCalledWith("TEST-CAR-1", { public_price: 12.8 });
    expect(screen.queryByText("暂不能上架，请补齐：")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "上架" }));
    await screen.findByRole("alertdialog", { name: "确认上架车辆" });
    expect(setVehicleListed).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认上架" }));
    await waitFor(() => expect(setVehicleListed).toHaveBeenCalledWith("TEST-CAR-1", true));
    await screen.findByRole("button", { name: "下架" });
  });

  it("上传失败仍提示缺图且不会发起上架", async () => {
    vi.mocked(uploadVehicleImages).mockRejectedValue(new Error("test network failure"));
    await openVehicle();
    fireEvent.click(screen.getByRole("button", { name: "上架" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑车辆" }));
    const input = document.querySelector<HTMLInputElement>('input[type="file"][accept="image/jpeg,image/png,image/webp"]')!;
    fireEvent.change(input, { target: { files: [new File(["png"], "car.png", { type: "image/png" })] } });
    await screen.findByText("以下图片未上传成功");
    expect(screen.getByText("至少一张有效车辆图片")).toBeTruthy();
    expect(storedVehicle.images).toHaveLength(0);
    expect(setVehicleListed).not.toHaveBeenCalled();
  });

  it("保存失败后还原原值，可以结束编辑并清除旧错误，不再提交", async () => {
    vi.mocked(updateVehicle).mockRejectedValue(new Error("test save failure"));
    await openVehicle();
    fireEvent.click(screen.getByRole("button", { name: "编辑车辆" }));
    fireEvent.change(screen.getByLabelText("品牌"), { target: { value: "修改品牌" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("车辆资料保存失败，请重试。");
    fireEvent.change(screen.getByLabelText("品牌"), { target: { value: "测试品牌" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("button", { name: "编辑车辆" });
    expect(screen.queryByText("车辆资料保存失败，请重试。")).toBeNull();
    expect(updateVehicle).toHaveBeenCalledTimes(1);
  });

  it("删除已下架车辆的最后一张图片后，上架仍受缺图校验保护", async () => {
    storedVehicle = { ...storedVehicle, images: [image], main_image: image };
    await openVehicle();
    fireEvent.click(screen.getByRole("button", { name: "编辑车辆" }));
    fireEvent.click(screen.getByRole("button", { name: "删除当前图片" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await screen.findByText("暂无车辆图片");
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "上架" }));
    expect(screen.getByText("至少一张有效车辆图片")).toBeTruthy();
    expect(setVehicleListed).not.toHaveBeenCalled();
  });
});

describe("车辆扩展资料（人工 API 夹具）", () => {
  it("旧车系不进入选项，改售价时省略车系并保留其他资料", async () => {
    storedVehicle = { ...storedVehicle, series: "卡罗拉", energy_type: "hybrid", displacement: "1.5L", battery_capacity_kwh: "82.5000000000000000001", drive_type: "front_wheel_drive" };
    await openVehicle();
    fireEvent.click(screen.getByRole("button", { name: "编辑车辆" }));
    const series = screen.getByRole<HTMLSelectElement>("combobox", { name: "车系" });
    expect(screen.getByText("原车系：卡罗拉（待选择分类）")).toBeTruthy();
    expect(Array.from(series.options).some((option) => option.value === "卡罗拉")).toBe(false);
    fireEvent.change(screen.getByLabelText("公开售价"), { target: { value: "20.00" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("button", { name: "编辑车辆" });
    expect(updateVehicle).toHaveBeenCalledWith("TEST-CAR-1", { public_price: 20 });
    expect(storedVehicle.series).toBe("卡罗拉");
    expect(screen.getByText("82.5000000000000000001 kWh")).toBeTruthy();
  });

  it("旧车系可主动清空，四个字段也提交 null", async () => {
    storedVehicle = { ...storedVehicle, series: "卡罗拉", energy_type: "electric", displacement: "2.0L", battery_capacity_kwh: "75", drive_type: "four_wheel_drive" };
    await openVehicle();
    fireEvent.click(screen.getByRole("button", { name: "编辑车辆" }));
    for (const name of ["车系", "能源类型", "驱动方式"]) fireEvent.change(screen.getByRole("combobox", { name }), { target: { value: "" } });
    for (const name of ["排量", "电池包容量"]) fireEvent.change(screen.getByLabelText(name), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("button", { name: "编辑车辆" });
    expect(updateVehicle).toHaveBeenCalledWith("TEST-CAR-1", { series: null, energy_type: null, displacement: null, battery_capacity_kwh: null, drive_type: null });
    expect(screen.getAllByText("未填写")).toHaveLength(5);
  });

  it("选项顺序固定，编辑后展示中文与完整小数", async () => {
    await openVehicle();
    fireEvent.click(screen.getByRole("button", { name: "编辑车辆" }));
    expect(Array.from(screen.getByRole<HTMLSelectElement>("combobox", { name: "能源类型" }).options).map((option) => option.text)).toEqual(["未填写", "燃油", "新能源混动", "新能源增程", "新能源纯电"]);
    expect(Array.from(screen.getByRole<HTMLSelectElement>("combobox", { name: "车系" }).options).map((option) => option.text)).toEqual(["未填写", "国产新能源", "合资新能源", "国产车", "德系车", "日系车", "美系车", "韩系车", "法系车", "其他"]);
    expect(Array.from(screen.getByRole<HTMLSelectElement>("combobox", { name: "驱动方式" }).options).map((option) => option.text)).toEqual(["未填写", "前驱", "后驱", "四驱"]);
    fireEvent.change(screen.getByLabelText("能源类型"), { target: { value: "range_extended" } });
    fireEvent.change(screen.getByLabelText("车系"), { target: { value: "德系车" } });
    fireEvent.change(screen.getByLabelText("排量"), { target: { value: " 原文 1.5L " } });
    fireEvent.change(screen.getByLabelText("电池包容量"), { target: { value: "82.50000000000000001" } });
    fireEvent.change(screen.getByLabelText("驱动方式"), { target: { value: "rear_wheel_drive" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("button", { name: "编辑车辆" });
    expect(screen.getByText("新能源增程")).toBeTruthy();
    expect(screen.getByText("后驱")).toBeTruthy();
    expect(screen.getByText("82.50000000000000001 kWh")).toBeTruthy();
    expect(storedVehicle.displacement).toBe("原文 1.5L");
  });

  it.each(["0", "-1", "1e2", "75kWh", "NaN", "9".repeat(101)])("容量 %s 不提交请求，修正后可重试", async (value) => {
    await openVehicle();
    fireEvent.click(screen.getByRole("button", { name: "编辑车辆" }));
    fireEvent.change(screen.getByLabelText("电池包容量"), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(updateVehicle).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("电池包容量必须");
    fireEvent.change(screen.getByLabelText("电池包容量"), { target: { value: "82.5" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByRole("button", { name: "编辑车辆" });
    expect(storedVehicle.battery_capacity_kwh).toBe("82.5");
  });
});
