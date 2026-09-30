export type Session = {
  id: string;
  title: string;
  summary: string;
  compacted_before_id: string | null;
  created_at: string;
  updated_at: string;
};

export type ChatMessage = {
  id: string;
  role: string;
  content: string;
  tool_calls: unknown;
  tool_call_id: string | null;
  thinking: string | null;
  created_at: string;
  decision?: string;
  is_error?: boolean;
  tool_name?: string | null;
  arguments?: unknown;
  prefix?: string;
  diff?: { before: string; after: string } | null;
};

export type LlmSettings = {
  base_url: string;
  model: string;
  api_key_set: boolean;
  context_turns: number;
};

export type AllowRule = {
  id: number;
  kind: string;
  pattern: string;
  created_at: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) {
        detail = body.detail;
      }
    } catch {
      // 响应没有 JSON 时保留状态说明。
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export function listSessions() {
  return request<Session[]>("/api/sessions");
}

export function createSession() {
  return request<Session>("/api/sessions", { method: "POST" });
}

export function deleteSession(id: string) {
  return request<{ ok: boolean }>(`/api/sessions/${id}`, { method: "DELETE" });
}

export function listMessages(id: string) {
  return request<ChatMessage[]>(`/api/sessions/${id}/messages`);
}

export function readLlm() {
  return request<LlmSettings>("/api/settings/llm");
}

export function saveLlm(body: {
  base_url: string;
  model: string;
  api_key: string;
  context_turns: number;
}) {
  return request<LlmSettings>("/api/settings/llm", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function probeLlm(body: { base_url: string; model: string; api_key: string }) {
  return request<{ ok: boolean; error?: string }>("/api/settings/llm/probe", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function listRules() {
  return request<AllowRule[]>("/api/settings/allow-rules");
}

export function createRule(kind: string, pattern: string) {
  return request<AllowRule>("/api/settings/allow-rules", {
    method: "POST",
    body: JSON.stringify({ kind, pattern }),
  });
}

export function deleteRule(id: number) {
  return request<{ ok: boolean }>(`/api/settings/allow-rules/${id}`, { method: "DELETE" });
}
