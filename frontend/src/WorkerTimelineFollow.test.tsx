import React from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { WorkerClientBaseline } from "../../packages/worker-ui-baseline/src/WorkerClientBaseline";
import { workerClientMock } from "../../packages/worker-ui-baseline/src/mockData";
import type { TimelineStepModel } from "../../packages/worker-ui-baseline/src/types";

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(200);
  vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(1200);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function(this: HTMLElement) {
    const viewport = this.closest(".cw-chain-viewport") as HTMLElement | null;
    const row = this.closest(".cw-step");
    const top = row ? (row.textContent?.includes("new history") ? 700 : 200) - (viewport?.scrollTop || 0) : 0;
    return { top, left: 0, width: 300, height: row ? 20 : 200, right: 300, bottom: top + 200, x: 0, y: top, toJSON() {} };
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const old: TimelineStepModel = { stepId: "old:bind", groupId: "old", title: "old invitation", state: "waiting", focus: false };
function view(steps: TimelineStepModel[]) {
  return <WorkerClientBaseline screen="running" model={{ ...workerClientMock, hookProcessSteps: steps }} />;
}
it("keeps a completed live step centered and preserves its DOM when prior rows change", () => {
  const current: TimelineStepModel = { stepId: "new:history", groupId: "new", title: "new history", state: "current", focus: true };
  const r = render(view([old, current]));
  const marker = r.getByText("new history").closest("li");
  const viewport = r.container.querySelector(".cw-chain-viewport") as HTMLElement;
  expect(viewport.scrollTop).toBe(610);
  r.rerender(view([old, { ...current, state: "done" }]));
  expect(viewport.scrollTop).toBe(610);
  r.rerender(view([{ ...old, stepId: "earlier:bind" }, old, { ...current, state: "done" }]));
  expect(r.getByText("new history").closest("li")).toBe(marker);
  expect(viewport.scrollTop).toBe(610);
});
it("does not pull a user back down while reading older steps", () => {
  const current: TimelineStepModel = { stepId: "new:history", title: "new history", state: "current", focus: true };
  const r = render(view([old, current]));
  const viewport = r.container.querySelector(".cw-chain-viewport") as HTMLElement;
  viewport.scrollTop = 20; fireEvent.scroll(viewport);
  r.rerender(view([old, { ...current, state: "done" }]));
  expect(viewport.scrollTop).toBe(20);
});
it("does not jump back to an old invitation after the active round ends", () => {
  const r = render(view([old, { stepId: "new:finished", title: "finished", state: "done", focus: false }]));
  const viewport = r.container.querySelector(".cw-chain-viewport") as HTMLElement;
  expect(viewport.scrollTop).toBe(1000);
});
