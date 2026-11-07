import asyncio
from pathlib import Path

import pytest

from app.agent.context import (
    HistoryMessage,
    build_model_messages,
    clip_text,
    compression_input,
    messages_to_fold,
    over_auto_threshold,
    payload_chars,
)
from app.agent.executor import is_context_length_error
from app.agent.stream import ToolCallAssembler
from app.permissions.gate import ApprovalGate
from app.permissions.policy import path_allowed, shell_allowed, shell_prefix
from app.tools.files import PathRejected, resolve_inside


def message(id_: str, role: str, content: str, **kwargs) -> HistoryMessage:
    return HistoryMessage(id=id_, role=role, content=content, **kwargs)


def test_shell_prefix_matches_plan():
    assert shell_prefix("echo hello") == "echo hello"
    assert shell_prefix("  ls  ") == "ls"
    assert shell_prefix("   ") is None
    prefixes = ["echo hello"]
    assert shell_allowed("echo hello world", prefixes)
    assert shell_allowed("echo hello && rm -rf /", prefixes)
    assert not shell_allowed("echo helloworld", prefixes)
    assert shell_allowed("ls -la", ["ls"])
    assert not shell_allowed("ls; rm -rf /", ["ls"])
    assert path_allowed("src/main.py.bak", ["src/main.py"])
    assert not path_allowed("src/other.py", ["src/main.py"])


def test_path_jail(tmp_path: Path):
    root = tmp_path / "session"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("no", encoding="utf-8")
    (root / "link").symlink_to(outside)

    assert resolve_inside(root, "note.txt") == (root / "note.txt").resolve()
    assert resolve_inside(root, str(root / "note.txt")) == (root / "note.txt").resolve()
    with pytest.raises(PathRejected):
        resolve_inside(root, "../outside/secret.txt")
    with pytest.raises(PathRejected):
        resolve_inside(root, str(outside / "secret.txt"))
    with pytest.raises(PathRejected):
        resolve_inside(root, str(root / ".." / "outside" / "secret.txt"))
    with pytest.raises(PathRejected):
        resolve_inside(root, "link/secret.txt")


def test_context_window_and_compaction():
    history = [
        message("1", "user", "第一轮" + "甲" * 400),
        message("2", "assistant", "好"),
        message("3", "user", "第二轮"),
        message("4", "user", "第三轮"),
        message("5", "user", "第四轮"),
    ]
    full = build_model_messages(
        history,
        context_limit=100_000,
        compacted_before_id=None,
        system="系统",
        summary="",
        tools=[],
    )
    packed = build_model_messages(
        history,
        context_limit=payload_chars(full[-2:], None),
        compacted_before_id=None,
        system="系统",
        summary="",
        tools=[],
    )
    packed_text = " ".join(item["content"] for item in packed)
    assert "第四轮" in packed_text
    assert "第一轮" not in packed_text

    folded = messages_to_fold(
        history,
        compacted_before_id=None,
        context_limit=payload_chars(full[-2:], None),
        system="系统",
        tools=[],
    )
    assert folded is not None
    assert folded[-1].id != "5"
    assert "第一轮" in folded[0].content

    payload = build_model_messages(
        history,
        context_limit=100_000,
        compacted_before_id="3",
        system="系统",
        summary="已经做过第一轮和第二轮",
    )
    contents = [item["content"] for item in payload]
    assert contents[0] == "系统"
    assert "已经做过第一轮和第二轮" in contents
    assert "第一轮" not in contents
    assert "第三轮" in contents
    assert "第四轮" in contents
    assert over_auto_threshold(900, 1000)
    assert not over_auto_threshold(899, 1000)


def test_thinking_is_omitted_and_tool_text_is_clipped():
    history = [
        message("1", "user", "看一下"),
        message("2", "assistant", "我来读", tool_calls=[{"id": "c1"}], thinking="先想一下"),
        message("3", "tool", "x" * 20, tool_call_id="c1"),
    ]
    payload = build_model_messages(
        history,
        context_limit=100_000,
        compacted_before_id=None,
        system="系统",
        summary="",
    )
    assistant = payload[2]
    assert "thinking" not in assistant
    assert assistant["tool_calls"] == [{"id": "c1"}]
    assert clip_text("abcdefghij", 6) == "abc\n...[已截断]...\nhij"


def test_compression_input_keeps_tool_calls():
    huge = "x" * 20000
    text = compression_input(
        [
            message(
                "1",
                "assistant",
                "",
                tool_calls=[
                    {
                        "function": {
                            "name": "write",
                            "arguments": '{"path": "a.py", "content": "' + huge + '"}',
                        }
                    }
                ],
            ),
            message("2", "tool", "已写入 a.py"),
        ]
    )
    assert "[工具 write]" in text
    assert "a.py" in text
    assert "已写入 a.py" in text
    assert "已截断" in text
    assert len(text) < 12000
    with_summary = compression_input(
        [message("3", "user", "后来的记录")],
        previous_summary="旧纪要",
    )
    assert with_summary.startswith("已有总结:\n旧纪要")
    assert "后来的记录" in with_summary


def test_context_length_error_detects_provider_text():
    assert is_context_length_error("接口返回 400：context_length_exceeded")
    assert is_context_length_error("This model's maximum context length is 8192 tokens")
    assert is_context_length_error("上下文长度超出限制")
    assert not is_context_length_error("接口返回 401：invalid api key")
    assert not is_context_length_error("模型请求超时")


def test_stream_assembler_waits_for_json():
    assembler = ToolCallAssembler()
    assembler.add(
        [
            {"index": 0, "id": "call-1", "function": {"name": "write", "arguments": '{"path":'}},
            {"index": 0, "function": {"arguments": ' "a.py"}'}},
        ]
    )
    done = assembler.finish()
    assert done[0].error == ""
    assert done[0].arguments == {"path": "a.py"}

    broken = ToolCallAssembler()
    broken.add([{"index": 0, "id": "call-2", "function": {"name": "write", "arguments": "{"}}])
    assert broken.finish()[0].error == "参数无法解析"
    assert broken.finish()[0].arguments is None


async def test_approval_timeout():
    gate = ApprovalGate()
    decision = await gate.wait({"id": "a1"}, timeout=0.01)
    assert decision == "timeout"
    assert gate.request is None
    assert gate.resolve("approve") is False


async def test_approval_resolve():
    gate = ApprovalGate()

    async def waiter():
        return await gate.wait({"id": "a2"}, timeout=1)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    assert gate.resolve("reject") is True
    assert await task == "reject"
