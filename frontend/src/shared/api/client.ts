import type { ApiEnvelope, ApiErrorPayload } from "./types";

const DEFAULT_BASE_URL = "http://127.0.0.1:8000/api";
const env = import.meta.env ?? {};
const ADMIN_TOKEN_STORAGE_KEY = "chejin_admin_token";

export const runtimeConfig = {
  baseUrl: env.VITE_API_BASE_URL || DEFAULT_BASE_URL,
  operatorId: env.VITE_OPERATOR_ID || "00000000-0000-0000-0000-000000000001",
  operatorName: env.VITE_OPERATOR_NAME || "Ops Tester",
  operatorRole: env.VITE_OPERATOR_ROLE || "admin",
  adminToken: env.VITE_ADMIN_TOKEN || "",
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

function authErrorMessage(status: number) {
  if (status === 401) {
    return "登录已失效，请重新登录。";
  }
  if (status === 403) {
    return "当前账号无权限访问该功能。";
  }
  return "接口调用失败。";
}

function fallbackErrorCode(status: number) {
  if (status === 401) {
    return "ADMIN_UNAUTHORIZED";
  }
  if (status === 403) {
    return "ADMIN_FORBIDDEN";
  }
  return "API_ERROR";
}

export async function apiErrorFromResponse(response: Response) {
  const envelope = (await response.json().catch(() => null)) as ApiEnvelope<unknown> | null;
  return apiErrorFromEnvelope(response.status, envelope);
}

function apiErrorFromEnvelope(status: number, envelope: ApiEnvelope<unknown> | null) {
  return new ApiError({
    status,
    code: envelope?.code || fallbackErrorCode(status),
    message: envelope?.message || authErrorMessage(status),
    data: envelope?.data ?? {},
    traceId: envelope?.trace_id,
  });
}

export function buildUrl(path: string, query?: RequestOptions["query"]) {
  const url = new URL(`${runtimeConfig.baseUrl}${path}`);
  Object.entries(query ?? {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  });
  return url.toString();
}

export function getAdminToken() {
  if (runtimeConfig.adminToken) {
    return runtimeConfig.adminToken;
  }
  if (typeof window === "undefined") {
    return "";
  }
  return window.localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY)?.trim() || "";
}

export function setAdminToken(token: string) {
  if (typeof window === "undefined") {
    runtimeConfig.adminToken = token.trim();
    return;
  }
  const cleanToken = token.trim();
  if (cleanToken) {
    window.localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, cleanToken);
  } else {
    window.localStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY);
  }
}

export function buildOperatorHeaders(headers?: HeadersInit): HeadersInit {
  const adminToken = getAdminToken();
  return {
    ...(adminToken ? { Authorization: `Bearer ${adminToken}` } : {}),
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

  const envelope = (await response.json().catch(() => null)) as ApiEnvelope<T> | null;
  if (!response.ok || envelope?.code !== "OK") {
    throw apiErrorFromEnvelope(response.status, envelope);
  }

  return envelope.data;
}
