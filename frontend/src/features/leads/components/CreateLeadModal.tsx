import { FormEvent, useState } from "react";

import { useLockBodyScroll } from "../../../shared/hooks/useLockBodyScroll";
import type { DuplicateLeadErrorData, LeadCreatePayload } from "../types";

type CustomFieldRow = {
  key: string;
  value: string;
};

type Props = {
  submitting: boolean;
  error: string | null;
  duplicateData: DuplicateLeadErrorData | null;
  onClose: () => void;
  onOpenDuplicateLead: (leadId: string) => void;
  onSubmit: (payload: LeadCreatePayload, options?: { continueAdding?: boolean }) => Promise<boolean>;
};

function normalizeList(values: string[]) {
  return values.map((value) => value.trim()).filter(Boolean);
}

function updateCustomFieldValue(values: CustomFieldRow[], index: number, patch: Partial<CustomFieldRow>) {
  return values.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item));
}

export function CreateLeadModal({ submitting, error, duplicateData, onClose, onOpenDuplicateLead, onSubmit }: Props) {
  useLockBodyScroll();

  const [customerName, setCustomerName] = useState("");
  const [phone, setPhone] = useState("");
  const [wechat, setWechat] = useState("");
  const [email, setEmail] = useState("");
  const [customFields, setCustomFields] = useState<CustomFieldRow[]>([
    { key: "", value: "" },
    { key: "", value: "" },
  ]);
  const [remark, setRemark] = useState("");

  function resetForm() {
    setCustomerName("");
    setPhone("");
    setWechat("");
    setEmail("");
    setCustomFields([
      { key: "", value: "" },
      { key: "", value: "" },
    ]);
    setRemark("");
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
    const continueAdding = submitter?.value === "continue";
    const customFieldPayload = customFields.reduce<Record<string, string>>((acc, item) => {
      const key = item.key.trim();
      const value = item.value.trim();
      if (key && value) {
        acc[key] = value;
      }
      return acc;
    }, {});

    const saved = await onSubmit({
      customer_name: customerName.trim(),
      phones: normalizeList([phone]),
      wechats: normalizeList([wechat]),
      emails: normalizeList([email]),
      remark: remark.trim() || undefined,
      custom_fields: Object.keys(customFieldPayload).length > 0 ? customFieldPayload : undefined,
    }, { continueAdding });

    if (saved && continueAdding) {
      resetForm();
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <form className="modal create-lead-modal" onSubmit={(event) => void handleSubmit(event)} aria-label="新增客户">
        <header className="modal-head">
          <div>
            <h2>新增客户</h2>
          </div>
        </header>

        <div className="modal-body">
          {duplicateData ? (
            <div className="duplicate-alert" role="alert">
              <div>
                <strong>该手机号已存在，不能重复新建</strong>
                <p>已重复录入 {duplicateData.duplicate_count} 次，本次备注将追加到原线索。</p>
                <p>
                  原线索：{duplicateData.duplicate_lead.customer_name}，销售：
                  {duplicateData.duplicate_lead.sales_name || "待分配"}。
                </p>
                {error ? <p className="error-meta">{error}</p> : null}
              </div>
              <button type="button" className="secondary-button" onClick={() => onOpenDuplicateLead(duplicateData.duplicate_lead.id)}>
                查看原线索
              </button>
            </div>
          ) : error ? (
            <div className="inline-alert error">
              <strong>{error}</strong>
            </div>
          ) : null}

          <section className="form-section">
            <h3>基础信息</h3>
            <label>
              <span>客户名称 *</span>
              <input value={customerName} onChange={(event) => setCustomerName(event.target.value)} required />
            </label>
          </section>

          <section className="form-section">
            <h3>联系方式</h3>
            <div className="contact-grid">
              <label>
                <span>手机 *</span>
                <input value={phone} onChange={(event) => setPhone(event.target.value)} inputMode="tel" required />
              </label>

              <label>
                <span>微信</span>
                <input value={wechat} onChange={(event) => setWechat(event.target.value)} />
              </label>

              <label>
                <span>邮箱</span>
                <input value={email} onChange={(event) => setEmail(event.target.value)} inputMode="email" />
              </label>
            </div>
          </section>

          <section className="form-section">
            <div className="section-title-row">
              <h3>自定义信息</h3>
              <button type="button" className="link-button" onClick={() => setCustomFields((current) => [...current, { key: "", value: "" }])}>
                添加字段
              </button>
            </div>
            <div className="custom-field-list">
              {customFields.map((item, index) => (
                <div className="custom-field-row" key={`custom-${index}`}>
                  <label>
                    <span>字段名称</span>
                    <input
                      value={item.key}
                      onChange={(event) => setCustomFields((current) => updateCustomFieldValue(current, index, { key: event.target.value }))}
                      aria-label={`字段名称 ${index + 1}`}
                    />
                  </label>
                  <label>
                    <span>字段内容</span>
                    <input
                      value={item.value}
                      onChange={(event) => setCustomFields((current) => updateCustomFieldValue(current, index, { value: event.target.value }))}
                      aria-label={`字段内容 ${index + 1}`}
                    />
                  </label>
                  <button
                    type="button"
                    className="ghost-button"
                    onClick={() => setCustomFields((current) => current.filter((_, itemIndex) => itemIndex !== index))}
                  >
                    删除
                  </button>
                </div>
              ))}
            </div>
          </section>

          <section className="form-section">
            <h3>备注</h3>
            <label>
              <span>备注内容</span>
              <textarea value={remark} onChange={(event) => setRemark(event.target.value)} rows={4} />
            </label>
          </section>
        </div>

        <footer className="modal-actions">
          <button type="button" onClick={onClose}>
            取消
          </button>
          <div className="footer-right">
            <button type="submit" className="primary-button save-button" value="save" disabled={submitting}>
              {submitting ? "保存中..." : "保存"}
            </button>
            <button type="submit" className="primary-button continue-button" value="continue" disabled={submitting}>
              {submitting ? "保存中..." : "保存并继续新增"}
            </button>
          </div>
        </footer>
      </form>
    </div>
  );
}
