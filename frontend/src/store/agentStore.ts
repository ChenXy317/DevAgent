import { useSyncExternalStore } from "react";

import {
  createSession,
  deleteSession,
  listMessages,
  listSessions,
  readLlm,
  type ChatMessage,
  type LlmSettings,
  type Session,
} from "../api/rest";
import { openSessionSocket, type ApprovalRequest, type ServerEvent, type SessionSocket } from "../api/ws";

export type Connection = "connecting" | "open" | "closed" | "error" | "idle";

export type LiveTool = {
  id: string;
  name: string;
  detail: string;
  output: string;
  status: string;
};

type AgentState = {
  sessions: Session[];
  currentId: string | null;
  messages: ChatMessage[];
  connection: Connection;
  runState: string;
  notice: string;
  approval: ApprovalRequest | null;
  approvalNote: string;
  llm: LlmSettings | null;
  creating: boolean;
  loadingSessions: boolean;
  loadingMessages: boolean;
  sessionError: string;
  messageError: string;
  deletingId: string | null;
  summary: string;
  compactedBeforeId: string | null;
  compressing: boolean;
  compressDraft: string;
  live: ChatMessage | null;
  liveTools: LiveTool[];
};

let state: AgentState = {
  sessions: [],
  currentId: null,
  messages: [],
  connection: "idle",
  runState: "idle",
  notice: "",
  approval: null,
  approvalNote: "",
  llm: null,
  creating: false,
  loadingSessions: false,
  loadingMessages: false,
  sessionError: "",
  messageError: "",
  deletingId: null,
  summary: "",
  compactedBeforeId: null,
  compressing: false,
  compressDraft: "",
  live: null,
  liveTools: [],
};

const listeners = new Set<() => void>();
let socket: SessionSocket | null = null;
let socketToken = 0;
let reconnectTimer = 0;
let reconnecting = false;

function setState(patch: Partial<AgentState>) {
  state = { ...state, ...patch };
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function turnCount(messages: ChatMessage[]) {
  let count = 0;
  for (const message of messages) {
    if (message.role === "user") {
      count += 1;
    }
  }
  if (count === 0 && messages.length > 0) {
    return 1;
  }
  return count;
}

export function canCompress() {
  const turns = state.llm?.context_turns ?? 20;
  const busy = state.runState === "running" || state.runState === "awaiting_approval" || state.compressing;
  return !busy && turnCount(state.messages) > turns;
}

function patchSession(id: string, patch: Partial<Session>) {
  return state.sessions.map((session) => (session.id === id ? { ...session, ...patch } : session));
}

function upsertMessage(message: ChatMessage) {
  const index = state.messages.findIndex((item) => item.id === message.id);
  const messages = [...state.messages];
  if (index >= 0) {
    messages[index] = { ...messages[index], ...message };
  } else {
    messages.push(message);
  }
  setState({ messages, live: state.live?.id === message.id ? null : state.live });
}

function appendChannel(id: string, channel: string, text: string) {
  if (state.live?.id === id) {
    const live = { ...state.live };
    if (channel === "thinking") {
      live.thinking = `${live.thinking ?? ""}${text}`;
    } else {
      live.content += text;
    }
    setState({ live });
    return;
  }
  setState({
    messages: state.messages.map((message) => {
      if (message.id !== id) {
        return message;
      }
      if (channel === "thinking") {
        return { ...message, thinking: `${message.thinking ?? ""}${text}` };
      }
      return { ...message, content: message.content + text };
    }),
  });
}

function handleEvent(token: number, event: ServerEvent) {
  if (token !== socketToken) {
    return;
  }
  if (event.type === "session.hello") {
    const transient = new Set(["连接中", "正在重连", "连接失败", "已断开", "连接已断开"]);
    const current = state.sessions.find((session) => session.id === event.session_id);
    setState({
      messages: event.messages ?? [],
      runState: event.state ?? "idle",
      approval: event.approval ?? null,
      approvalNote: "",
      summary: event.summary ?? current?.summary ?? "",
      compactedBeforeId: event.compacted_before_id ?? current?.compacted_before_id ?? null,
      live: null,
      liveTools: [],
      compressing: false,
      compressDraft: "",
      loadingMessages: false,
      messageError: "",
      notice: reconnecting ? "已重新连接" : transient.has(state.notice) ? "" : state.notice,
      sessions: event.session_id
        ? patchSession(event.session_id, {
            title: event.title ?? current?.title ?? "",
            summary: event.summary ?? current?.summary ?? "",
            compacted_before_id: event.compacted_before_id ?? current?.compacted_before_id ?? null,
          })
        : state.sessions,
    });
    reconnecting = false;
    return;
  }
  if (event.type === "agent.state" && event.state) {
    setState({ runState: event.state });
    return;
  }
  if (event.type === "message.start" && event.id) {
    setState({
      live: {
        id: event.id,
        role: event.role || "assistant",
        content: "",
        thinking: "",
        tool_calls: null,
        tool_call_id: null,
        created_at: event.created_at || "",
      },
    });
    return;
  }
  if (event.type === "message.delta" && event.id && event.text) {
    appendChannel(event.id, event.channel || "content", event.text);
    return;
  }
  if (event.type === "message.done" && event.id) {
    upsertMessage({
      id: event.id,
      role: event.role || "assistant",
      content: event.content || "",
      thinking: event.thinking ?? null,
      tool_calls: event.tool_calls ?? null,
      tool_call_id: null,
      created_at: event.created_at || "",
    });
    if (event.role === "user" && event.title && state.currentId) {
      setState({ sessions: patchSession(state.currentId, { title: event.title }) });
    }
    return;
  }
  if (event.type === "tool.start" && event.tool_call_id) {
    setState({
      liveTools: [
        ...state.liveTools,
        {
          id: event.tool_call_id,
          name: event.name || "工具",
          detail: JSON.stringify(event.arguments ?? {}, null, 2),
          output: "",
          status: "执行中",
        },
      ],
    });
    return;
  }
  if (event.type === "tool.progress" && event.tool_call_id) {
    setState({
      liveTools: state.liveTools.map((tool) =>
        tool.id === event.tool_call_id
          ? { ...tool, output: `${tool.output}${event.text || ""}`.slice(-8000) }
          : tool,
      ),
    });
    return;
  }
  if (event.type === "tool.result" && event.tool_call_id) {
    upsertMessage({
      id: event.id || event.tool_call_id,
      role: "tool",
      content: event.text || "",
      thinking: null,
      tool_calls: null,
      tool_call_id: event.tool_call_id,
      created_at: "",
      decision: event.decision,
      is_error: event.is_error,
      tool_name: event.name,
      arguments: event.arguments,
      prefix: event.prefix,
      diff: event.diff,
    });
    setState({ liveTools: state.liveTools.filter((tool) => tool.id !== event.tool_call_id) });
    return;
  }
  if (event.type === "approval.request") {
    setState({
      approval: {
        id: event.id,
        tool: event.tool,
        arguments: event.arguments,
        prefix: event.prefix,
      },
      approvalNote: "",
    });
    return;
  }
  if (event.type === "approval.resolved") {
    setState({
      approval: null,
      approvalNote: event.decision === "timeout" ? "两分钟未选择，已拒绝" : "",
    });
    return;
  }
  if (event.type === "compress.start") {
    setState({ compressing: true, compressDraft: "" });
    return;
  }
  if (event.type === "compress.delta") {
    setState({ compressDraft: state.compressDraft + (event.text || "") });
    return;
  }
  if (event.type === "compress.done") {
    setState({
      compressing: false,
      compressDraft: "",
      summary: event.summary || "",
      compactedBeforeId: event.compacted_before_id ?? state.compactedBeforeId,
      sessions: state.currentId
        ? patchSession(state.currentId, {
            summary: event.summary || "",
            compacted_before_id: event.compacted_before_id ?? state.compactedBeforeId,
          })
        : state.sessions,
    });
    return;
  }
  if (event.type === "session.deleted" && state.currentId) {
    setState({ sessions: state.sessions.filter((session) => session.id !== state.currentId) });
    return;
  }
  if (event.type === "error") {
    setState({ notice: event.message ?? "出错" });
  }
}

function connect(sessionId: string, token: number) {
  socket?.close();
  window.clearTimeout(reconnectTimer);
  socket = openSessionSocket(
    sessionId,
    (event) => handleEvent(token, event),
    (status) => {
      if (token !== socketToken) {
        return;
      }
      if (status === "error") {
        setState({ connection: status, notice: "连接失败" });
        return;
      }
      if (status === "closed") {
        setState({ connection: status, notice: "正在重连" });
        reconnecting = true;
        reconnectTimer = window.setTimeout(() => {
          if (token === socketToken && state.currentId === sessionId) {
            connect(sessionId, token);
          }
        }, 1000);
        return;
      }
      setState({ connection: status, notice: status === "connecting" ? "连接中" : state.notice });
    },
  );
}

export async function loadSessions() {
  setState({ loadingSessions: true, sessionError: "" });
  try {
    const [sessions, llm] = await Promise.all([listSessions(), readLlm()]);
    setState({ sessions, llm, loadingSessions: false });
  } catch (error) {
    setState({
      loadingSessions: false,
      sessionError: error instanceof Error ? error.message : "加载失败",
    });
  }
}

export async function selectSession(id: string) {
  const token = ++socketToken;
  reconnecting = false;
  const current = state.sessions.find((session) => session.id === id);
  setState({
    currentId: id,
    messages: [],
    notice: "",
    approval: null,
    approvalNote: "",
    connection: "connecting",
    runState: "idle",
    loadingMessages: true,
    messageError: "",
    summary: current?.summary ?? "",
    compactedBeforeId: current?.compacted_before_id ?? null,
    compressing: false,
    compressDraft: "",
    live: null,
    liveTools: [],
  });
  connect(id, token);
  try {
    const messages = await listMessages(id);
    if (token === socketToken && state.messages.length === 0) {
      setState({ messages, loadingMessages: false });
    } else if (token === socketToken) {
      setState({ loadingMessages: false });
    }
  } catch (error) {
    if (token === socketToken && state.messages.length === 0) {
      setState({
        loadingMessages: false,
        messageError: error instanceof Error ? error.message : "加载失败",
      });
    }
  }
}

export async function newSession() {
  setState({ creating: true, sessionError: "" });
  try {
    const session = await createSession();
    setState({ sessions: [session, ...state.sessions], creating: false });
    await selectSession(session.id);
  } catch (error) {
    setState({
      creating: false,
      sessionError: error instanceof Error ? error.message : "创建失败",
    });
  }
}

export async function removeSession(id: string) {
  setState({ deletingId: id, sessionError: "" });
  try {
    await deleteSession(id);
  } catch (error) {
    setState({
      deletingId: null,
      sessionError: error instanceof Error ? error.message : "删除失败",
    });
    return;
  }
  const sessions = state.sessions.filter((session) => session.id !== id);
  setState({ sessions, deletingId: null });
  if (state.currentId === id) {
    socketToken += 1;
    socket?.close();
    socket = null;
    const next = sessions[0];
    if (next) {
      await selectSession(next.id);
    } else {
      setState({
        currentId: null,
        messages: [],
        connection: "idle",
        runState: "idle",
        notice: "",
        summary: "",
        compactedBeforeId: null,
        live: null,
        liveTools: [],
      });
    }
  }
}

export function refreshLlm(llm: LlmSettings) {
  setState({ llm });
}

export function sendText(text: string) {
  const content = text.trim();
  if (!content) {
    setState({ notice: "请输入内容" });
    return false;
  }
  if (!state.llm?.base_url || !state.llm.model) {
    setState({ notice: "请先在设置里填写模型" });
    return false;
  }
  if (!socket || state.connection !== "open") {
    setState({ notice: "连接已断开" });
    return false;
  }
  setState({ notice: "", approvalNote: "" });
  socket.send({ type: "user_message", text: content });
  return true;
}

export function requestStop() {
  socket?.send({ type: "stop" });
}

export function requestCompress() {
  socket?.send({ type: "compress" });
}

export function sendDecision(decision: "approve" | "approve_always" | "reject") {
  socket?.send({ type: "approval.response", decision });
}

export function useAgent() {
  return useSyncExternalStore(subscribe, () => state, () => state);
}
