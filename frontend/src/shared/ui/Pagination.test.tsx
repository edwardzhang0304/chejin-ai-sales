import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getPaginationItems, Pagination } from "./Pagination";

describe("Pagination", () => {
  afterEach(cleanup);

  it("复用同一套首尾和省略号算法", () => {
    expect(getPaginationItems(1, 3)).toEqual([1, 2, 3]);
    expect(getPaginationItems(2, 10)).toEqual([1, 2, 3, "...", 10]);
    expect(getPaginationItems(5, 10)).toEqual([1, "...", 4, 5, 6, "...", 10]);
    expect(getPaginationItems(9, 10)).toEqual([1, "...", 8, 9, 10]);
  });

  it("分页和每页条数交互通过明确回调传出", () => {
    const onPageChange = vi.fn();
    const onPageSizeChange = vi.fn();
    render(
      <Pagination
        ariaLabel="测试分页"
        currentPage={2}
        pageSize={20}
        pageSizeAriaLabel="测试每页条数"
        pageSizeOptions={[20, 50]}
        total={120}
        totalPages={6}
        totalUnit="条"
        onPageChange={onPageChange}
        onPageSizeChange={onPageSizeChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    fireEvent.change(screen.getByRole("combobox", { name: "测试每页条数" }), { target: { value: "50" } });

    expect(onPageChange).toHaveBeenCalledWith(3);
    expect(onPageSizeChange).toHaveBeenCalledWith(50);
  });
});
