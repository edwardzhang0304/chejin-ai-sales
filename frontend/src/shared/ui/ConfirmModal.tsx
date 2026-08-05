type Props = {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  dangerous?: boolean;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
};

export function ConfirmModal({ open, title, description, confirmLabel, dangerous = false, busy = false, onCancel, onConfirm }: Props) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onCancel();
    }}>
      <section className="modal small-modal confirm-modal" role="alertdialog" aria-modal="true" aria-labelledby="confirm-modal-title" aria-describedby="confirm-modal-description">
        <div className="modal-head">
          <div>
            <p>请确认</p>
            <h2 id="confirm-modal-title">{title}</h2>
          </div>
        </div>
        <p id="confirm-modal-description" className="modal-copy">{description}</p>
        <footer>
          <button type="button" disabled={busy} onClick={onCancel}>取消</button>
          <button type="button" disabled={busy} className={dangerous ? "danger-button" : "primary-button"} onClick={onConfirm}>
            {busy ? "处理中..." : confirmLabel}
          </button>
        </footer>
      </section>
    </div>
  );
}
