"""按字符上限装入要发送的上下文，并选出压缩时移出的轮次。"""

import json
from dataclasses import dataclass, field


TOOL_RESULT_LIMIT = 8000
DEFAULT_CONTEXT_LIMIT = 100_000
MIN_CONTEXT_LIMIT = 20_000
MAX_CONTEXT_LIMIT = 2_000_000
AUTO_COMPACT_PERCENT = 90


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


def payload_chars(messages: list[dict], tools: list | None) -> int:
    encoded = json.dumps(messages, ensure_ascii=False)
    if tools is not None:
        encoded += json.dumps(tools, ensure_ascii=False)
    return len(encoded)


def _flat(turns: list[list[HistoryMessage]]) -> list[HistoryMessage]:
    return [message for turn in turns for message in turn]


def assemble_messages(
    selected: list[HistoryMessage],
    *,
    system: str,
    summary: str,
    pending: list[HistoryMessage] | None = None,
) -> list[dict]:
    payload = [{"role": "system", "content": system}]
    if summary.strip():
        payload.append({"role": "system", "content": summary})
    payload.extend(to_api_message(message) for message in selected)
    payload.extend(to_api_message(message) for message in pending or [])
    return payload


def measure_messages(
    selected: list[HistoryMessage],
    *,
    system: str,
    summary: str,
    tools: list | None,
    pending: list[HistoryMessage] | None = None,
) -> int:
    return payload_chars(
        assemble_messages(selected, system=system, summary=summary, pending=pending),
        tools,
    )


def context_occupancy(
    messages: list[HistoryMessage],
    *,
    compacted_before_id: str | None,
    system: str,
    summary: str,
    tools: list | None,
) -> int:
    visible_summary = summary if compacted_before_id and summary.strip() else ""
    post = after_compaction(messages, compacted_before_id)
    return measure_messages(post, system=system, summary=visible_summary, tools=tools)


def over_auto_threshold(occupancy: int, context_limit: int) -> bool:
    return occupancy * 100 >= context_limit * AUTO_COMPACT_PERCENT


def pack_turns(
    turns: list[list[HistoryMessage]],
    *,
    context_limit: int,
    system: str,
    summary: str,
    tools: list | None,
) -> list[HistoryMessage]:
    chosen: list[list[HistoryMessage]] = []
    for turn in reversed(turns):
        candidate = [turn, *chosen]
        size = measure_messages(_flat(candidate), system=system, summary=summary, tools=tools)
        if size <= context_limit or not chosen:
            chosen = candidate
            if size > context_limit:
                break
        else:
            break
    return _flat(chosen)


def messages_to_fold(
    messages: list[HistoryMessage],
    *,
    compacted_before_id: str | None,
    context_limit: int,
    system: str,
    tools: list | None,
) -> list[HistoryMessage] | None:
    post = after_compaction(messages, compacted_before_id)
    turns = split_turns(post)
    if len(turns) < 2:
        return None
    target = max(1, context_limit // 2)
    kept = [turns[-1]]
    for turn in reversed(turns[:-1]):
        candidate = [turn, *kept]
        size = measure_messages(_flat(candidate), system=system, summary="", tools=tools)
        if size <= target:
            kept = candidate
        else:
            break
    kept_ids = {message.id for message in _flat(kept)}
    dropped = [message for message in post if message.id not in kept_ids]
    if not dropped:
        return None
    return dropped


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


def compression_input(messages: list[HistoryMessage], previous_summary: str = "") -> str:
    lines: list[str] = []
    if previous_summary.strip():
        lines.append("已有总结:\n" + previous_summary.strip())
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
    return "\n\n".join(lines) if previous_summary.strip() else "\n".join(lines)


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
    context_limit: int,
    compacted_before_id: str | None,
    system: str,
    summary: str,
    tools: list | None = None,
    pending: list[HistoryMessage] | None = None,
) -> list[dict]:
    visible_summary = summary if compacted_before_id and summary.strip() else ""
    selected = pack_turns(
        split_turns(after_compaction(messages, compacted_before_id)),
        context_limit=context_limit,
        system=system,
        summary=visible_summary,
        tools=tools,
    )
    return assemble_messages(selected, system=system, summary=visible_summary, pending=pending)
