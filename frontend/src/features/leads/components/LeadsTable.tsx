import type { LeadListItem } from "../types";

type Props = {
  items: LeadListItem[];
  loading: boolean;
  error: string | null;
  selectedIds: Set<string>;
  activeLeadId: string | null;
  onRetry: () => void;
  onToggleSelected: (leadId: string) => void;
  onToggleAllVisible: () => void;
  onOpenDetail: (leadId: string) => void;
};

const statusText: Record<string, string> = {
  assigned: "已分配",
  unassigned: "未分配",
  invalid: "无效",
};

export function LeadsTable({
  items,
  loading,
  error,
  selectedIds,
  activeLeadId,
  onRetry,
  onToggleSelected,
  onToggleAllVisible,
  onOpenDetail,
}: Props) {
  const allVisibleSelected = items.length > 0 && items.every((item) => selectedIds.has(item.id));

  if (loading) {
    return <div className="state-box">正在加载线索...</div>;
  }

  if (error) {
    return (
      <div className="state-box error">
        <span>{error}</span>
        <button type="button" onClick={onRetry}>
          重试
        </button>
      </div>
    );
  }

  if (items.length === 0) {
    return <div className="state-box">暂无线索，请调整筛选条件或新增客户。</div>;
  }

  return (
    <div className="table-card">
      <table>
        <thead>
          <tr>
            <th className="checkbox-cell">
              <input type="checkbox" checked={allVisibleSelected} onChange={onToggleAllVisible} aria-label="选择当前页线索" />
            </th>
            <th>客户</th>
            <th>联系方式</th>
            <th>状态</th>
            <th>销售</th>
            <th>重复</th>
            <th>备注摘要</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id} className={activeLeadId === item.id ? "selected" : undefined} onClick={() => onOpenDetail(item.id)}>
              <td className="checkbox-cell" onClick={(event) => event.stopPropagation()}>
                <input
                  type="checkbox"
                  checked={selectedIds.has(item.id)}
                  onChange={() => onToggleSelected(item.id)}
                  aria-label={`选择${item.customer_name}`}
                />
              </td>
              <td className="lead-cell">
                <strong>{item.customer_name}</strong>
              </td>
              <td className="contact-cell">
                <strong>{item.primary_phone_masked || "-"}</strong>
                <small>{item.primary_wechat_masked || "未填写微信"}</small>
              </td>
              <td>
                <span className={`status ${item.status}`}>{statusText[item.status]}</span>
              </td>
              <td>{item.sales_name || "暂无"}</td>
              <td>{item.duplicate_count > 0 ? `${item.duplicate_count} 次` : "-"}</td>
              <td>{item.remark_summary || "-"}</td>
              <td>{new Date(item.updated_at).toLocaleString("zh-CN")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
