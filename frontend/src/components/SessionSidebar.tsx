import type { Session } from "../api/rest";

export function SessionSidebar({
  sessions,
  currentId,
  creating,
  loading,
  error,
  deletingId,
  onCreate,
  onSelect,
  onDelete,
  onRetry,
}: {
  sessions: Session[];
  currentId: string | null;
  creating: boolean;
  loading: boolean;
  error: string;
  deletingId: string | null;
  onCreate: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRetry: () => void;
}) {
  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <h1>会话</h1>
        <button type="button" onClick={onCreate} disabled={creating}>
          {creating ? "创建中" : "新建"}
        </button>
      </div>
      {loading ? <p className="muted">加载中</p> : null}
      {error ? (
        <p className="notice">
          {error}
          <button type="button" onClick={onRetry}>
            重试
          </button>
        </p>
      ) : null}
      <ul>
        {sessions.map((session) => {
          const deleting = deletingId === session.id;
          return (
            <li key={session.id} className={session.id === currentId ? "active" : ""}>
              <button
                type="button"
                className="session-name"
                disabled={deleting}
                onClick={() => onSelect(session.id)}
              >
                {session.title || "新会话"}
              </button>
              <button
                type="button"
                className="text-button"
                disabled={deleting}
                onClick={() => {
                  if (window.confirm("删除这个会话和工作目录中的文件")) {
                    onDelete(session.id);
                  }
                }}
              >
                {deleting ? "删除中" : "删除"}
              </button>
            </li>
          );
        })}
      </ul>
      {!loading && sessions.length === 0 ? <p className="muted">还没有会话</p> : null}
    </aside>
  );
}
