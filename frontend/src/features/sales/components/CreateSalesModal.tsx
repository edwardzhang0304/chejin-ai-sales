import { FormEvent, useState } from "react";

import type { SalesUpsertPayload } from "../types";

type Props = {
  submitting: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (payload: SalesUpsertPayload) => Promise<boolean>;
};

function optionalText(value: string) {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function CreateSalesModal({ submitting, error, onClose, onSubmit }: Props) {
  const [salesName, setSalesName] = useState("");
  const [phone, setPhone] = useState("");
  const [wechat, setWechat] = useState("");
  const [sortOrder, setSortOrder] = useState("");
  const [remark, setRemark] = useState("");
  const [enabled, setEnabled] = useState(true);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const saved = await onSubmit({
      sales_name: salesName.trim(),
      phone: optionalText(phone),
      wechat: optionalText(wechat),
      feishu_user_id: null,
      enabled,
      sort_order: sortOrder.trim() ? Number(sortOrder) : null,
      remark: optionalText(remark),
    });

    if (saved) {
      onClose();
    }
  }

  const canSubmit = salesName.trim().length > 0 && !submitting;

  return (
    <div className="modal-backdrop" role="presentation">
      <form className="modal sales-modal" onSubmit={(event) => void handleSubmit(event)} aria-label="新增销售">
        <header className="modal-head">
          <div>
            <h2>新增销售</h2>
          </div>
        </header>

        <div className="modal-body sales-modal-body">
          {error ? (
            <div className="inline-alert error" role="alert">
              <strong>{error}</strong>
            </div>
          ) : null}

          <section className="form-section">
            <h3>基础信息</h3>
            <div className="sales-form-grid">
              <label>
                <span>销售姓名 *</span>
                <input value={salesName} onChange={(event) => setSalesName(event.target.value)} required />
              </label>

              <label>
                <span>手机号</span>
                <input value={phone} onChange={(event) => setPhone(event.target.value)} inputMode="tel" />
              </label>

              <label>
                <span>微信</span>
                <input value={wechat} onChange={(event) => setWechat(event.target.value)} />
              </label>

              <label>
                <span>排序</span>
                <input value={sortOrder} onChange={(event) => setSortOrder(event.target.value)} inputMode="numeric" type="number" min="0" />
              </label>
            </div>
          </section>

          <section className="form-section">
            <h3>销售状态</h3>
            <div className="sales-check-grid">
              <label className="toggle-row">
                <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
                启用销售
              </label>
            </div>
          </section>

          <section className="form-section">
            <h3>备注</h3>
            <label>
              <span>备注内容</span>
              <textarea value={remark} onChange={(event) => setRemark(event.target.value)} rows={3} />
            </label>
          </section>
        </div>

        <footer className="modal-actions">
          <button type="button" className="secondary-button" onClick={onClose}>
            取消
          </button>
          <button type="submit" className="primary-button" disabled={!canSubmit}>
            {submitting ? "保存中..." : "保存"}
          </button>
        </footer>
      </form>
    </div>
  );
}
