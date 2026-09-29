import { useEffect, useState } from "react";

import { Chat } from "./components/Chat";
import { SessionSidebar } from "./components/SessionSidebar";
import { Settings } from "./components/Settings";
import { loadSessions, newSession, removeSession, selectSession, useAgent } from "./store/agentStore";

export function App() {
  const agent = useAgent();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const current = agent.sessions.find((session) => session.id === agent.currentId);

  useEffect(() => {
    void loadSessions();
  }, []);

  return (
    <div className="app">
      <div className={sidebarOpen ? "sidebar-wrap open" : "sidebar-wrap"}>
        <SessionSidebar
          sessions={agent.sessions}
          currentId={agent.currentId}
          creating={agent.creating}
          loading={agent.loadingSessions}
          error={agent.sessionError}
          deletingId={agent.deletingId}
          onCreate={() => {
            void newSession();
            setSidebarOpen(false);
          }}
          onSelect={(id) => {
            void selectSession(id);
            setSidebarOpen(false);
          }}
          onDelete={(id) => {
            void removeSession(id);
          }}
          onRetry={() => {
            void loadSessions();
          }}
        />
      </div>
      <main className="main">
        {agent.currentId ? (
          <Chat
            title={current?.title || "新会话"}
            onOpenSessions={() => setSidebarOpen((open) => !open)}
            onOpenSettings={() => setSettingsOpen(true)}
          />
        ) : (
          <section className="chat empty-chat">
            <header className="topbar">
              <button type="button" className="menu-button" onClick={() => setSidebarOpen((open) => !open)}>
                会话
              </button>
              <strong className="title">未选择会话</strong>
              <button type="button" onClick={() => setSettingsOpen(true)}>
                设置
              </button>
            </header>
            <p className="muted">新建一个会话后开始。</p>
          </section>
        )}
        {settingsOpen ? <Settings onClose={() => setSettingsOpen(false)} /> : null}
      </main>
    </div>
  );
}
