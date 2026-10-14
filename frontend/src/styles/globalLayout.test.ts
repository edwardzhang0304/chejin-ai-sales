import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

const stylesheet = readFileSync("src/styles/global.css", "utf8");

describe("运营后台公共布局合同", () => {
  it("六个模块共用同一工作区宽度，不再各自固定页面宽度", () => {
    expect(stylesheet).toMatch(
      /\.leads-page,\s*\.sales-page,\s*\.workers-page,\s*\.tasks-page,\s*\.logs-page,\s*\.knowledge-page\s*\{[^}]*width:\s*100%/s,
    );
    expect(stylesheet).toMatch(
      /\.vehicles-page,\s*\.knowledge-page\s*\{[^}]*width:\s*100%/s,
    );
    expect(stylesheet).toMatch(/\.vehicle-management-grid\s*\{[^}]*width:\s*100%/s);
  });

  it("列表默认铺满，只有打开详情时才保留详情列", () => {
    for (const selector of ["content-grid", "management-grid"]) {
      expect(stylesheet).toMatch(new RegExp(`\\.${selector}\\s*\\{[^}]*grid-template-columns:\\s*minmax\\(0, 1fr\\) 360px`, "s"));
    }
    expect(stylesheet).toContain(".content-grid:not(:has(> .detail-drawer))");
    expect(stylesheet).toContain(".management-grid:not(:has(> .management-drawer:not(.closed)))");
    expect(stylesheet).not.toMatch(/width:\s*(728|768)px/);
  });

  it("列表、筛选、表格和分页继续使用设计稿的固定垂直基线", () => {
    expect(stylesheet).toMatch(/\.list-region\s*\{[^}]*display:\s*block[^}]*width:\s*100%/s);
    expect(stylesheet).toMatch(/\.task-filter-card\s*\{[^}]*height:\s*164px/s);
    expect(stylesheet).toMatch(
      /\.screen-sales \.management-table-card,\s*\.screen-workers \.management-table-card\s*\{[^}]*height:\s*524px/s,
    );
    expect(stylesheet).toMatch(
      /\.vehicle-pagination-row,\s*\.paginated-management-pagination-row\s*\{[^}]*margin-top:\s*14px/s,
    );
  });

  it("窄工作区筛选控件不会跨列重叠", () => {
    expect(stylesheet).toMatch(
      /@media \(max-width: 1439px\)[\s\S]*?\.filter-card > \.search-field,\s*\.filter-card > \.select-field\s*\{[^}]*width:\s*100%[^}]*min-width:\s*0/s,
    );
  });

  it("真实长名称不会撑高线索详情抽屉", () => {
    expect(stylesheet).toMatch(
      /\.detail-drawer \.drawer-head h2\s*\{[^}]*overflow:\s*hidden[^}]*text-overflow:\s*ellipsis[^}]*white-space:\s*nowrap/s,
    );
    expect(stylesheet).toMatch(
      /\.identity-card dd\s*\{[^}]*overflow:\s*hidden[^}]*text-overflow:\s*ellipsis[^}]*white-space:\s*nowrap/s,
    );
    expect(stylesheet).toMatch(
      /\.detail-drawer \.drawer-close-button\s*\{[^}]*flex:\s*0 0 36px/s,
    );
    expect(stylesheet).toMatch(
      /\.identity-card\s*\{[^}]*width:\s*100%[^}]*min-width:\s*0[^}]*overflow:\s*hidden/s,
    );
  });
});
