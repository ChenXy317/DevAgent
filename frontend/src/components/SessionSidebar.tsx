import { useState } from "react";

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
  onCreate: (workdir: string) => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRetry: () => void;
}) {
  const [path, setPath] = useState("");
  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <h1>会话</h1>
        <button
          type="button"
          onClick={() => {
            onCreate(path.trim());
            setPath("");
          }}
          disabled={creating}
        >
          {creating ? "创建中" : "新建"}
        </button>
      </div>
      <label className="sidebar-path">
        目录
        <input
          value={path}
          placeholder="可留空"
          onChange={(event) => setPath(event.target.value)}
        />
      </label>
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
                  const text = session.workdir
                    ? "删除这个会话。绑定的目录不会删除"
                    : "删除这个会话和工作目录中的文件";
                  if (window.confirm(text)) {
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
