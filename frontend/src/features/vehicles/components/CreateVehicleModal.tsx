import { useEffect, useRef, useState } from "react";

type Props = {
  open: boolean;
  busy: boolean;
  error: string | null;
  onCancel: () => void;
  onSubmit: (displayName: string) => void;
};

export function CreateVehicleModal({ open, busy, error, onCancel, onSubmit }: Props) {
  const [displayName, setDisplayName] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setDisplayName("");
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }, [open]);

  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation">
      <form
        className="modal small-modal vehicle-create-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-vehicle-title"
        onSubmit={(event) => {
          event.preventDefault();
          const value = displayName.trim();
          if (value) onSubmit(value);
        }}
      >
        <header><h2 id="create-vehicle-title">新增车辆</h2></header>
        <div className="form-stack">
          <label>
            <span>车辆展示名称 <b>*</b></span>
            <input ref={inputRef} maxLength={200} value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="例如：2022 款 2.0T 运动版" />
          </label>
          <div className="generated-note vehicle-generated-note">
            <strong>保存后由系统自动处理</strong>
            <span>生成车辆编号</span>
            <span>车辆默认为已下架</span>
            <span>保存后在详情中上传图片和补充资料</span>
          </div>
        </div>
        {error ? <div className="inline-alert error" role="alert">{error}</div> : null}
        <footer>
          <button type="button" disabled={busy} onClick={onCancel}>取消</button>
          <button className="primary-button" type="submit" disabled={busy || !displayName.trim()}>{busy ? "保存中..." : "保存"}</button>
        </footer>
      </form>
    </div>
  );
}
