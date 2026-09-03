import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { formatBusinessError } from "../../shared/api/client";
import { ConfirmModal } from "../../shared/ui/ConfirmModal";
import { CloseIcon } from "../../shared/ui/Icons";
import { Pagination } from "../../shared/ui/Pagination";
import { Toast } from "../../shared/ui/Toast";
import {
  confirmKnowledgeRelease,
  createKnowledgeDraft,
  getKnowledgeItem,
  getKnowledgeRelease,
  getKnowledgeSummary,
  listKnowledgeItems,
  listKnowledgeReleases,
  previewKnowledgeRelease,
  previewKnowledgeRollback,
  stageKnowledgeArchive,
  updateKnowledgeDraft,
} from "./api";
import type {
  KnowledgeFilterStatus,
  KnowledgeItem,
  KnowledgeOperation,
  KnowledgePreview,
  KnowledgeRelease,
  KnowledgeReleaseSummary,
  KnowledgeSummary,
} from "./types";
import "./knowledge.css";

const pageSizeOptions = [20, 50] as const;

function formatDateTime(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date).replaceAll("/", "-");
}

function operationLabel(operation: KnowledgeOperation | "bootstrap") {
  return {
    bootstrap: "初始化",
    create: "新增",
    update: "修改",
    archive: "归档",
    rollback: "回滚",
  }[operation];
}

function KnowledgeStatusBadge({ status }: { status: KnowledgeItem["status"] }) {
  const label = status === "draft" ? "草稿" : status === "published" ? "已发布" : "已归档";
  return <span className={`status ${status === "published" ? "assigned" : "unassigned"}`}>{label}</span>;
}

type EditorValue = { title: string; content: string };

function KnowledgeEditorFields({ value, onChange, disabled }: { value: EditorValue; onChange: (value: EditorValue) => void; disabled: boolean }) {
  return (
    <div className="knowledge-editor-fields">
      <label>
        <span>知识标题</span>
        <input
          value={value.title}
          maxLength={80}
          disabled={disabled}
          placeholder="例如：客户询问价格时的回复规则"
          onChange={(event) => onChange({ ...value, title: event.target.value })}
        />
        <small>{value.title.length}/80</small>
      </label>
      <label>
        <span>规则正文</span>
        <textarea
          value={value.content}
          maxLength={5000}
          disabled={disabled}
          placeholder="填写 Brain 可检索和引用的正式业务知识"
          onChange={(event) => onChange({ ...value, content: event.target.value })}
        />
        <small>{value.content.length}/5000</small>
      </label>
    </div>
  );
}

function PublishPreviewModal({ preview, busy, onCancel, onConfirm }: { preview: KnowledgePreview | null; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  if (!preview) return null;
  const firstChange = preview.change_set[0];
  return (
    <div className="modal-backdrop knowledge-preview-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onCancel();
    }}>
      <section className="modal knowledge-preview-modal" role="dialog" aria-modal="true" aria-labelledby="knowledge-preview-title">
        <header className="modal-head">
          <div>
            <p>发布确认</p>
            <h2 id="knowledge-preview-title">{preview.operation === "archive" ? "确认归档知识" : preview.operation === "rollback" ? "确认回滚知识版本" : "确认发布知识"}</h2>
          </div>
          <button type="button" className="icon-button" aria-label="关闭发布确认" disabled={busy} onClick={onCancel}><CloseIcon /></button>
        </header>
        <div className="knowledge-preview-body">
          <div className="knowledge-version-compare">
            <div><span>当前线上版本</span><strong>{preview.current_version}</strong></div>
            <div><span>目标新版本</span><strong>{preview.target_version}</strong></div>
            <div><span>变更类型</span><strong>{operationLabel(preview.operation)}</strong></div>
          </div>
          {preview.can_publish ? (
            <div className="knowledge-validation-success" role="status">
              <strong>自动校验通过</strong>
              <span>确认后将生成新的不可变知识版本，只影响新创建的 AI 对话批次。</span>
            </div>
          ) : (
            <div className="knowledge-validation-errors" role="alert">
              <strong>自动校验未通过</strong>
              {preview.validation_issues.map((issue, index) => (
                <p key={`${issue.field}-${index}`}><b>{issue.problem}</b><span>{issue.suggestion}</span></p>
              ))}
            </div>
          )}
          {firstChange ? (
            <div className="knowledge-diff-grid">
              <section>
                <span>当前线上内容</span>
                <strong>{firstChange.before?.title || "当前线上版本中不存在该知识"}</strong>
                <p>{firstChange.before?.content || "—"}</p>
              </section>
              <section>
                <span>发布后内容</span>
                <strong>{firstChange.after?.title || "归档后从线上知识中移除"}</strong>
                <p>{firstChange.after?.content || "历史内容和发布记录继续保留。"}</p>
              </section>
            </div>
          ) : null}
          {preview.operation === "rollback" && preview.change_set.length > 1 ? (
            <p className="knowledge-change-count">本次回滚共包含 {preview.change_set.length} 条知识差异。</p>
          ) : null}
        </div>
        <footer className="modal-actions">
          <button type="button" disabled={busy} onClick={onCancel}>取消</button>
          {preview.can_publish ? (
            <button type="button" className="primary-button" disabled={busy} onClick={onConfirm}>{busy ? "发布中..." : "确认发布"}</button>
          ) : null}
        </footer>
      </section>
    </div>
  );
}

function CreateKnowledgeModal({ open, value, busy, error, onChange, onCancel, onSave }: {
  open: boolean;
  value: EditorValue;
  busy: boolean;
  error: string | null;
  onChange: (value: EditorValue) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onCancel();
    }}>
      <section className="modal knowledge-create-modal" role="dialog" aria-modal="true" aria-labelledby="knowledge-create-title">
        <header className="modal-head">
          <div><p>知识管理</p><h2 id="knowledge-create-title">新增知识</h2></div>
          <button className="icon-button" type="button" aria-label="关闭新增知识" disabled={busy} onClick={onCancel}><CloseIcon /></button>
        </header>
        <KnowledgeEditorFields value={value} onChange={onChange} disabled={busy} />
        {error ? <p className="knowledge-form-error" role="alert">{error}</p> : null}
        <footer className="modal-actions">
          <button type="button" disabled={busy} onClick={onCancel}>取消</button>
          <button type="button" className="primary-button" disabled={busy} onClick={onSave}>{busy ? "保存中..." : "保存草稿"}</button>
        </footer>
      </section>
    </div>
  );
}

function KnowledgeDetailDrawer({ item, loading, error, editing, draft, busy, onDraftChange, onClose, onEdit, onCancelEdit, onSaveDraft, onPublish, onArchive }: {
  item: KnowledgeItem | null;
  loading: boolean;
  error: string | null;
  editing: boolean;
  draft: EditorValue;
  busy: boolean;
  onDraftChange: (value: EditorValue) => void;
  onClose: () => void;
  onEdit: () => void;
  onCancelEdit: () => void;
  onSaveDraft: () => void;
  onPublish: () => void;
  onArchive: () => void;
}) {
  if (!item && !loading && !error) return null;
  return (
    <aside className={`panel management-drawer standard-management-drawer knowledge-detail-drawer ${editing ? "is-editing" : ""}`} aria-label="知识详情">
      <header className="drawer-head standard-management-drawer-head">
        <div><p>{editing ? "编辑知识" : "知识详情"}</p><h2>{item?.title || "加载中..."}</h2></div>
        <button className="icon-button drawer-close-button" type="button" aria-label="关闭知识详情" onClick={onClose}><CloseIcon /></button>
      </header>
      {loading ? <div className="state-box">正在加载知识详情...</div> : null}
      {error ? <div className="state-box error" role="alert">{error}</div> : null}
      {item && !loading ? (
        editing ? (
          <div className="management-drawer-form knowledge-drawer-edit">
            <div className="management-drawer-scroll"><KnowledgeEditorFields value={draft} onChange={onDraftChange} disabled={busy} /></div>
            <footer className="management-drawer-actions">
              <h3>操作</h3>
              <div className="drawer-actions">
                <button type="button" disabled={busy} onClick={onCancelEdit}>取消</button>
                <button type="button" className="primary-button" disabled={busy} onClick={onSaveDraft}>{busy ? "保存中..." : "保存草稿"}</button>
              </div>
            </footer>
          </div>
        ) : (
          <div className="management-drawer-form knowledge-drawer-body">
            <div className="management-drawer-scroll">
              <section className="drawer-section">
                <h3>知识正文</h3>
                <p className="knowledge-content">{item.content}</p>
              </section>
              <section className="drawer-section">
                <h3>状态与发布信息</h3>
                <dl className="drawer-dl">
                  <div><dt>状态</dt><dd>{item.status === "draft" ? "草稿" : item.status === "published" ? "已发布" : "已归档"}</dd></div>
                  <div><dt>当前版本</dt><dd>{item.release_history?.[0]?.version || "-"}</dd></div>
                  <div><dt>最后编辑人</dt><dd>{item.last_editor_name}</dd></div>
                  <div><dt>更新时间</dt><dd>{formatDateTime(item.updated_at)}</dd></div>
                </dl>
              </section>
            </div>
            <footer className="management-drawer-actions">
              <h3>操作</h3>
              <div className="drawer-actions">
                {item.draft_operation !== "archive" ? <button type="button" onClick={onEdit}>编辑知识</button> : null}
                {item.draft_revision_id ? <button type="button" className="primary-button" disabled={busy} onClick={onPublish}>{busy ? "校验中..." : item.draft_operation === "archive" ? "发布归档" : "发布"}</button> : null}
                {item.status === "published" && !item.draft_revision_id ? <button type="button" className="danger-button" disabled={busy} onClick={onArchive}>归档</button> : null}
              </div>
            </footer>
          </div>
        )
      ) : null}
    </aside>
  );
}

function ReleaseHistoryDrawer({ open, releases, currentVersion, loading, error, onClose, onOpenRelease }: {
  open: boolean;
  releases: KnowledgeReleaseSummary[];
  currentVersion: string;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onOpenRelease: (id: string) => void;
}) {
  if (!open) return null;
  return (
    <aside className="panel management-drawer standard-management-drawer knowledge-release-drawer" aria-label="知识发布记录">
      <header className="drawer-head standard-management-drawer-head">
        <div><p>知识管理</p><h2>发布记录</h2></div>
        <button className="icon-button drawer-close-button" type="button" aria-label="关闭发布记录" onClick={onClose}><CloseIcon /></button>
      </header>
      <div className="management-drawer-scroll knowledge-release-scroll">
        <div className="knowledge-current-release-card"><span>当前线上版本</span><strong>{currentVersion}</strong></div>
        {loading ? <div className="state-box">正在加载发布记录...</div> : null}
        {error ? <div className="state-box error" role="alert">{error}</div> : null}
        <div className="knowledge-release-list">
          {releases.map((release) => (
            <button type="button" key={release.id} onClick={() => onOpenRelease(release.id)}>
              <span><strong>{release.version}</strong>{release.is_current ? <em>当前生效</em> : null}</span>
              <small>{release.change_summary}</small>
              <time>{release.operator_name} · {formatDateTime(release.published_at)}</time>
            </button>
          ))}
        </div>
      </div>
    </aside>
  );
}

function ReleaseDetailModal({ release, loading, error, onClose, onRollback }: {
  release: KnowledgeRelease | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
  onRollback: (id: string) => void;
}) {
  if (!release && !loading && !error) return null;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section className="modal knowledge-release-modal" role="dialog" aria-modal="true" aria-labelledby="knowledge-release-title">
        <header className="modal-head">
          <div><p>知识发布记录</p><h2 id="knowledge-release-title">{release?.version || "版本详情"}</h2></div>
          <button className="icon-button" type="button" aria-label="关闭版本详情" onClick={onClose}><CloseIcon /></button>
        </header>
        {loading ? <div className="state-box">正在加载版本详情...</div> : null}
        {error ? <div className="state-box error" role="alert">{error}</div> : null}
        {release ? (
          <div className="knowledge-release-detail">
            <div className="knowledge-version-compare">
              <div><span>构建结果</span><strong>成功</strong></div>
              <div><span>操作人</span><strong>{release.operator_name}</strong></div>
              <div><span>发布时间</span><strong>{formatDateTime(release.published_at)}</strong></div>
            </div>
            <section><h3>变更摘要</h3><p>{release.change_summary}</p></section>
            <section><h3>版本差异</h3>
              <ul>{release.change_set.map((change) => <li key={`${change.type}-${change.item_id}`}><span>{operationLabel(change.type)}</span><strong>{change.title}</strong></li>)}</ul>
            </section>
            <section><h3>完整快照</h3><p>共 {release.snapshot.length} 条正式知识，摘要 {release.snapshot_sha256.slice(0, 12)}…</p></section>
          </div>
        ) : null}
        <footer className="modal-actions">
          <button type="button" onClick={onClose}>关闭</button>
          {release && !release.is_current ? <button type="button" className="primary-button" onClick={() => onRollback(release.id)}>回滚到此版本</button> : null}
        </footer>
      </section>
    </div>
  );
}

export function KnowledgePage() {
  const [keyword, setKeyword] = useState("");
  const [debouncedKeyword, setDebouncedKeyword] = useState("");
  const [status, setStatus] = useState<KnowledgeFilterStatus>("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<KnowledgeSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeItemId, setActiveItemId] = useState<string | null>(null);
  const [detail, setDetail] = useState<KnowledgeItem | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<EditorValue>({ title: "", content: "" });
  const [createOpen, setCreateOpen] = useState(false);
  const [createDraft, setCreateDraft] = useState<EditorValue>({ title: "", content: "" });
  const [formError, setFormError] = useState<string | null>(null);
  const [preview, setPreview] = useState<KnowledgePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [discardAction, setDiscardAction] = useState<"close-detail" | "cancel-edit" | "close-create" | null>(null);
  const [releasesOpen, setReleasesOpen] = useState(false);
  const [releases, setReleases] = useState<KnowledgeReleaseSummary[]>([]);
  const [releasesLoading, setReleasesLoading] = useState(false);
  const [releasesError, setReleasesError] = useState<string | null>(null);
  const [releaseDetail, setReleaseDetail] = useState<KnowledgeRelease | null>(null);
  const [releaseDetailLoading, setReleaseDetailLoading] = useState(false);
  const [releaseDetailError, setReleaseDetailError] = useState<string | null>(null);
  const [toast, setToast] = useState<{ message: string; tone: "success" | "error" } | null>(null);
  const listRequestId = useRef(0);
  const detailRequestId = useRef(0);

  const detailDirty = editing && detail !== null && (draft.title !== detail.title || draft.content !== detail.content);
  const createDirty = Boolean(createDraft.title || createDraft.content);
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedKeyword(keyword.trim());
      setPage(1);
    }, 320);
    return () => window.clearTimeout(timer);
  }, [keyword]);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const requestId = ++listRequestId.current;
    setLoading(true);
    setError(null);
    try {
      const [list, dashboard] = await Promise.all([
        listKnowledgeItems({ keyword: debouncedKeyword, status, page, page_size: pageSize }, signal),
        getKnowledgeSummary(signal),
      ]);
      if (signal?.aborted || requestId !== listRequestId.current) return;
      setItems(list.items);
      setTotal(list.total);
      setSummary(dashboard);
      const maxPage = Math.max(1, Math.ceil(list.total / pageSize));
      if (page > maxPage) setPage(maxPage);
    } catch (err) {
      if (signal?.aborted || requestId !== listRequestId.current) return;
      setError(formatBusinessError(err, "知识列表加载失败，请稍后重试。"));
    } finally {
      if (!signal?.aborted && requestId === listRequestId.current) setLoading(false);
    }
  }, [debouncedKeyword, page, pageSize, status]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (!activeItemId) {
      setDetail(null);
      setEditing(false);
      return;
    }
    const controller = new AbortController();
    const requestId = ++detailRequestId.current;
    setDetailLoading(true);
    setDetailError(null);
    void getKnowledgeItem(activeItemId, controller.signal)
      .then((item) => {
        if (controller.signal.aborted || requestId !== detailRequestId.current) return;
        setDetail(item);
        setDraft({ title: item.title, content: item.content });
      })
      .catch((err) => {
        if (!controller.signal.aborted && requestId === detailRequestId.current) setDetailError(formatBusinessError(err, "知识详情加载失败，请稍后重试。"));
      })
      .finally(() => {
        if (!controller.signal.aborted && requestId === detailRequestId.current) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [activeItemId]);

  const notify = useCallback((message: string, tone: "success" | "error" = "success") => setToast({ message, tone }), []);
  const isEmpty = !loading && !error && items.length === 0;
  const filteredEmpty = isEmpty && (Boolean(debouncedKeyword) || status !== "all");

  async function saveNewDraft() {
    setBusy(true);
    setFormError(null);
    try {
      const item = await createKnowledgeDraft(createDraft);
      setCreateOpen(false);
      setCreateDraft({ title: "", content: "" });
      setDetail(item);
      setActiveItemId(item.id);
      setDraft({ title: item.title, content: item.content });
      await refresh();
      notify("草稿已保存，确认无误后可发布");
    } catch (err) {
      const message = formatBusinessError(err, "知识草稿保存失败，请稍后重试。");
      setFormError(message);
      notify(message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function saveExistingDraft() {
    if (!detail) return;
    setBusy(true);
    setFormError(null);
    try {
      const item = await updateKnowledgeDraft(detail.id, {
        title: draft.title,
        content: draft.content,
        expected_updated_at: detail.updated_at,
      });
      setDetail(item);
      setDraft({ title: item.title, content: item.content });
      setEditing(false);
      await refresh();
      notify("草稿已保存，线上知识未变更");
    } catch (err) {
      const message = formatBusinessError(err, "知识草稿保存失败，请稍后重试。");
      setFormError(message);
      notify(message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function startDraftPreview() {
    if (!detail?.draft_revision_id || !detail.draft_operation) return;
    setBusy(true);
    setFormError(null);
    try {
      setPreview(await previewKnowledgeRelease({
        operation: detail.draft_operation,
        item_id: detail.id,
        expected_updated_at: detail.updated_at,
      }));
    } catch (err) {
      const message = formatBusinessError(err, "知识校验失败，请稍后重试。");
      setFormError(message);
      notify(message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function stageArchiveAndPreview() {
    if (!detail) return;
    setBusy(true);
    try {
      const item = await stageKnowledgeArchive(detail.id);
      setDetail(item);
      setDraft({ title: item.title, content: item.content });
      setPreview(await previewKnowledgeRelease({
        operation: "archive",
        item_id: item.id,
        expected_updated_at: item.updated_at,
      }));
    } catch (err) {
      notify(formatBusinessError(err, "归档校验失败，线上知识未变更。"), "error");
    } finally {
      setBusy(false);
    }
  }

  async function confirmPreview() {
    if (!preview) return;
    setBusy(true);
    try {
      const result = await confirmKnowledgeRelease(preview.preview_id, preview.content_digest);
      const operation = preview.operation;
      setPreview(null);
      setCreateOpen(false);
      setCreateDraft({ title: "", content: "" });
      setEditing(false);
      setReleaseDetail(null);
      if (result.item) {
        setDetail(result.item);
        setActiveItemId(result.item.id);
        setDraft({ title: result.item.title, content: result.item.content });
      } else if (operation === "rollback") {
        setActiveItemId(null);
        setReleasesOpen(false);
      }
      await refresh();
      notify(operation === "archive" ? "知识已归档，新线上版本已发布" : operation === "rollback" ? "已回滚并生成新的知识版本" : "知识发布成功，新创建的 AI 对话批次将使用此版本");
    } catch (err) {
      notify(formatBusinessError(err, "知识发布失败，当前线上版本未发生变化。"), "error");
    } finally {
      setBusy(false);
    }
  }

  async function openReleases() {
    setReleasesOpen(true);
    setReleasesLoading(true);
    setReleasesError(null);
    try {
      const result = await listKnowledgeReleases(1, 50);
      setReleases(result.items);
    } catch (err) {
      setReleasesError(formatBusinessError(err, "发布记录加载失败，请稍后重试。"));
    } finally {
      setReleasesLoading(false);
    }
  }

  async function openReleaseDetail(id: string) {
    setReleaseDetailLoading(true);
    setReleaseDetailError(null);
    try {
      setReleaseDetail(await getKnowledgeRelease(id));
    } catch (err) {
      setReleaseDetailError(formatBusinessError(err, "版本详情加载失败，请稍后重试。"));
    } finally {
      setReleaseDetailLoading(false);
    }
  }

  async function startRollback(id: string) {
    setBusy(true);
    try {
      setPreview(await previewKnowledgeRollback(id));
      setReleaseDetail(null);
    } catch (err) {
      notify(formatBusinessError(err, "回滚预览生成失败，请稍后重试。"), "error");
    } finally {
      setBusy(false);
    }
  }

  function closeDetail() {
    if (detailDirty) {
      setDiscardAction("close-detail");
      return;
    }
    setActiveItemId(null);
  }

  function cancelEdit() {
    if (detailDirty) {
      setDiscardAction("cancel-edit");
      return;
    }
    setEditing(false);
  }

  function closeCreate() {
    if (createDirty) {
      setDiscardAction("close-create");
      return;
    }
    setCreateOpen(false);
  }

  const discardCopy = useMemo(() => {
    if (discardAction === "close-create") return { title: "放弃新增知识？", description: "已填写的标题和正文不会保存。", label: "放弃并关闭" };
    if (discardAction === "cancel-edit") return { title: "放弃未发布的修改？", description: "当前编辑内容不会保存，线上知识不受影响。", label: "放弃修改" };
    return { title: "放弃未发布的修改？", description: "当前编辑内容不会保存，线上知识不受影响。", label: "放弃并关闭" };
  }, [discardAction]);

  return (
    <div className="knowledge-page">
      <header className="page-header">
        <div><p className="eyebrow">业务知识运营</p><h1>知识管理</h1></div>
        <button type="button" className="primary-button" onClick={() => { setCreateDraft({ title: "", content: "" }); setFormError(null); setCreateOpen(true); }}>新增知识</button>
      </header>

      <section className="metric-grid" aria-label="知识概览">
        <article className="knowledge-metric-interactive">
          <button type="button" onClick={() => void openReleases()}>
            <span>当前线上版本</span><strong>{summary?.current_release.version || "—"}</strong><p>{summary ? formatDateTime(summary.current_release.published_at) : "正在加载"}</p>
          </button>
        </article>
        <article><span>今日发布</span><strong>{summary?.published_today ?? "—"}</strong><p>{summary ? `${summary.published_today_breakdown.create} 新增 · ${summary.published_today_breakdown.update} 修改 · ${summary.published_today_breakdown.archive} 归档` : "成功发布次数"}</p></article>
        <article><span>已发布知识</span><strong>{summary?.published_count ?? "—"}</strong><p>累计已归档 {summary?.archived_count ?? "—"} 条</p></article>
      </section>

      <div className="management-grid knowledge-management-grid">
        <section className="panel management-list-panel knowledge-list-panel">
          <header className="panel-header"><div><h2>知识列表</h2><p>发布成功后，仅新创建的 AI 对话批次使用新版本</p></div></header>
          <div className="management-filter-card two-field-management-filter knowledge-filter-card">
            <label className="management-search-field"><span>搜索</span><input type="search" value={keyword} placeholder="标题或正文关键词" aria-label="搜索知识" onChange={(event) => setKeyword(event.target.value)} /></label>
            <label><span>状态</span><select aria-label="筛选知识状态" value={status} onChange={(event) => { setStatus(event.target.value as KnowledgeFilterStatus); setPage(1); }}><option value="all">全部状态</option><option value="draft">草稿</option><option value="published">已发布</option><option value="archived">已归档</option></select></label>
          </div>
          <div className="paginated-management-table-card knowledge-table-card">
            {loading ? <div className="management-empty-state"><span className="loading-spinner" aria-hidden="true" /><strong>正在加载知识列表</strong><span>请稍候。</span></div> : error ? <div className="management-empty-state error" role="alert"><strong>知识列表加载失败</strong><span>{error}</span><button type="button" onClick={() => void refresh()}>重新加载</button></div> : isEmpty ? <div className="management-empty-state"><strong>{filteredEmpty ? "没有符合条件的知识" : "暂无知识"}</strong><span>{filteredEmpty ? "调整关键词或状态后再试" : "点击右上角新增第一条正式知识"}</span>{filteredEmpty ? <button type="button" onClick={() => { setKeyword(""); setStatus("all"); }}>清除筛选</button> : null}</div> : (
              <table className="management-table knowledge-table">
                <thead><tr><th>知识标题</th><th>状态</th><th>最后编辑人</th><th>最后更新时间</th></tr></thead>
                <tbody>
                  {items.map((item) => (
                    <tr key={item.id} tabIndex={0} className={activeItemId === item.id ? "active-row" : undefined} onClick={() => { setReleasesOpen(false); setActiveItemId(item.id); }} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setReleasesOpen(false); setActiveItemId(item.id); } }}>
                      <td title={item.title}><strong>{item.title}</strong></td>
                      <td><KnowledgeStatusBadge status={item.status} /></td>
                      <td>{item.last_editor_name}</td>
                      <td>{formatDateTime(item.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          {!loading && !error && items.length > 0 ? (
            <Pagination
              ariaLabel="知识列表分页"
              className="paginated-management-pagination-row"
              currentPage={page}
              disabled={loading}
              pageSize={pageSize}
              pageSizeAriaLabel="每页知识数量"
              pageSizeOptions={pageSizeOptions}
              total={total}
              totalPages={totalPages}
              totalUnit="条知识"
              onPageChange={setPage}
              onPageSizeChange={(value) => { setPageSize(value); setPage(1); }}
            />
          ) : null}
        </section>

        {releasesOpen ? (
          <ReleaseHistoryDrawer open releases={releases} currentVersion={summary?.current_release.version || "—"} loading={releasesLoading} error={releasesError} onClose={() => setReleasesOpen(false)} onOpenRelease={(id) => void openReleaseDetail(id)} />
        ) : activeItemId ? (
          <KnowledgeDetailDrawer item={detail} loading={detailLoading} error={detailError} editing={editing} draft={draft} busy={busy} onDraftChange={setDraft} onClose={closeDetail} onEdit={() => { if (detail) { setDraft({ title: detail.title, content: detail.content }); setEditing(true); } }} onCancelEdit={cancelEdit} onSaveDraft={() => void saveExistingDraft()} onPublish={() => void startDraftPreview()} onArchive={() => void stageArchiveAndPreview()} />
        ) : null}
      </div>

      <CreateKnowledgeModal open={createOpen} value={createDraft} busy={busy} error={formError} onChange={setCreateDraft} onCancel={closeCreate} onSave={() => void saveNewDraft()} />
      <PublishPreviewModal preview={preview} busy={busy} onCancel={() => setPreview(null)} onConfirm={() => void confirmPreview()} />
      <ReleaseDetailModal release={releaseDetail} loading={releaseDetailLoading} error={releaseDetailError} onClose={() => { setReleaseDetail(null); setReleaseDetailError(null); }} onRollback={(id) => void startRollback(id)} />
      <ConfirmModal open={discardAction !== null} title={discardCopy.title} description={discardCopy.description} confirmLabel={discardCopy.label} dangerous onCancel={() => setDiscardAction(null)} onConfirm={() => {
        if (discardAction === "close-create") { setCreateOpen(false); setCreateDraft({ title: "", content: "" }); }
        else if (discardAction === "cancel-edit") { if (detail) setDraft({ title: detail.title, content: detail.content }); setEditing(false); }
        else { setActiveItemId(null); setEditing(false); }
        setDiscardAction(null);
      }} />
      <Toast message={toast?.message || null} tone={toast?.tone} onDismiss={() => setToast(null)} />
    </div>
  );
}
