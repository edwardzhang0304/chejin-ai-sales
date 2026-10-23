type Props = {
  activeModule: string | null;
};

export function EmptyWorkspace({ activeModule }: Props) {
  return (
    <div className="empty-workspace">
      <div className="empty-panel">
        <span className="empty-icon">车</span>
        <h1>{activeModule ? "该模块正在迁移中" : "请选择左侧模块"}</h1>

      </div>
    </div>
  );
}
