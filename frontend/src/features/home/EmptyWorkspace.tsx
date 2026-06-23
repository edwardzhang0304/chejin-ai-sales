type Props = {
  activeModule: string | null;
};

export function EmptyWorkspace({ activeModule }: Props) {
  return (
    <div className="empty-workspace">
      <div className="empty-panel">
        <span className="empty-icon">车</span>
        <h1>{activeModule ? "该模块正在迁移中" : "请选择左侧模块"}</h1>
        <p>系统会根据当前账号权限展示可访问模块。选择线索管理后，可进行客户录入、去重、列表查看和自动分配结果追踪。</p>
      </div>
    </div>
  );
}
