"""把流式工具调用的碎片拼成完整参数。"""

import json
from dataclasses import dataclass, field


@dataclass
class ParsedToolCall:
    index: int
    call_id: str
    name: str
    arguments: dict | None
    error: str = ""
    raw: str = ""


@dataclass
class ToolCallAssembler:
    parts: dict[int, dict[str, str]] = field(default_factory=dict)

    def add(self, deltas: list[dict] | None) -> None:
        for delta in deltas or []:
            index = int(delta.get("index", 0))
            slot = self.parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if delta.get("id"):
                slot["id"] = str(delta["id"])
            function = delta.get("function") or {}
            if function.get("name"):
                slot["name"] = str(function["name"])
            if function.get("arguments"):
                slot["arguments"] += str(function["arguments"])

    def finish(self) -> list[ParsedToolCall]:
        parsed: list[ParsedToolCall] = []
        for index in sorted(self.parts):
            slot = self.parts[index]
            raw = slot["arguments"]
            try:
                arguments = json.loads(raw or "{}")
            except json.JSONDecodeError:
                parsed.append(
                    ParsedToolCall(index, slot["id"], slot["name"], None, "参数无法解析", raw)
                )
                continue
            if not isinstance(arguments, dict):
                parsed.append(
                    ParsedToolCall(index, slot["id"], slot["name"], None, "参数无法解析", raw)
                )
                continue
            parsed.append(ParsedToolCall(index, slot["id"], slot["name"], arguments, "", raw or "{}"))
        return parsed
