import { useEffect, useState } from "react";

import type { LeadDetail } from "../types";

type Props = {
  detail: LeadDetail | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onRetry: () => void;
  onMarkInvalid: (leadId: string) => void;
  onRestore: (leadId: string) => void;
  revealedPhones: Record<string, string>;
  onRevealPhone: (contactId: string) => void;
};

const statusText: Record<string, string> = {
  assigned: "已分配",
  unassigned: "未分配",
  invalid: "无效",
};

const contactText: Record<string, string> = {
  phone: "手机",
  wechat: "微信",
  email: "邮箱",
};

type DetailTab = "overview" | "notes" | "duplicates" | "assignments";

const detailTabs: Array<{ key: DetailTab; label: string }> = [
  { key: "overview", label: "概览" },
  { key: "notes", label: "备注" },
  { key: "duplicates", label: "重复记录" },
  { key: "assignments", label: "分配记录" },
];

export function LeadDetailDrawer({ detail, loading, error, onClose, onRetry, onMarkInvalid, onRestore, revealedPhones, onRevealPhone }: Props) {
  const [activeTab, setActiveTab] = useState<DetailTab>("overview");

  useEffect(() => {
    setActiveTab("overview");
  }, [detail?.id]);

  if (error && !loading) {
    return (
      <aside className="detail-drawer empty" aria-label="线索详情">
        <div className="state-box error">
          <span>{error}</span>
          <button type="button" className="secondary-button" onClick={onRetry}>
            重试
          </button>
        </div>
      </aside>
    );
  }

  if (!detail && !loading) {
    return (
      <aside className="detail-drawer empty" aria-label="线索详情">
        <p>选择一条线索查看详情</p>
      </aside>
    );
  }

  return (
    <aside className="detail-drawer" aria-label="线索详情">
      <div className="drawer-head">
        <div>
          <p className="eyebrow">线索详情</p>
          <h2>{loading ? "加载中" : detail?.customer_name}</h2>
        </div>
        <button type="button" className="icon-button" onClick={onClose} aria-label="关闭详情">
          ×
        </button>
      </div>

      {loading || !detail ? (
        <div className="state-box">正在加载详情...</div>
      ) : (
        <div className="drawer-body">
          <section className="identity-card">
            <span className={`status-badge ${detail.status}`}>{statusText[detail.status]}</span>
            <dl>
              <div>
                <dt>主手机号</dt>
                <dd>{detail.primary_phone_masked || "未填写"}</dd>
              </div>
              <div>
                <dt>当前销售</dt>
                <dd>{detail.sales_name || "暂无"}</dd>
              </div>
              <div>
                <dt>分配方式</dt>
                <dd>{detail.assign_status === "assigned" ? "轮询自动分配" : "待分配"}</dd>
              </div>
              <div>
                <dt>创建时间</dt>
                <dd>{new Date(detail.created_at).toLocaleString("zh-CN")}</dd>
              </div>
            </dl>
          </section>

          <div className="tabs" role="tablist" aria-label="线索详情标签">
            {detailTabs.map((tab) => (
              <button
                className={activeTab === tab.key ? "active" : undefined}
                type="button"
                role="tab"
                aria-selected={activeTab === tab.key}
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {activeTab === "overview" ? (
            <>
              <section className="detail-block contact-block">
                <h3>联系方式</h3>
                {detail.contacts.map((contact) => (
                  <p key={contact.id} className="contact-row">
                    <span>{contactText[contact.contact_type] || contact.contact_type}</span>
                    <strong>{revealedPhones[contact.id] || contact.masked_value}</strong>
                    {contact.contact_type === "phone" ? (
                      <button type="button" className="secondary-button contact-action-button" onClick={() => onRevealPhone(contact.id)}>
                        {revealedPhones[contact.id] ? "已查看" : "查看完整"}
                      </button>
                    ) : contact.contact_type === "wechat" ? (
                      <button
                        type="button"
                        className="secondary-button contact-action-button"
                        onClick={() => void navigator.clipboard?.writeText(contact.masked_value)}
                      >
                        复制
                      </button>
                    ) : null}
                  </p>
                ))}
              </section>

              <section className="detail-block task-flow-block">
                <h3>任务链路</h3>
                <ol className="flow-list">
                  {detail.task_nodes.map((node) => (
                    <li key={node.key}>
                      <strong>{node.label}</strong>
                      <span>{node.time ? new Date(node.time).toLocaleString("zh-CN") : "等待处理"}</span>
                    </li>
                  ))}
                </ol>
              </section>

              <section className="drawer-action-section detail-actions" aria-label="线索操作">
                <h3>操作</h3>
                <div className="drawer-actions">
                {detail.status === "invalid" ? (
                  <button type="button" className="secondary-button" onClick={() => onRestore(detail.id)}>
                    恢复有效
                  </button>
                ) : (
                  <button type="button" className="secondary-button" onClick={() => onMarkInvalid(detail.id)}>
                    标记无效
                  </button>
                )}
                </div>
              </section>
            </>
          ) : null}

          {activeTab === "notes" ? (
            <section className="detail-block">
              <h3>备注</h3>
              {detail.notes.length > 0 ? (
                <ol className="record-list">
                  {detail.notes.map((note) => (
                    <li key={note.id}>
                      <strong>{note.note_type}</strong>
                      <p>{note.content}</p>
                      <span>{new Date(note.created_at).toLocaleString("zh-CN")}</span>
                    </li>
                  ))}
                </ol>
              ) : (
                <p>暂无备注记录</p>
              )}
            </section>
          ) : null}

          {activeTab === "duplicates" ? (
            <section className="detail-block">
              <h3>重复记录</h3>
              {detail.duplicate_events.length > 0 ? (
                <ol className="record-list">
                  {detail.duplicate_events.map((event) => (
                    <li key={event.id}>
                      <strong>{event.submitted_customer_name || "重复录入"}</strong>
                      <p>{event.submitted_phone_masked || "未记录手机号"}</p>
                      <span>{new Date(event.created_at).toLocaleString("zh-CN")}</span>
                    </li>
                  ))}
                </ol>
              ) : (
                <p>暂无重复记录</p>
              )}
            </section>
          ) : null}

          {activeTab === "assignments" ? (
            <section className="detail-block">
              <h3>分配记录</h3>
              {detail.assignments?.length ? (
                <ol className="record-list">
                  {detail.assignments.map((assignment) => (
                    <li key={assignment.id}>
                      <strong>{assignment.sales_name || "暂无销售"}</strong>
                      <p>{assignment.assignment_status === "succeeded" ? "分配成功" : assignment.failure_reason || "分配失败"}</p>
                      <span>{new Date(assignment.created_at).toLocaleString("zh-CN")}</span>
                    </li>
                  ))}
                </ol>
              ) : (
                <p>暂无分配记录</p>
              )}
            </section>
          ) : null}
        </div>
      )}
    </aside>
  );
}
