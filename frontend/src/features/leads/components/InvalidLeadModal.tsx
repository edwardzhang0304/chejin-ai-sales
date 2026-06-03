import { FormEvent, useState } from "react";

import type { InvalidLeadPayload, InvalidReason } from "../types";

type Props = {
  count: number;
  submitting: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (payload: InvalidLeadPayload) => void;
};

const invalidReasons: Array<{ value: InvalidReason; label: string }> = [
  { value: "empty_number", label: "空号" },
  { value: "wrong_info", label: "信息错误" },
  { value: "not_target_customer", label: "非目标客户" },
  { value: "test_data", label: "测试数据" },
  { value: "duplicate_or_mistaken", label: "重复误录" },
  { value: "other", label: "其他" },
];

export function InvalidLeadModal({ count, submitting, error, onClose, onSubmit }: Props) {
  const [reason, setReason] = useState<InvalidReason>("wrong_info");
  const [remark, setRemark] = useState("");

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    onSubmit({
      invalid_reason: reason,
      invalid_remark: remark.trim() || undefined,
    });
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <form className="modal" onSubmit={handleSubmit} aria-label="标记无效线索">
        <header className="modal-head">
          <div>
            <h2>标记为无效线索</h2>
            {count > 1 ? <p>本次将批量处理 {count} 条线索。</p> : null}
          </div>
        </header>

        {error ? <div className="inline-alert error">{error}</div> : null}

        <label>
          <span>无效原因</span>
          <select value={reason} onChange={(event) => setReason(event.target.value as InvalidReason)}>
            {invalidReasons.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span>补充说明</span>
          <textarea value={remark} onChange={(event) => setRemark(event.target.value)} rows={3} placeholder="请输入补充说明" />
        </label>

        <footer className="modal-actions">
          <button type="button" className="secondary-button" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button type="submit" className="primary-button" disabled={submitting}>
            {submitting ? "处理中..." : "确认标记无效"}
          </button>
        </footer>
      </form>
    </div>
  );
}
