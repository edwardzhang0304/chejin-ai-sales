import { useEffect, useState } from "react";
import { request } from "../../../shared/api/client";
import { ConfirmModal } from "../../../shared/ui/ConfirmModal";

type Binding = {
  id: string;
  authorization_revision: number;
  remark_code: string;
  reinvite_available: boolean;
};

export function HookReinviteAction({ leadId }: { leadId: string }) {
  const [target, setTarget] = useState<Binding | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setTarget(null); setConfirm(false); setError(""); setLoading(true);
    request<{ items: Binding[] }>(`/leads/${leadId}/wechat-bindings`, { signal: controller.signal })
      .then(({ items }) => {
        // Ambiguous bindings need operator investigation, never choose one silently.
        const eligible = items.filter((item) => item.reinvite_available);
        setTarget(eligible.length === 1 ? eligible[0] : null);
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "无法读取客户托管状态");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [leadId, revision]);

  async function submit() {
    if (!target || busy) return;
    setBusy(true); setError("");
    try {
      await request(`/leads/${leadId}/wechat/reinvite`, { method: "POST", body: {
        binding_id: target.id, binding_revision: target.authorization_revision,
        confirm_end_previous: true, reason: "运营确认结束旧托管并重新邀请",
      } });
      setConfirm(false); setTarget(null);
      setMessage("旧托管已结束，重新邀请任务已创建。新邀请核验通过前不会恢复自动回复；原发送继续核对，不会补发。");
    } catch (failure) {
      setConfirm(false);
      setError(failure instanceof Error ? failure.message : "重新邀请失败，请刷新状态后核对");
    } finally { setBusy(false); }
  }

  return <section className="detail-block">
    <h3>微信托管</h3>
    {loading ? <p role="status">正在读取托管状态...</p> : null}
    {message ? <p role="status">{message}</p> : null}
    {error ? <><p role="alert">{error}</p><button type="button" className="secondary-button" disabled={busy} onClick={() => setRevision((value) => value + 1)}>刷新托管状态</button></> : null}
    {target ? <button type="button" className="secondary-button" disabled={busy} onClick={() => setConfirm(true)}>重新邀请</button> : null}
    {!target && !loading && !message && !error ? <p>当前没有可重新邀请的托管记录。</p> : null}
    <ConfirmModal open={confirm} title={`重新邀请 ${target?.remark_code || "此客户"}`} dangerous busy={busy}
      description="确认结束该客户的旧托管，并创建新的好友邀请任务？旧消息保留原记录继续核对，不会补发。只有新邀请完成并重新核验后，才建立新托管；手工加回或已是好友不会自动恢复。"
      confirmLabel="结束旧托管并重新邀请" onCancel={() => setConfirm(false)} onConfirm={() => void submit()} />
  </section>;
}
