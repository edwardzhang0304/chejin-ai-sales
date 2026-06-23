import { useLockBodyScroll } from "../../../shared/hooks/useLockBodyScroll";
import type { LeadListItem } from "../types";

type Props = {
  lead: LeadListItem | null;
  submitting: boolean;
  error: string | null;
  onClose: () => void;
  onConfirm: () => void;
};

export function RestoreLeadModal({ lead, submitting, error, onClose, onConfirm }: Props) {
  useLockBodyScroll();

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal small-modal" role="dialog" aria-modal="true" aria-label="恢复为有效线索">
        <header>
          <h2>恢复为有效线索</h2>
        </header>

        {error ? <div className="inline-alert error">{error}</div> : null}

        <p className="modal-copy">恢复后，如果线索存在当前销售，状态回到已分配；否则回到未分配并可触发重新分配。</p>

        <footer>
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
