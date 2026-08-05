import { afterEach, describe, expect, it, vi } from "vitest";

import { downloadVehicleTemplate, listVehicles, uploadVehicleImages } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("vehicle API", () => {
  it("serializes list filters through the shared API envelope", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: "OK",
      message: "ok",
      trace_id: "trace-list",
      data: { items: [], page: 2, page_size: 20, total: 0 },
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await listVehicles({ keyword: "宝马", listing_status: "listed", page: 2, page_size: 20 });

    const requestUrl = String(fetchMock.mock.calls[0][0]);
    expect(requestUrl).toContain("keyword=%E5%AE%9D%E9%A9%AC");
    expect(requestUrl).toContain("listing_status=listed");
    expect(requestUrl).toContain("page=2");
  });

  it("uses multipart boundary from the browser and includes the Cookie session", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: "OK",
      message: "ok",
      trace_id: "trace-upload",
      data: { items: [], succeeded: 0, failed: 0 },
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await uploadVehicleImages("CJ-001", [new File(["image"], "car.jpg", { type: "image/jpeg" })]);

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(init.body).toBeInstanceOf(FormData);
    expect(init.credentials).toBe("include");
    expect(headers.has("Authorization")).toBe(false);
    expect(headers.has("Content-Type")).toBe(false);
  });

  it("downloads protected files as blobs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(new Blob(["xlsx"]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const blob = await downloadVehicleTemplate();

    expect(blob.size).toBeGreaterThan(0);
    expect(String(fetchMock.mock.calls[0][0])).toContain("/vehicles/excel/template");
    expect((fetchMock.mock.calls[0][1] as RequestInit).credentials).toBe("include");
  });
});
