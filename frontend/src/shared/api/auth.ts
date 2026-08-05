import { request } from "./client";

export type AuthSession = {
  operator_id: string;
  operator_name: string;
};

export function getAuthSession(signal?: AbortSignal) {
  return request<AuthSession>("/auth/session", { method: "GET", signal, skipUnauthorizedNotification: true });
}

export function login(username: string, password: string, signal?: AbortSignal) {
  return request<AuthSession>("/auth/login", {
    method: "POST",
    body: { username, password },
    signal,
    skipUnauthorizedNotification: true,
  });
}

export function logout(signal?: AbortSignal) {
  return request<{ logged_out: boolean }>("/auth/logout", {
    method: "POST",
    signal,
    skipUnauthorizedNotification: true,
  });
}
