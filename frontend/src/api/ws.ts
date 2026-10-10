export type ServerEvent = {
  type: string;
  message?: string;
  messages?: import("./rest").ChatMessage[];
  state?: string;
  title?: string;
  summary?: string;
  compacted_before_id?: string | null;
  session_id?: string;
  approval?: ApprovalRequest | null;
  id?: string;
  role?: string;
  content?: string;
  thinking?: string | null;
  tool_calls?: unknown;
  created_at?: string;
  channel?: string;
  text?: string;
  tool_call_id?: string;
  name?: string;
  tool?: string;
  arguments?: unknown;
  is_error?: boolean;
  decision?: string;
  prefix?: string;
  diff?: { before: string; after: string } | null;
};

export type ApprovalRequest = {
  id?: string;
  tool?: string;
  arguments?: unknown;
  prefix?: string;
};

export type SessionSocket = {
  send: (payload: object) => void;
  close: () => void;
};

export function openSessionSocket(
  sessionId: string,
  onEvent: (event: ServerEvent) => void,
  onStatus: (status: "connecting" | "open" | "closed" | "error") => void,
): SessionSocket {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(
    `${protocol}://${window.location.host}/ws?session_id=${encodeURIComponent(sessionId)}`,
  );
  onStatus("connecting");
  socket.onopen = () => onStatus("open");
  socket.onerror = () => onStatus("error");
  socket.onclose = () => onStatus("closed");
  socket.onmessage = (event) => {
    try {
      onEvent(JSON.parse(String(event.data)) as ServerEvent);
    } catch {
      onEvent({ type: "error", message: "消息无法解析" });
    }
  };
  return {
    send(payload: object) {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(payload));
      }
    },
    close() {
      socket.onclose = null;
      socket.close();
    },
  };
}
