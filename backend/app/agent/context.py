"""按轮裁剪送给模型的历史，并算出压缩时留下的结尾。"""

from dataclasses import dataclass, field


TOOL_RESULT_LIMIT = 8000


@dataclass
class HistoryMessage:
    id: str
    role: str
    content: str = ""
    tool_calls: list | None = None
    tool_call_id: str | None = None
    thinking: str | None = None
    extra: dict = field(default_factory=dict)


def clip_text(text: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n...[已截断]...\n{text[-tail:]}"


def split_turns(messages: list[HistoryMessage]) -> list[list[HistoryMessage]]:
    turns: list[list[HistoryMessage]] = []
    current: list[HistoryMessage] = []
    for message in messages:
        if message.role == "user" and current:
            turns.append(current)
            current = [message]
        else:
            current.append(message)
    if current:
        turns.append(current)
    return turns


def after_compaction(
    messages: list[HistoryMessage], compacted_before_id: str | None
) -> list[HistoryMessage]:
    if not compacted_before_id:
        return list(messages)
    for index, message in enumerate(messages):
        if message.id == compacted_before_id:
            return list(messages[index + 1 :])
    return list(messages)


def recent_messages(messages: list[HistoryMessage], context_turns: int) -> list[HistoryMessage]:
    if context_turns < 1:
        return []
    kept = split_turns(messages)[-context_turns:]
    return [message for turn in kept for message in turn]


def summary_messages(
    messages: list[HistoryMessage], context_turns: int
) -> list[HistoryMessage] | None:
    turns = split_turns(messages)
    if len(turns) <= context_turns:
        return None
    older = turns[:-context_turns]
    return [message for turn in older for message in turn]


def _render_tool_calls(tool_calls: list) -> str:
    lines: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            function = {}
        name = function.get("name") or "unknown"
        arguments = function.get("arguments") or ""
        if not isinstance(arguments, str):
            arguments = str(arguments)
        lines.append(f"[工具 {name}] {clip_text(arguments)}")
    return "\n".join(lines)


def compression_input(messages: list[HistoryMessage]) -> str:
    lines: list[str] = []
    for message in messages:
        parts: list[str] = []
        body = message.content or ""
        if message.role == "tool":
            body = clip_text(body)
        if body:
            parts.append(body)
        if message.tool_calls:
            rendered = _render_tool_calls(message.tool_calls)
            if rendered:
                parts.append(rendered)
        lines.append(f"{message.role}: " + "\n".join(parts))
    return "\n".join(lines)


def to_api_message(message: HistoryMessage) -> dict:
    if message.role == "assistant":
        payload: dict = {"role": "assistant", "content": message.content if message.content else None}
        if message.tool_calls:
            payload["tool_calls"] = message.tool_calls
        elif payload["content"] is None:
            payload["content"] = ""
        return payload
    if message.role == "tool":
        return {
            "role": "tool",
            "content": clip_text(message.content),
            "tool_call_id": message.tool_call_id or "",
        }
    return {"role": message.role, "content": message.content}


def build_model_messages(
    messages: list[HistoryMessage],
    *,
    context_turns: int,
    compacted_before_id: str | None,
    system: str,
    summary: str,
    pending: list[HistoryMessage] | None = None,
) -> list[dict]:
    selected = recent_messages(after_compaction(messages, compacted_before_id), context_turns)
    payload = [{"role": "system", "content": system}]
    if compacted_before_id and summary.strip():
        payload.append({"role": "system", "content": summary})
    payload.extend(to_api_message(message) for message in selected)
    payload.extend(to_api_message(message) for message in pending or [])
    return payload
