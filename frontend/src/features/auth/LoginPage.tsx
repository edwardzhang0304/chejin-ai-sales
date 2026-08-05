import { useState } from "react";
import type { FormEvent } from "react";

import { ApiError } from "../../shared/api/client";
import { login } from "../../shared/api/auth";
import type { AuthSession } from "../../shared/api/auth";
import { EyeIcon } from "../../shared/ui/Icons";

type Props = {
  message: string | null;
  onAuthenticated: (session: AuthSession) => void;
};

export function LoginPage({ message, onAuthenticated }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = Boolean(username.trim() && password && !submitting);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      const session = await login(username.trim(), password);
      setPassword("");
      setSubmitting(false);
      onAuthenticated(session);
    } catch (requestError) {
      setError(requestError instanceof ApiError && requestError.status === 401
        ? "账号或密码错误"
        : "暂时无法登录，请稍后重试");
      setSubmitting(false);
    }
  }

  return (
    <main className="login-view">
      <section className="login-panel" aria-labelledby="login-title">
        <header className="login-brand">
          <span className="brand-mark" aria-hidden="true">车</span>
          <span>
            <strong>车金 AI</strong>
            <small>运营后台</small>
          </span>
        </header>

        <div className="login-heading">
          <h1 id="login-title">登录运营后台</h1>
          <p>使用已开通的运营账号登录</p>
        </div>

        {error || message ? (
          <div className={`login-alert${message && !error ? " is-session" : ""}`} role="alert">
            {error || message}
          </div>
        ) : null}

        <form className="login-form" noValidate onSubmit={(event) => void handleSubmit(event)}>
          <label>
            <span>账号</span>
            <input
              type="text"
              name="username"
              autoComplete="username"
              placeholder="请输入账号"
              value={username}
              disabled={submitting}
              onChange={(event) => { setUsername(event.target.value); setError(null); }}
            />
          </label>
          <label>
            <span>密码</span>
            <span className="password-field">
              <input
                type={passwordVisible ? "text" : "password"}
                name="password"
                autoComplete="current-password"
                placeholder="请输入密码"
                value={password}
                disabled={submitting}
                onChange={(event) => { setPassword(event.target.value); setError(null); }}
              />
              <button
                type="button"
                className="password-toggle"
                aria-label={passwordVisible ? "隐藏密码" : "显示密码"}
                aria-pressed={passwordVisible}
                disabled={submitting}
                onClick={() => setPasswordVisible((visible) => !visible)}
              >
                <EyeIcon />
              </button>
            </span>
          </label>
          <button className="primary-button login-submit" type="submit" disabled={!canSubmit}>
            <span>{submitting ? "登录中" : "登录"}</span>
            {submitting ? <span className="login-spinner" aria-hidden="true" /> : null}
          </button>
        </form>
      </section>
    </main>
  );
}
