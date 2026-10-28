import { useEffect, useState } from "react";
import { request } from "../../../shared/api/client";

type Review = {
  id: string; reason: string; status: string; revision: number; note: string | null;
  created_at: string; resolved_at: string | null;
  messages: Array<{ id: string; content: string | null; message_type: string; sender_role: string; withdrawn?: boolean; system_notice?: boolean; occurred_at: string | null }>;
};
const reasons: Record<string, string> = {
  profile_succession: "客户档案切换前后归属待核对",
  legacy_invitation_clock: "旧邀请缺少可靠时间依据",
  legacy_reply_owner: "旧会话回复归属待核对",
};

export function HistoryReviewPanel({ leadId }: { leadId: string }) {
  const [rows, setRows] = useState<Review[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    request<Review[]>(`/leads/${leadId}/wechat/history-reviews`, { signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setRows(data); })
      .catch((err: unknown) => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "加载失败"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [leadId, refresh]);
  async function resolve(row: Review) {
    if (busy || !notes[row.id]?.trim()) return;
    setBusy(row.id); setError("");
    try {
      await request(`/leads/${leadId}/wechat/history-reviews/${row.id}/resolve`, {
        method: "POST", body: { revision: row.revision, note: notes[row.id].trim() },
      });
      setRefresh((value) => value + 1);
    } catch (err) { setError(err instanceof Error ? err.message : "提交失败"); }
    finally { setBusy(null); }
  }
  return <section className="detail-block" aria-label="历史核对">
    <h3>历史核对</h3>
    {error ? <div role="alert">{error}<button type="button" className="secondary-button" onClick={() => setRefresh((value) => value + 1)}>刷新</button></div> : null}
    {loading ? <p role="status">正在加载...</p> : rows.length === 0 ? <p>暂无历史核对事项</p> :
      <ol className="record-list">{rows.map((row) => <li key={row.id}>
        <strong>{row.status === "processed" ? "已处理" : "待核对"} · {reasons[row.reason] || "历史归属待核对"}</strong>
        {row.messages.length === 0 ? <p>此边界尚无可列出的聊天消息</p> : row.messages.map((message) => <div key={message.id}>
          <p>{message.sender_role === "customer" ? "客户" : "发出记录"} · {message.occurred_at ? new Date(message.occurred_at).toLocaleString("zh-CN") : "时间未确认"}</p>
          {message.system_notice ? <strong>系统提示（原记录保留）</strong> : null}
          {message.withdrawn ? <strong>已撤回（原记录保留）</strong> : null}
          <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{message.content || ({ voice: "[语音]", image: "[图片]" }[message.message_type] || "[非文字消息]")}</p>
        </div>)}
        {row.status === "processed" ? <p>核对说明：{row.note}</p> : <>
          <label htmlFor={`history-note-${row.id}`}>核对说明</label>
          <textarea id={`history-note-${row.id}`} maxLength={2000} value={notes[row.id] || ""}
            onChange={(event) => setNotes((value) => ({ ...value, [row.id]: event.target.value }))} disabled={busy !== null} />
          <button type="button" className="secondary-button" disabled={busy !== null || !notes[row.id]?.trim()}
            onClick={() => void resolve(row)}>{busy === row.id ? "提交中..." : "标记已处理"}</button>
        </>}
      </li>)}</ol>}
  </section>;
}
