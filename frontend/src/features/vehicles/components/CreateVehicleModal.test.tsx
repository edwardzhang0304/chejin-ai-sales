import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CreateVehicleModal } from "./CreateVehicleModal";

afterEach(cleanup);
describe("车辆新增仍只填写名称", () => {
  it("第一步只有名称，其他资料留待创建后在详情编辑", () => {
    const onSubmit = vi.fn();
    render(<CreateVehicleModal open busy={false} error={null} onCancel={() => {}} onSubmit={onSubmit} />);
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.getByText("保存后在详情中上传图片和补充资料")).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/车辆展示名称/), { target: { value: " 测试新车 " } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(onSubmit).toHaveBeenCalledWith("测试新车");
  });
});
