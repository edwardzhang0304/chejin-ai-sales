import type { ApiEnvelope, ApiErrorPayload } from "./types";

const DEFAULT_BASE_URL = "http://127.0.0.1:8000/api";

export const runtimeConfig = {
  baseUrl: import.meta.env.VITE_API_BASE_URL || DEFAULT_BASE_URL,
  operatorId: import.meta.env.VITE_OPERATOR_ID || "00000000-0000-0000-0000-000000000001",
  operatorName: import.meta.env.VITE_OPERATOR_NAME || "Ops Tester",
  operatorRole: import.meta.env.VITE_OPERATOR_ROLE || "admin",
};

export class ApiError extends Error {
  code: string;
  status: number;
  data: unknown;
  traceId: string | null;

  constructor(payload: ApiErrorPayload) {
    super(payload.message);
    this.name = "ApiError";
    this.code = payload.code;
    this.status = payload.status;
    this.data = payload.data;
    this.traceId = payload.traceId ?? null;
  }
}

export function formatApiError(error: unknown, fallback: string) {
  if (error instanceof ApiError) {
    const parts = [`错误码：${error.code}`];
    if (error.traceId) {
      parts.push(`Trace ID：${error.traceId}`);
    }
    return `${error.message}（${parts.join("，")}）`;
  }
  return fallback;
}

type RequestOptions = Omit<RequestInit, "body"> & {
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
};

export function buildUrl(path: string, query?: RequestOptions["query"]) {
  const url = new URL(`${runtimeConfig.baseUrl}${path}`);
  Object.entries(query ?? {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  });
  return url.toString();
}

export function buildOperatorHeaders(headers?: HeadersInit): HeadersInit {
  return {
    "X-Operator-Id": runtimeConfig.operatorId,
    "X-Operator-Name": runtimeConfig.operatorName,
    "X-Operator-Role": runtimeConfig.operatorRole,
    ...headers,
  };
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, query, headers, ...init } = options;
  const response = await fetch(buildUrl(path, query), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...buildOperatorHeaders(headers),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  const envelope = (await response.json()) as ApiEnvelope<T>;
  if (!response.ok || envelope.code !== "OK") {
    throw new ApiError({
      status: response.status,
      code: envelope.code,
      message: envelope.message,
      data: envelope.data,
      traceId: envelope.trace_id,
    });
  }

  return envelope.data;
}
