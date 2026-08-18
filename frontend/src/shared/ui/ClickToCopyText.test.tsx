import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ClickToCopyText } from "./ClickToCopyText";

describe("ClickToCopyText", () => {
  const writeText = vi.fn().mockResolvedValue(undefined);

  beforeEach(() => {
    writeText.mockClear();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
  });

  afterEach(cleanup);

  it("点击字段文字直接复制，不渲染独立复制按钮文案", async () => {
    render(<ClickToCopyText label="任务 ID" value="task-001" />);

    const field = screen.getByRole("button", { name: "复制任务 ID：task-001" });
    expect(field.textContent).toContain("task-001");
    expect(screen.queryByText("复制")).toBeNull();

    fireEvent.click(field);

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("task-001"));
    await waitFor(() => expect(field.getAttribute("data-copy-state")).toBe("copied"));
  });

  it("空值短横线不可复制", () => {
    render(<ClickToCopyText label="异常原因" value="-" />);

    const field = screen.getByRole("button") as HTMLButtonElement;
    expect(field.disabled).toBe(true);
    fireEvent.click(field);
    expect(writeText).not.toHaveBeenCalled();
  });
});
