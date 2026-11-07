import asyncio
import json

import httpx

from app.agent.context import messages_to_fold
from app.agent.executor import set_transport
from app.agent.prompts import system_prompt
from app.agent.loop import get_loop, history_from, shutdown_loop
from app.config import get_settings
from app.persist import add_message, add_rule, get_session, list_messages, list_rules, set_summary
from app.tools.files import session_directory


def sse(deltas: list[dict]) -> httpx.Response:
    lines = ["data: " + json.dumps({"choices": [{"index": 0, "delta": delta}]}, ensure_ascii=False) for delta in deltas]
    lines.append("data: [DONE]")
    return httpx.Response(200, text="\n".join(lines) + "\n")


class Script:
    def __init__(self, rounds: list[list[dict]]):
        self.rounds = list(rounds)
        self.bodies: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content.decode()))
        return sse(self.rounds.pop(0))

    def install(self) -> None:
        set_transport(httpx.MockTransport(self.handler))


async def configure(client, limit: int = 100_000) -> None:
    saved = await client.put(
        "/api/settings/llm",
        json={
            "base_url": "http://example.test/v1",
            "model": "demo",
            "api_key": "test-key",
            "context_limit": limit,
        },
    )
    assert saved.status_code == 200


async def open_session(client):
    created = await client.post("/api/sessions")
    session_id = created.json()["id"]
    loop = get_loop(session_id)
    sent: list[dict] = []

    class Socket:
        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    loop.sockets.add(Socket())
    return session_id, loop, sent


def tool_call(call_id: str, name: str, arguments: dict, index: int = 0) -> dict:
    return {
        "tool_calls": [
            {
                "index": index,
                "id": call_id,
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ]
    }


async def test_tools_run_in_order_after_failure_and_rule_hit(client):
    await configure(client)
    session_id, loop, sent = await open_session(client)
    await add_rule("shell_prefix", "echo hello")
    script = Script(
        [
            [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": "missing.txt"}),
                            },
                        },
                        {
                            "index": 1,
                            "id": "c2",
                            "function": {
                                "name": "shell",
                                "arguments": json.dumps({"command": "echo hello && echo allowed"}),
                            },
                        },
                    ]
                }
            ],
            [{"content": "做完了"}],
        ]
    )
    script.install()
    await loop.on_user_message("处理文件")
    await loop.queue.join()

    assert len(script.bodies) == 2
    tools = [item for item in sent if item["type"] == "tool.result"]
    assert [item["name"] for item in tools] == ["read", "shell"]
    assert tools[0]["is_error"] is True
    assert "文件不存在" in tools[0]["text"]
    assert "allowed" in tools[1]["text"]
    assert tools[1]["decision"] == "以后都允许"
    follow = json.dumps(script.bodies[1]["messages"], ensure_ascii=False)
    assert "文件不存在" in follow
    assert "allowed" in follow
    assert loop.state == "done"
    await shutdown_loop(session_id)


async def test_rejection_still_runs_later_tool_and_requests_again(client):
    await configure(client)
    session_id, loop, _sent = await open_session(client)
    root = session_directory(get_settings().workdir_path(), session_id)
    root.mkdir()
    (root / "hello.txt").write_text("内容", encoding="utf-8")
    script = Script(
        [
            [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "w1",
                            "function": {
                                "name": "write",
                                "arguments": json.dumps({"path": "secret.txt", "content": "no"}),
                            },
                        },
                        {
                            "index": 1,
                            "id": "r1",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": "hello.txt"}),
                            },
                        },
                    ]
                }
            ],
            [{"content": "好"}],
        ]
    )
    script.install()

    async def reject() -> None:
        for _ in range(300):
            if loop.gate.request is not None:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(loop.state)
        hello = await loop.hello()
        assert hello is not None
        assert hello["approval"]["tool"] == "write"
        assert hello["approval"]["prefix"] == "secret.txt"
        assert hello["state"] == "awaiting_approval"
        await loop.on_approval("reject")

    waiter = asyncio.create_task(reject())
    await loop.on_user_message("改文件")
    await loop.queue.join()
    await waiter
    assert not (root / "secret.txt").exists()
    rows = await list_messages(session_id)
    texts = [row["content"] for row in rows]
    assert any("已拒绝" in text for text in texts)
    assert "内容" in texts
    assert len(script.bodies) == 2
    await shutdown_loop(session_id)


async def test_approval_timeout_writes_no_rule(client):
    await configure(client)
    session_id, loop, sent = await open_session(client)
    loop.approval_timeout = 0.05
    script = Script(
        [
            [tool_call("w1", "write", {"path": "note.txt", "content": "x"})],
            [{"content": "继续"}],
        ]
    )
    script.install()
    await loop.on_user_message("写一下")
    await loop.queue.join()
    assert await list_rules() == []
    root = session_directory(get_settings().workdir_path(), session_id)
    assert not (root / "note.txt").exists()
    assert any(item.get("decision") == "timeout" for item in sent if item["type"] == "approval.resolved")
    assert any("两分钟未选择" in item.get("text", "") for item in sent if item["type"] == "tool.result")
    assert len(script.bodies) == 2
    await shutdown_loop(session_id)


async def test_write_rule_does_not_allow_edit(client):
    await configure(client)
    session_id, loop, _sent = await open_session(client)
    await add_rule("write_path", "note.txt")
    root = session_directory(get_settings().workdir_path(), session_id)
    root.mkdir()
    (root / "note.txt").write_text("old", encoding="utf-8")
    loop.approval_timeout = 0.05
    script = Script(
        [
            [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "w1",
                            "function": {
                                "name": "write",
                                "arguments": json.dumps({"path": "note.txt", "content": "new"}),
                            },
                        },
                        {
                            "index": 1,
                            "id": "e1",
                            "function": {
                                "name": "edit",
                                "arguments": json.dumps(
                                    {"path": "note.txt", "old_string": "new", "new_string": "edited"}
                                ),
                            },
                        },
                    ]
                }
            ],
            [{"content": "好"}],
        ]
    )
    script.install()
    await loop.on_user_message("改一下")
    await loop.queue.join()
    assert (root / "note.txt").read_text(encoding="utf-8") == "new"
    rows = await list_messages(session_id)
    edit = [row for row in rows if row.get("tool_name") == "edit"]
    assert edit and edit[0]["decision"] == "超时"
    await shutdown_loop(session_id)


async def test_thinking_is_stored_but_not_sent(client):
    await configure(client)
    session_id, loop, _sent = await open_session(client)
    await add_message(session_id, "user", "第一轮")
    await add_message(session_id, "user", "第四轮")
    await add_message(session_id, "assistant", "可见回复", thinking="隐藏的思考")
    script = Script([[{"reasoning_content": "本轮思考"}, {"content": "好"}]])
    script.install()
    await loop.on_user_message("第五轮")
    await loop.queue.join()
    body = json.dumps(script.bodies[0]["messages"], ensure_ascii=False)
    assert "第一轮" in body
    assert "第四轮" in body
    assert "第五轮" in body
    assert "可见回复" in body
    assert "隐藏的思考" not in body
    assert "本轮思考" not in body
    stored = await list_messages(session_id)
    assert any(row["thinking"] == "本轮思考" for row in stored)
    await shutdown_loop(session_id)


async def test_compress_folds_older_turns_and_failure_is_stable(client):
    session_id, loop, sent = await open_session(client)
    covered = await add_message(session_id, "user", "更早的原文")
    await set_summary(session_id, "旧总结", covered["id"])
    await add_message(
        session_id,
        "user",
        "第一轮目标" + "甲" * 30000,
    )
    await add_message(
        session_id,
        "assistant",
        "已经决定",
        thinking="隐藏的思考",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "write", "arguments": '{"path": "a.py"}'},
            }
        ],
    )
    await add_message(session_id, "user", "第四轮保留")
    history = history_from(await list_messages(session_id))
    root = str(await loop._root())
    system = system_prompt(root, True)
    dropped = messages_to_fold(
        history,
        compacted_before_id=covered["id"],
        context_limit=40_000,
        system=system,
        tools=[],
    )
    assert dropped is not None
    assert any("第一轮目标" in item.content for item in dropped)
    assert all(item.content != "第四轮保留" for item in dropped)
    await configure(client, limit=40_000)
    script = Script([[{"content": "纪要正文"}]])
    script.install()
    await loop.on_compress()
    assert loop.compress_task is not None
    await loop.compress_task
    user_input = script.bodies[0]["messages"][1]["content"]
    assert "tools" not in script.bodies[0]
    assert "已有总结" in user_input
    assert "旧总结" in user_input
    assert "第一轮目标" in user_input
    assert "write" in user_input
    assert "a.py" in user_input
    assert "更早的原文" not in user_input
    assert "第四轮保留" not in user_input
    assert "隐藏的思考" not in json.dumps(script.bodies[0], ensure_ascii=False)
    session = await get_session(session_id)
    assert session is not None
    assert session["summary"] == "纪要正文"
    assert session["compacted_before_id"] == dropped[-1].id
    payload = json.dumps(await loop.model_messages(), ensure_ascii=False)
    assert "纪要正文" in payload
    assert "第四轮保留" in payload
    assert "第一轮目标" not in payload
    assert "更早的原文" not in payload

    follow = Script([[{"content": "继续好"}]])
    follow.install()
    await loop.on_user_message("继续")
    await loop.queue.join()
    follow_text = json.dumps(follow.bodies[0]["messages"], ensure_ascii=False)
    assert "纪要正文" in follow_text
    assert "第一轮目标" not in follow_text
    assert "更早的原文" not in follow_text

    await add_message(session_id, "user", "再压一轮" + "丙" * 30000)
    await set_summary(session_id, "旧总结", dropped[-1].id)
    set_transport(httpx.MockTransport(lambda _request: httpx.Response(500, text="boom")))
    await loop.on_compress()
    assert loop.compress_task is not None
    await loop.compress_task
    kept = await get_session(session_id)
    assert kept is not None
    assert kept["summary"] == "旧总结"
    assert kept["compacted_before_id"] == dropped[-1].id
    assert any(item.get("message", "").startswith("接口返回 500") for item in sent if item["type"] == "error")
    await shutdown_loop(session_id)


async def test_finish_task_runs_the_rest_of_the_batch(client):
    await configure(client)
    session_id, loop, sent = await open_session(client)
    root = session_directory(get_settings().workdir_path(), session_id)
    root.mkdir()
    (root / "hello.txt").write_text("内容", encoding="utf-8")
    script = Script(
        [
            [
                tool_call("f1", "finish_task", {"message": "收工"}, 0),
                tool_call("r1", "read", {"path": "hello.txt"}, 1),
            ],
            [{"content": "不应再请求"}],
        ]
    )
    script.install()
    await loop.on_user_message("结束")
    await loop.queue.join()
    assert len(script.bodies) == 1
    tools = [item for item in sent if item["type"] == "tool.result"]
    assert [item["name"] for item in tools] == ["finish_task", "read"]
    assert "内容" in tools[1]["text"]
    rows = await list_messages(session_id)
    by_call = {row["tool_call_id"]: row["id"] for row in rows if row.get("tool_call_id")}
    assert tools[1]["id"] == by_call["r1"]
    assert any(row["role"] == "assistant" and "收工" in row["content"] for row in rows)
    assert loop.state == "done"
    await shutdown_loop(session_id)


async def test_broken_arguments_are_stored_as_object(client):
    await configure(client)
    session_id, loop, sent = await open_session(client)
    script = Script(
        [
            [
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "bad",
                            "function": {"name": "read", "arguments": "{"},
                        }
                    ]
                }
            ],
            [{"content": "继续"}],
        ]
    )
    script.install()
    await loop.on_user_message("读一下")
    await loop.queue.join()
    assert len(script.bodies) == 2
    assistant = next(item for item in script.bodies[1]["messages"] if item.get("tool_calls"))
    assert assistant["tool_calls"][0]["function"]["arguments"] == "{}"
    assert any("参数无法解析" in item.get("text", "") for item in sent if item["type"] == "tool.result")
    await shutdown_loop(session_id)


async def test_compress_without_older_messages_does_not_call_model(client):
    await configure(client)
    session_id, loop, sent = await open_session(client)
    await add_message(session_id, "user", "只有一轮")

    def deny(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"不应调用模型 {request.url}")

    set_transport(httpx.MockTransport(deny))
    await loop.on_compress()
    assert loop.compress_task is not None
    await loop.compress_task
    assert any(item.get("message") == "没有更早的消息可压缩" for item in sent)
    await shutdown_loop(session_id)


async def test_auto_compact_before_request_then_continues(client):
    session_id, loop, sent = await open_session(client)
    await add_message(session_id, "user", "旧轮" + "甲" * 30000)
    usage = await loop._context_usage()
    assert usage is not None
    await configure(client, limit=max(20_000, int(usage["chars"] * 100 / 95)))
    script = Script([[{"content": "纪要"}], [{"content": "好"}]])
    script.install()
    await loop.on_user_message("新问题")
    await loop.queue.join()
    assert "tools" not in script.bodies[0]
    assert "tools" in script.bodies[1]
    assert "旧轮" in script.bodies[0]["messages"][1]["content"]
    assert "新问题" not in script.bodies[0]["messages"][1]["content"]
    follow = json.dumps(script.bodies[1]["messages"], ensure_ascii=False)
    assert "纪要" in follow
    assert "新问题" in follow
    assert "旧轮" not in follow
    assert any(item.get("type") == "compress.start" and item.get("auto") is True for item in sent)
    assert loop.state == "done"
    await shutdown_loop(session_id)


async def test_auto_compact_stop_leaves_summary_and_skips_the_model(client):
    session_id, loop, sent = await open_session(client)
    await add_message(session_id, "user", "旧轮" + "甲" * 30000)
    usage = await loop._context_usage()
    assert usage is not None
    await configure(client, limit=max(20_000, int(usage["chars"] * 100 / 95)))
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        loop.stop_flag = True
        loop.stop_event.set()
        return sse([{"content": "不应保存"}])

    set_transport(httpx.MockTransport(handler))
    await loop.on_user_message("新问题")
    await loop.queue.join()
    assert len(bodies) == 1
    assert "tools" not in bodies[0]
    session = await get_session(session_id)
    assert session is not None
    assert session["summary"] == ""
    assert session["compacted_before_id"] is None
    assert any(item.get("message") == "已取消压缩" for item in sent)
    assert loop.state == "idle"
    await shutdown_loop(session_id)


async def test_context_length_error_compacts_once_and_retries(client):
    session_id, loop, sent = await open_session(client)
    await add_message(session_id, "user", "旧轮" + "乙" * 60000)
    usage = await loop._context_usage()
    assert usage is not None
    await configure(client, limit=max(20_000, int(usage["chars"] / 0.8) + 1))
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if "tools" in body and len(bodies) == 1:
            return httpx.Response(400, text="context_length_exceeded")
        if "tools" not in body:
            return sse([{"content": "挤出来的纪要"}])
        return sse([{"content": "重试成功"}])

    set_transport(httpx.MockTransport(handler))
    await loop.on_user_message("新问题")
    await loop.queue.join()
    assert len(bodies) == 3
    assert "tools" in bodies[0]
    assert "tools" not in bodies[1]
    assert "旧轮" in bodies[1]["messages"][1]["content"]
    assert "tools" in bodies[2]
    assert "挤出来的纪要" in json.dumps(bodies[2]["messages"], ensure_ascii=False)
    stored = await list_messages(session_id)
    assert any(row["content"] == "重试成功" for row in stored)
    assert not any(item.get("message") == "上下文仍然过长，已停止" for item in sent)
    await shutdown_loop(session_id)


async def test_context_length_error_stops_when_retry_is_still_too_long(client):
    session_id, loop, sent = await open_session(client)
    await add_message(session_id, "user", "旧轮" + "乙" * 60000)
    usage = await loop._context_usage()
    assert usage is not None
    await configure(client, limit=max(20_000, int(usage["chars"] / 0.8) + 1))
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if "tools" not in body:
            return sse([{"content": "纪要"}])
        return httpx.Response(400, text="maximum context length is 8192")

    set_transport(httpx.MockTransport(handler))
    await loop.on_user_message("新问题")
    await loop.queue.join()
    assert len(bodies) == 3
    assert any(item.get("message") == "上下文仍然过长，已停止" for item in sent)
    assert loop.state == "error"
    await shutdown_loop(session_id)


async def test_auto_compact_pauses_when_occupancy_does_not_drop(client):
    session_id, loop, sent = await open_session(client)
    await add_message(session_id, "user", "旧轮" + "甲" * 30000)
    usage = await loop._context_usage()
    assert usage is not None
    await configure(client, limit=max(20_000, int(usage["chars"] * 100 / 95)))
    script = Script([[{"content": "巨" * 80000}], [{"content": "继续好"}]])
    script.install()
    await loop.on_user_message("新问题")
    await loop.queue.join()
    assert len(script.bodies) == 2
    assert "tools" not in script.bodies[0]
    assert "tools" in script.bodies[1]
    assert any(item.get("message") == "压缩后占用没有下降，已停止自动压缩" for item in sent)
    assert loop.state == "done"
    await shutdown_loop(session_id)


async def test_current_turn_over_limit_is_sent_without_compacting(client):
    await configure(client, limit=30_000)
    session_id, loop, sent = await open_session(client)
    script = Script([[{"content": "照样回答"}]])
    script.install()
    await loop.on_user_message("大" * 40_000)
    await loop.queue.join()
    assert len(script.bodies) == 1
    assert "tools" in script.bodies[0]
    assert any(
        item.get("message") == "当前这一轮已经超过上下文上限的 90%，无法继续压缩" for item in sent
    )
    assert loop.state == "done"
    await shutdown_loop(session_id)


async def test_auto_compact_after_tool_can_be_stopped(client):
    session_id, loop, sent = await open_session(client)
    root = session_directory(get_settings().workdir_path(), session_id)
    root.mkdir()
    (root / "a.txt").write_text("文件甲" + "甲" * 8000, encoding="utf-8")
    (root / "b.txt").write_text("文件乙", encoding="utf-8")
    await add_message(session_id, "user", "垫高" + "丙" * 24000)
    usage = await loop._context_usage()
    assert usage is not None
    threshold = usage["chars"] + 1500
    limit = max(20_000, int(threshold * 100 / 90) + 1)
    await configure(client, limit=limit)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if "tools" not in body:
            loop.stop_flag = True
            loop.stop_event.set()
            return sse([{"content": "不要这条纪要"}])
        return sse(
            [
                tool_call("r1", "read", {"path": "a.txt"}, 0),
                tool_call("r2", "read", {"path": "b.txt"}, 1),
            ]
        )

    set_transport(httpx.MockTransport(handler))
    await loop.on_user_message("读两个文件")
    await loop.queue.join()
    assert bodies and "tools" in bodies[0]
    assert any("tools" not in body for body in bodies[1:])
    tools = [item for item in sent if item["type"] == "tool.result"]
    assert tools[0]["name"] == "read"
    assert "文件甲" in tools[0]["text"]
    assert all("文件乙" not in item.get("text", "") for item in tools)
    session = await get_session(session_id)
    assert session is not None
    assert session["summary"] == ""
    assert any(item.get("message") == "已取消压缩" for item in sent)
    await shutdown_loop(session_id)
