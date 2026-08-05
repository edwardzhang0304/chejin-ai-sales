import type { ApiEnvelope, ApiErrorPayload } from "./types";

const DEFAULT_BASE_URL = "http://127.0.0.1:8000/api";
const env = import.meta.env ?? {};

export const runtimeConfig = {
  baseUrl: env.VITE_API_BASE_URL || DEFAULT_BASE_URL,
};

type UnauthorizedListener = () => void;

const unauthorizedListeners = new Set<UnauthorizedListener>();

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

/**
 * 面向运营人员的错误文案：保留可反馈给支持人员的追踪编号，隐藏工程错误码。
 */
export function formatBusinessError(error: unknown, fallback: string) {
  if (error instanceof ApiError) {
    return error.traceId ? `${error.message}（错误编号：${error.traceId}）` : error.message;
  }
  return fallback;
}

type RequestOptions = Omit<RequestInit, "body"> & {
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | null>;
  skipUnauthorizedNotification?: boolean;
};

type RawRequestOptions = RequestInit & {
  query?: RequestOptions["query"];
  skipUnauthorizedNotification?: boolean;
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

function notifyUnauthorized(response: Response, skipNotification = false) {
  if (response.status !== 401 || skipNotification) return;
  unauthorizedListeners.forEach((listener) => listener());
}

export function onUnauthorized(listener: UnauthorizedListener) {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
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

function buildAdminRequestHeaders(headers?: HeadersInit, json = false) {
  const result = new Headers(headers);
  result.delete("Authorization");
  result.delete("X-Operator-Id");
  result.delete("X-Operator-Name");
  result.delete("X-Operator-Role");
  if (json && !result.has("Content-Type")) result.set("Content-Type", "application/json");
  return result;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, query, headers, skipUnauthorizedNotification, ...init } = options;
  const response = await fetch(buildUrl(path, query), {
    ...init,
    credentials: "include",
    headers: buildAdminRequestHeaders(headers, body !== undefined),
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  const envelope = (await response.json().catch(() => null)) as ApiEnvelope<T> | null;
  if (!response.ok || envelope?.code !== "OK") {
    notifyUnauthorized(response, skipUnauthorizedNotification);
    throw apiErrorFromEnvelope(response.status, envelope);
  }

  return envelope.data;
}

export async function requestForm<T>(path: string, formData: FormData, options: RawRequestOptions = {}): Promise<T> {
  const { query, headers, skipUnauthorizedNotification, ...init } = options;
  const response = await fetch(buildUrl(path, query), {
    ...init,
    method: init.method || "POST",
    credentials: "include",
    headers: buildAdminRequestHeaders(headers),
    body: formData,
  });
  const envelope = (await response.json().catch(() => null)) as ApiEnvelope<T> | null;
  if (!response.ok || envelope?.code !== "OK") {
    notifyUnauthorized(response, skipUnauthorizedNotification);
    throw apiErrorFromEnvelope(response.status, envelope);
  }
  return envelope.data;
}

export async function requestBlob(path: string, options: RawRequestOptions = {}): Promise<Blob> {
  const { query, headers, skipUnauthorizedNotification, ...init } = options;
  const response = await fetch(buildUrl(path, query), {
    ...init,
    credentials: "include",
    headers: buildAdminRequestHeaders(headers),
  });
  if (!response.ok) {
    notifyUnauthorized(response, skipUnauthorizedNotification);
    throw await apiErrorFromResponse(response);
  }
  return response.blob();
}
