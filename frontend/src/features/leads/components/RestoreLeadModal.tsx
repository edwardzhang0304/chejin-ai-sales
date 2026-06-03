import type { LeadListItem } from "../types";

type Props = {
  lead: LeadListItem | null;
  submitting: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
};

export function RestoreLeadModal({ lead, submitting, error, onClose, onConfirm }: Props) {
  const restoreText = lead?.sales_id
    ? `恢复后将按原销售 ${lead.sales_name || "-"} 回到已分配线索。`
    : "恢复后因暂无销售归属，将回到未分配线索。";

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal confirm-modal" role="dialog" aria-modal="true" aria-label="恢复为有效线索">
        <header className="modal-head">
          <div>
            <h2>恢复为有效线索</h2>
            <p>{lead ? `确认恢复「${lead.customer_name}」吗？` : "确认恢复该线索吗？"}</p>
          </div>
        </header>

        {error ? <div className="inline-alert error">{error}</div> : null}

        <div className="confirm-copy">
          <p>{restoreText}</p>
          <p>恢复后该线索会重新进入有效线索列表，后续操作将继续写入操作日志。</p>
        </div>

        <footer className="modal-actions">
          <button type="button" className="secondary-button" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button type="button" className="primary-button" onClick={onConfirm} disabled={submitting}>
            {submitting ? "恢复中..." : "确认恢复有效"}
          </button>
        </footer>
      </section>
    </div>
  );
}
