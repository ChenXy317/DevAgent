import { useEffect, useRef, useState } from "react";

import type { ChatMessage } from "../api/rest";
import {
  canCompress,
  requestCompress,
  requestStop,
  rewindTo,
  selectSession,
  sendDecision,
  sendText,
  useAgent,
  type Connection,
  type LiveTool,
} from "../store/agentStore";
import { ApprovalModal } from "./ApprovalModal";
import { ThinkingBlock } from "./ThinkingBlock";
import { ToolCallCard } from "./ToolCallCard";

const RUN_LABEL: Record<string, string> = {
  idle: "空闲",
  running: "运行中",
  awaiting_approval: "等待审批",
  done: "已完成",
  error: "出错",
};

const CONNECTION_LABEL: Record<Connection, string> = {
  idle: "未连接",
  connecting: "连接中",
  open: "已连接",
  closed: "已断开",
  error: "连接失败",
};

function formatArgs(value: unknown) {
  if (value == null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

function decisionLabel(message: { decision?: string; prefix?: string }) {
  if (message.decision === "超时") {
    return "两分钟未选择，已拒绝";
  }
  if (message.decision === "以后都允许") {
    return message.prefix ? `已写入前缀 ${message.prefix}` : "已写入前缀";
  }
  if (message.decision === "拒绝") {
    return "已拒绝";
  }
  if (message.decision === "本次允许") {
    return "本次允许";
  }
  if (message.decision === "直接执行") {
    return "直接执行";
  }
  return message.decision || "";
}

function MessageView({
  message,
  onRewind,
}: {
  message: ChatMessage;
  onRewind?: () => void;
}) {
  if (message.role === "tool") {
    return (
      <ToolCallCard
        name={message.tool_name || "工具"}
        detail={formatArgs(message.arguments)}
        output={message.is_error ? "" : message.content}
        error={message.is_error ? message.content : ""}
        status={decisionLabel(message)}
        before={message.diff ? message.diff.before : undefined}
        after={message.diff ? message.diff.after : undefined}
      />
    );
  }
  return (
    <article className={`bubble ${message.role}`}>
      {message.thinking ? <ThinkingBlock text={message.thinking} /> : null}
      {message.content ? <p>{message.content}</p> : null}
      {message.role === "user" && onRewind ? (
        <button type="button" className="rewind-button" onClick={onRewind}>
          回退
        </button>
      ) : null}
    </article>
  );
}

function LiveToolView({ tool }: { tool: LiveTool }) {
  return <ToolCallCard name={tool.name} detail={tool.detail} output={tool.output} status={tool.status} />;
}

export function Chat({
  title,
  onOpenSessions,
  onOpenSettings,
}: {
  title: string;
  onOpenSessions: () => void;
  onOpenSettings: () => void;
}) {
  const agent = useAgent();
  const [draft, setDraft] = useState("");
  const [stopping, setStopping] = useState(false);
  const [foldOpen, setFoldOpen] = useState(false);
  const timeline = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const wasCompressing = useRef(false);
  const running = agent.runState === "running" || agent.runState === "awaiting_approval";
  function rewind(messageId: string) {
    if (running || agent.compressing) {
      return;
    }
    if (window.confirm("回退到这条消息之前。之后的写入会撤掉，命令不会撤")) {
      void rewindTo(messageId);
    }
  }
  const compressEnabled = canCompress();
  const compressTitle = agent.compressing
    ? "正在压缩"
    : running
      ? "正在运行"
      : agent.messages.length === 0
        ? "没有消息"
        : compressEnabled
          ? "压缩上下文"
          : "没有更早的消息可压缩";
  const pivot = agent.compactedBeforeId
    ? agent.messages.findIndex((message) => message.id === agent.compactedBeforeId)
    : -1;
  const older = pivot >= 0 ? agent.messages.slice(0, pivot + 1) : [];
  const recent = pivot >= 0 ? agent.messages.slice(pivot + 1) : agent.messages;
  const summaryText = agent.compressing ? agent.compressDraft || "正在压缩" : agent.summary;

  useEffect(() => {
    if (!running && !agent.compressing) {
      setStopping(false);
    }
  }, [running, agent.compressing]);

  useEffect(() => {
    if (agent.compressing) {
      wasCompressing.current = true;
      setFoldOpen(true);
    } else if (wasCompressing.current) {
      wasCompressing.current = false;
      setFoldOpen(true);
    }
  }, [agent.compressing]);

  useEffect(() => {
    const box = timeline.current;
    if (box && stick.current) {
      box.scrollTop = box.scrollHeight;
    }
  }, [agent.messages, agent.live, agent.liveTools, agent.notice, agent.approval, agent.compressDraft, agent.approvalNote]);

  function submit() {
    if (running || agent.compressing) {
      setStopping(true);
      requestStop();
      return;
    }
    if (sendText(draft)) {
      setDraft("");
    }
  }

  return (
    <section className="chat">
      <header className="topbar">
        <button type="button" className="menu-button" onClick={onOpenSessions}>
          会话
        </button>
        <strong className="title">{title}</strong>
        <span>{CONNECTION_LABEL[agent.connection]}</span>
        <span>{RUN_LABEL[agent.runState] ?? agent.runState}</span>
        {agent.contextChars !== null ? (
          <span>
            {agent.contextChars} / {agent.contextLimit ?? agent.llm?.context_limit ?? 0}
          </span>
        ) : null}
        <button type="button" disabled={!compressEnabled} title={compressTitle} onClick={requestCompress}>
          {agent.compressing ? "正在压缩" : "压缩上下文"}
        </button>
        <button
          type="button"
          disabled={!running && !agent.compressing}
          title={running || agent.compressing ? "停止" : "当前没有运行"}
          onClick={() => {
            setStopping(true);
            requestStop();
          }}
        >
          {stopping ? "正在停止" : "停止"}
        </button>
        <button type="button" onClick={onOpenSettings}>
          设置
        </button>
      </header>
      {agent.notice ? <p className="notice">{agent.notice}</p> : null}
      <div
        className="timeline"
        ref={timeline}
        onScroll={() => {
          const box = timeline.current;
          if (!box) {
            return;
          }
          stick.current = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
        }}
      >
        {agent.loadingMessages ? <p className="muted">加载中</p> : null}
        {agent.messageError ? (
          <p className="notice">
            {agent.messageError}
            <button type="button" onClick={() => void selectSession(agent.currentId || "")}>
              重试
            </button>
          </p>
        ) : null}
        {older.length > 0 || agent.compressing ? (
          <details
            className="folded"
            open={foldOpen}
            onToggle={(event) => setFoldOpen(event.currentTarget.open)}
          >
            <summary>已压缩的历史</summary>
            {summaryText ? <p>{summaryText}</p> : null}
            {older.map((message) => (
              <MessageView
                key={message.id}
                message={message}
                onRewind={
                  message.role === "user" && !running && !agent.compressing
                    ? () => rewind(message.id)
                    : undefined
                }
              />
            ))}
          </details>
        ) : null}
        {recent.map((message) => (
          <MessageView
            key={message.id}
            message={message}
            onRewind={
              message.role === "user" && !running && !agent.compressing
                ? () => rewind(message.id)
                : undefined
            }
          />
        ))}
        {agent.live ? <MessageView message={agent.live} /> : null}
        {agent.liveTools.map((tool) => (
          <LiveToolView key={tool.id} tool={tool} />
        ))}
        {agent.approval ? <ApprovalModal request={agent.approval} onDecide={sendDecision} /> : null}
        {agent.approvalNote ? <p className="approval-note">{agent.approvalNote}</p> : null}
      </div>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <textarea
          value={draft}
          placeholder="输入任务"
          rows={3}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              if (!running && !agent.compressing) {
                submit();
              }
            }
          }}
        />
        <button type="submit">{stopping ? "正在停止" : running || agent.compressing ? "停止" : "发送"}</button>
      </form>
    </section>
  );
}
