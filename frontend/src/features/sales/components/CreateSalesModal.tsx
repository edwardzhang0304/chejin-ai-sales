import { FormEvent, useState } from "react";

import { useLockBodyScroll } from "../../../shared/hooks/useLockBodyScroll";
import type { SalesCreatePayload, SalesWorkerSummary } from "../types";

type Props = {
  submitting: boolean;
  error: string | null;
  workerOptions: SalesWorkerSummary[];
  onClose: () => void;
  onSubmit: (payload: SalesCreatePayload) => Promise<boolean>;
};

const PHONE_PATTERN = /^1[3-9]\d{9}$/;

function optionalText(value: string) {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function CreateSalesModal({ submitting, error, workerOptions, onClose, onSubmit }: Props) {
  useLockBodyScroll();

  const [salesName, setSalesName] = useState("");
  const [phone, setPhone] = useState("");
  const [wechat, setWechat] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [workerId, setWorkerId] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedPhone = phone.trim();
    if (!PHONE_PATTERN.test(normalizedPhone)) {
      setValidationError("请输入 11 位有效手机号。");
      return;
    }
    setValidationError(null);
    const saved = await onSubmit({
      sales_name: salesName.trim(),
      phone: normalizedPhone,
      wechat: optionalText(wechat),
      worker_id: optionalText(workerId),
      enabled,
      sort_order: null,
      remark: null,
    });

    if (saved) {
      onClose();
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <form className="modal sales-modal" onSubmit={(event) => void handleSubmit(event)} aria-label="新增销售">
        <header>
          <h2>新增销售</h2>
        </header>

        <div className="form-stack">
          {error || validationError ? (
            <div className="inline-alert error" role="alert">
              <strong>{validationError || error}</strong>
            </div>
          ) : null}

          <section className="form-section">
            <div className="drawer-form">
              <label>
                <span>
                  销售姓名 <b>*</b>
                </span>
                <input value={salesName} onChange={(event) => setSalesName(event.target.value)} placeholder="请输入销售姓名" required />
              </label>

              <label>
                <span>
                  手机号 <b>*</b>
                </span>
                <input
                  value={phone}
                  onChange={(event) => setPhone(event.target.value)}
                  inputMode="tel"
                  autoComplete="tel"
                  maxLength={11}
                  pattern="1[3-9][0-9]{9}"
                  placeholder="请输入 11 位手机号"
                  required
                />
              </label>

              <label>
                <span>微信号</span>
                <input value={wechat} onChange={(event) => setWechat(event.target.value)} placeholder="请输入微信号" />
              </label>

              <label>
                <span className="field-label">
                  状态 <span className="tip-icon" tabIndex={0} title="启用/关闭分配线索" aria-label="状态说明">!</span>
                </span>
                <select value={enabled ? "enabled" : "disabled"} onChange={(event) => setEnabled(event.target.value === "enabled")}>
                  <option value="enabled">启用</option>
                  <option value="disabled">停用</option>
                </select>
              </label>

              <label>
                <span>选择 Worker</span>
                <select value={workerId} onChange={(event) => setWorkerId(event.target.value)}>
                  <option value="">请选择 Worker，可暂不绑定</option>
                  {workerOptions.map((worker) => (
                    <option key={worker.id} value={worker.id}>
                      {worker.worker_name}（{worker.online_status === "online" ? "在线" : "离线"} / {worker.enabled ? "启用" : "停用"}）
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <p className="drawer-hint">如需后续绑定或更换 Worker，请进入销售详情抽屉，点击“编辑销售”处理。</p>
          </section>
        </div>

        <footer>
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button type="submit" className="primary-button" disabled={submitting}>
            {submitting ? "保存中..." : "保存"}
          </button>
        </footer>
      </form>
    </div>
  );
}
