import { useEffect, useState } from "react";
import { request } from "../../../shared/api/client";
import { ConfirmModal } from "../../../shared/ui/ConfirmModal";

type Binding = {
  id: string;
  authorization_revision: number;
  remark_code: string;
  reinvite_available: boolean;
};

type Transfer = {
  binding_id: string; binding_revision: number; source_task_id: string;
  previous_lead_id: string; previous_name: string; previous_remark_code: string; target_name: string;
};

export function HookReinviteAction({ leadId }: { leadId: string }) {
  const [target, setTarget] = useState<Binding | null>(null);
  const [transfer, setTransfer] = useState<Transfer | null>(null);
  const [transferConfirm, setTransferConfirm] = useState(false);
  const [needsReview, setNeedsReview] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setTarget(null); setTransfer(null); setNeedsReview(false); setConfirm(false); setTransferConfirm(false); setMessage(""); setError(""); setLoading(true);
    Promise.all([
      request<{ items: Binding[] }>(`/leads/${leadId}/wechat-bindings`, { signal: controller.signal }),
      request<{ candidate: Transfer | null; needs_review: boolean }>(`/leads/${leadId}/wechat/transfer`, { signal: controller.signal }),
    ]).then(([{ items }, succession]) => {
        if (controller.signal.aborted) return;
        setTransfer(succession.candidate); setNeedsReview(succession.needs_review);
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

  async function submitTransfer() {
    if (!transfer || busy) return;
    setBusy(true); setError("");
    try {
      await request(`/leads/${leadId}/wechat/transfer`, { method: "POST", body: {
        binding_id: transfer.binding_id, binding_revision: transfer.binding_revision,
        source_task_id: transfer.source_task_id, confirm_end_previous: true,
        reason: "运营确认同一微信以后由当前新档案处理",
      } });
      setTransferConfirm(false); setTransfer(null); setTarget(null);
      setMessage("已确认档案接续。等待 Worker 核验原微信身份；不会重复加好友或补发旧消息。");
    } catch (failure) {
      setTransferConfirm(false);
      setError(failure instanceof Error ? failure.message : "档案接续失败，请刷新后核对");
    } finally { setBusy(false); }
  }

  return <section className="detail-block">
    <h3>微信托管</h3>
    {loading ? <p role="status">正在读取托管状态...</p> : null}
    {message ? <p role="status">{message}</p> : null}
    {error ? <><p role="alert">{error}</p><button type="button" className="secondary-button" disabled={busy} onClick={() => setRevision((value) => value + 1)}>刷新托管状态</button></> : null}
    {transfer ? <button type="button" className="secondary-button" disabled={busy} onClick={() => setTransferConfirm(true)}>接续已有微信</button> : null}
    {target && !needsReview ? <button type="button" className="secondary-button" disabled={busy} onClick={() => setConfirm(true)}>重新邀请</button> : null}
    {!target && !transfer && !loading && !message && !error ? <p>{needsReview ? "存在旧档案关联，请先核对客户身份与原任务。" : "当前没有可重新邀请的托管记录。"}</p> : null}
    <ConfirmModal open={transferConfirm} title="确认微信归属" dangerous busy={busy}
      description={`将旧档案“${transfer?.previous_name || ""}（${transfer?.previous_remark_code || ""}）”的微信交给当前档案“${transfer?.target_name || ""}”处理？旧档案停止自动回复，旧聊天和发送凭证保留。程序核验同一微信后才启用新档案，不会再次发送好友申请或补发旧消息。`}
      confirmLabel="确认以后由新档案处理" onCancel={() => setTransferConfirm(false)} onConfirm={() => void submitTransfer()} />
    <ConfirmModal open={confirm} title={`重新邀请 ${target?.remark_code || "此客户"}`} dangerous busy={busy}
      description="确认结束该客户的旧托管，并创建新的好友邀请任务？旧消息保留原记录继续核对，不会补发。只有新邀请完成并重新核验后，才建立新托管；手工加回或已是好友不会自动恢复。"
      confirmLabel="结束旧托管并重新邀请" onCancel={() => setConfirm(false)} onConfirm={() => void submit()} />
  </section>;
}
