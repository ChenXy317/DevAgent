"""每个会话一个循环。按顺序请求模型、执行工具，再把结果送回去。"""

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import WebSocket

from app.agent.context import (
    HistoryMessage,
    build_model_messages,
    clip_text,
    compression_input,
    context_occupancy,
    messages_to_fold,
    over_auto_threshold,
)
from app.agent.executor import ModelError, RunStopped, is_context_length_error, stream_chat
from app.agent.prompts import compression_prompt, system_prompt
from app.agent.stream import ParsedToolCall, ToolCallAssembler
from app.config import get_settings
from app.permissions.gate import ApprovalGate
from app.permissions.policy import matched_pattern, shell_prefix
from app.persist import (
    DuplicateRule,
    NotFound,
    add_message,
    add_rule,
    add_tool_log,
    get_llm,
    get_session,
    list_messages,
    list_rules,
    new_id,
    set_summary,
    update_message,
)
from app.tools.files import (
    edit_text,
    glob_files,
    grep_text,
    read_text,
    session_directory,
    write_text,
)
from app.tools.registry import TOOLS
from app.tools.search import search_web
from app.tools.shell import SHELL_TIMEOUT_SECONDS, kill_group, run_shell

log = logging.getLogger("app.agent.loop")

MAX_ROUNDS = 50
APPROVAL_TOOLS = {"write", "edit", "shell"}
RULE_KIND = {"write": "write_path", "edit": "edit_path", "shell": "shell_prefix"}

_loops: dict[str, "AgentLoop"] = {}


def _run_file_tool(root: Path, name: str, arguments: dict, should_stop: Callable[[], bool]) -> dict:
    if name == "read":
        return read_text(root, arguments.get("path"))
    if name == "write":
        return write_text(root, arguments.get("path"), arguments.get("content"))
    if name == "edit":
        return edit_text(root, arguments.get("path"), arguments.get("old_string"), arguments.get("new_string"))
    if name == "glob":
        return glob_files(root, arguments.get("pattern"))
    if name == "grep":
        return grep_text(root, arguments.get("pattern"), arguments.get("path"), should_stop=should_stop)
    return {"text": "未知工具", "is_error": True}


def history_from(rows: list[dict]) -> list[HistoryMessage]:
    return [
        HistoryMessage(
            id=row["id"],
            role=row["role"],
            content=row.get("content") or "",
            tool_calls=row.get("tool_calls"),
            tool_call_id=row.get("tool_call_id"),
            thinking=row.get("thinking"),
        )
        for row in rows
    ]


def get_loop(session_id: str) -> "AgentLoop":
    loop = _loops.get(session_id)
    if loop is None:
        loop = AgentLoop(session_id)
        _loops[session_id] = loop
    return loop


def peek_loop(session_id: str) -> "AgentLoop | None":
    return _loops.get(session_id)


def drop_loop(session_id: str) -> None:
    loop = _loops.pop(session_id, None)
    if loop is None:
        return
    loop.stop_flag = True
    loop.stop_event.set()
    loop.gate.cancel_as_reject()
    loop._kill_shell()
    for task in (loop.worker, loop.compress_task):
        if task is not None and not task.done():
            task.cancel()


async def shutdown_loop(session_id: str) -> None:
    loop = _loops.get(session_id)
    if loop is None:
        return
    tasks = [task for task in (loop.worker, loop.compress_task) if task is not None and not task.done()]
    drop_loop(session_id)
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


class AgentLoop:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.state = "idle"
        self.sockets: set[WebSocket] = set()
        self.gate = ApprovalGate()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.compress_task: asyncio.Task | None = None
        self.busy = asyncio.Lock()
        self.stop_flag = False
        self.stop_event = asyncio.Event()
        self.compressing = False
        self.autocompact_paused = False
        self.shell_pid: int | None = None
        self.approval_timeout = 120.0
        self.shell_timeout = float(SHELL_TIMEOUT_SECONDS)
        self.draft_id = ""
        self.draft_content = ""

    async def broadcast(self, payload: dict) -> None:
        dead: list[WebSocket] = []
        for socket in self.sockets:
            try:
                await socket.send_json(payload)
            except Exception:
                dead.append(socket)
        for socket in dead:
            self.sockets.discard(socket)

    async def _set_state(self, state: str) -> None:
        self.state = state
        await self.broadcast({"type": "agent.state", "state": state})

    async def _root(self) -> Path:
        session = await get_session(self.session_id)
        if session and session.get("workdir"):
            return Path(session["workdir"])
        root = session_directory(get_settings().workdir_path(), self.session_id)
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _kill_shell(self) -> None:
        if self.shell_pid:
            kill_group(self.shell_pid)

    def _drop_queue(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self.queue.task_done()

    def _ensure_worker(self) -> None:
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._drain())

    async def hello(self) -> dict | None:
        session = await get_session(self.session_id)
        if session is None:
            return None
        payload = {
            "type": "session.hello",
            "session_id": session["id"],
            "title": session["title"],
            "summary": session["summary"],
            "compacted_before_id": session["compacted_before_id"],
            "messages": await list_messages(self.session_id),
            "state": self.state,
        }
        if self.gate.request is not None:
            payload["approval"] = self.gate.request
        usage = await self._context_usage()
        if usage is not None:
            payload["context_chars"] = usage["chars"]
            payload["context_limit"] = usage["limit"]
        return payload

    async def on_user_message(self, text: str) -> None:
        cleaned = str(text).strip()
        if not cleaned:
            return
        config = await get_llm()
        if not config["base_url"].strip() or not config["model"].strip():
            await self.broadcast({"type": "error", "message": "请先在设置里填写模型"})
            return
        await self.queue.put(cleaned)
        self._ensure_worker()

    async def on_stop(self) -> None:
        if self.state not in {"running", "awaiting_approval"} and not self.compressing:
            return
        self.stop_flag = True
        self.stop_event.set()
        self.gate.cancel_as_reject()
        self._kill_shell()
        self._drop_queue()

    async def on_compress(self) -> None:
        if (
            self.compressing
            or self.state in {"running", "awaiting_approval"}
            or self.busy.locked()
            or (self.compress_task is not None and not self.compress_task.done())
        ):
            await self.broadcast({"type": "error", "message": "正在运行"})
            return
        self.compress_task = asyncio.create_task(self._compress_guard())

    async def on_approval(self, decision: str) -> None:
        if decision not in {"approve", "approve_always", "reject"}:
            await self.broadcast({"type": "error", "message": "审批决定无效"})
            return
        if not self.gate.resolve(decision):
            await self.broadcast({"type": "error", "message": "当前没有等待审批"})

    async def model_messages(self) -> list[dict]:
        session = await get_session(self.session_id)
        if session is None:
            return []
        config = await get_llm()
        history = history_from(await list_messages(self.session_id))
        has_summary = bool((session["summary"] or "").strip() and session["compacted_before_id"])
        return build_model_messages(
            history,
            context_limit=int(config["context_limit"]),
            compacted_before_id=session["compacted_before_id"],
            system=system_prompt(str(await self._root()), has_summary),
            summary=session["summary"] or "",
            tools=TOOLS,
        )

    async def _drain(self) -> None:
        while True:
            text = await self.queue.get()
            try:
                async with self.busy:
                    await self._run_user_turn(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("turn failed")
                await self.broadcast({"type": "error", "message": "运行失败"})
                await self._set_state("error")
            finally:
                self.queue.task_done()

    async def _run_user_turn(self, text: str) -> None:
        self.stop_flag = False
        self.stop_event.clear()
        try:
            row = await add_message(self.session_id, "user", text)
        except NotFound:
            await self.broadcast({"type": "error", "message": "会话不存在"})
            return
        await self.broadcast(
            {
                "type": "message.done",
                "id": row["id"],
                "role": "user",
                "content": row["content"],
                "thinking": None,
                "tool_calls": None,
                "created_at": row["created_at"],
                "title": row["title"],
            }
        )
        self.autocompact_paused = False
        await self._set_state("running")
        for _ in range(MAX_ROUNDS):
            if self.stop_flag:
                await self._set_state("idle")
                return
            calls = await self._one_completion()
            if calls is None:
                return
            if not calls:
                await self._set_state("done")
                return
            status = await self._run_tools(calls)
            if status == "stop":
                if self.state == "running":
                    await self._set_state("idle")
                return
            if status == "finish":
                await self._set_state("done")
                return
        await self._say("已达到 50 轮，停止继续请求模型。")
        await self._set_state("done")

    async def _say(self, text: str) -> None:
        row = await add_message(self.session_id, "assistant", text)
        await self.broadcast(
            {
                "type": "message.done",
                "id": row["id"],
                "role": "assistant",
                "content": text,
                "thinking": None,
                "tool_calls": None,
                "created_at": row["created_at"],
            }
        )

    async def _done_message(self, row: dict, content: str, thinking: str, tool_calls: list | None) -> None:
        await self.broadcast(
            {
                "type": "message.done",
                "id": row["id"],
                "role": "assistant",
                "content": content,
                "thinking": thinking or None,
                "tool_calls": tool_calls,
                "created_at": row["created_at"],
            }
        )

    async def _one_completion(self) -> list[ParsedToolCall] | None:
        compact = await self._compact_now("auto")
        if compact == "stop":
            await self._set_state("idle")
            return None
        return await self._complete_round(allow_overflow_retry=True)

    async def _complete_round(
        self,
        *,
        allow_overflow_retry: bool,
        row: dict | None = None,
        content: list[str] | None = None,
        thinking: list[str] | None = None,
    ) -> list[ParsedToolCall] | None:
        config = await get_llm()
        messages = await self.model_messages()
        if not messages:
            await self.broadcast({"type": "error", "message": "会话不存在"})
            await self._set_state("error")
            return None
        if row is None:
            row = await add_message(self.session_id, "assistant", "")
            content = []
            thinking = []
            self.draft_id = row["id"]
            self.draft_content = ""
            await self.broadcast(
                {"type": "message.start", "id": row["id"], "role": "assistant", "created_at": row["created_at"]}
            )
        else:
            content = content if content is not None else []
            thinking = thinking if thinking is not None else []
            content.clear()
            thinking.clear()
            self.draft_content = ""
        assert content is not None and thinking is not None
        assembler = ToolCallAssembler()

        async def on_delta(delta: dict) -> None:
            if self.stop_flag:
                raise RunStopped()
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                content.append(piece)
                self.draft_content = "".join(content)
                await self.broadcast(
                    {"type": "message.delta", "id": row["id"], "channel": "content", "text": piece}
                )
            reason = delta.get("reasoning_content")
            if not isinstance(reason, str):
                reason = delta.get("reasoning")
            if isinstance(reason, str) and reason:
                thinking.append(reason)
                await self.broadcast(
                    {"type": "message.delta", "id": row["id"], "channel": "thinking", "text": reason}
                )
            tool_calls = delta.get("tool_calls")
            if tool_calls:
                assembler.add(tool_calls)

        try:
            await stream_chat(
                base_url=config["base_url"],
                model=config["model"],
                api_key=config["api_key"],
                messages=messages,
                tools=TOOLS,
                on_delta=on_delta,
                stop=self.stop_event,
            )
        except RunStopped:
            await self._stop_draft(row, content, thinking)
            return None
        except ModelError as exc:
            if allow_overflow_retry and is_context_length_error(exc.message):
                retry = await self._compact_now("overflow")
                if retry == "stop":
                    await self._stop_draft(row, content, thinking)
                    return None
                if retry == "done":
                    return await self._complete_round(
                        allow_overflow_retry=False,
                        row=row,
                        content=content,
                        thinking=thinking,
                    )
                exc = ModelError("上下文过长，已停止")
            elif not allow_overflow_retry and is_context_length_error(exc.message):
                exc = ModelError("上下文仍然过长，已停止")
            await self._fail_draft(row, exc.message, thinking)
            return None

        calls = assembler.finish()
        text = "".join(content)
        thought = "".join(thinking)
        self.draft_content = text
        api_calls = self._api_calls(calls) if calls else None
        await update_message(row["id"], content=text, thinking=thought or None, tool_calls=api_calls)
        await self._done_message(row, text, thought, api_calls)
        return calls

    async def _stop_draft(self, row: dict, content: list[str], thinking: list[str]) -> None:
        text = "".join(content)
        text = f"{text}\n已停止" if text else "已停止"
        self.draft_content = text
        thought = "".join(thinking)
        await update_message(row["id"], content=text, thinking=thought or None)
        await self._done_message(row, text, thought, None)
        await self._set_state("idle")

    async def _fail_draft(self, row: dict, message: str, thinking: list[str]) -> None:
        thought = "".join(thinking)
        self.draft_content = message
        await update_message(row["id"], content=message, thinking=thought or None)
        await self._done_message(row, message, thought, None)
        await self.broadcast({"type": "error", "message": message})
        await self._set_state("error")

    def _api_calls(self, calls: list[ParsedToolCall]) -> list[dict]:
        payload = []
        for index, call in enumerate(calls):
            call_id = call.call_id or f"call-{index}"
            call.call_id = call_id
            arguments = "{}" if call.error else (call.raw or "{}")
            payload.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": call.name or "unknown", "arguments": arguments},
                }
            )
        return payload

    async def _run_tools(self, calls: list[ParsedToolCall]) -> str:
        finished = False
        for index, call in enumerate(calls):
            if self.stop_flag:
                await self._abort_from(calls, index)
                return "stop"
            call_id = call.call_id or f"call-{index}"
            call.call_id = call_id
            arguments = call.arguments if isinstance(call.arguments, dict) else {}
            name = call.name or "unknown"
            await self._tool_start(call_id, name, arguments if not call.error else {})
            if call.error:
                await self._store_tool(call_id, name, {}, call.error, True, "直接执行", None, None)
                if await self._after_tool() == "stop":
                    await self._abort_from(calls, index + 1)
                    return "stop"
                continue
            if name == "finish_task":
                message = str(arguments.get("message") or "")
                if message:
                    prefix = "" if not self.draft_content or self.draft_content.endswith("\n") else "\n"
                    await self._append_content(f"{prefix}{message}")
                await self._store_tool(call_id, name, arguments, "已结束", False, "直接执行", None, None)
                finished = True
                if await self._after_tool() == "stop":
                    await self._abort_from(calls, index + 1)
                    return "stop"
                continue
            if name in APPROVAL_TOOLS:
                status = await self._approve_and_run(call_id, name, arguments)
            else:
                status = await self._invoke(call_id, name, arguments, "直接执行", None)
            if status == "stop":
                await self._abort_from(calls, index + 1)
                return "stop"
        return "finish" if finished else "continue"

    async def _approve_and_run(self, call_id: str, name: str, arguments: dict) -> str:
        matched = await self._rule_match(name, arguments)
        if matched:
            return await self._invoke(call_id, name, arguments, "以后都允许", matched)
        prefix = self._proposed_prefix(name, arguments)
        request = {"id": new_id(), "tool": name, "arguments": arguments}
        if prefix:
            request["prefix"] = prefix
        await self._set_state("awaiting_approval")
        await self.broadcast({"type": "approval.request", **request})
        decision = await self.gate.wait(request, timeout=self.approval_timeout)
        await self.broadcast({"type": "approval.resolved", "id": request["id"], "decision": decision})
        if self.stop_flag:
            await self._store_tool(call_id, name, arguments, "已停止", True, "拒绝", None, None)
            await self._set_state("idle")
            return "stop"
        if decision in {"reject", "timeout"}:
            text = "两分钟未选择，已拒绝" if decision == "timeout" else "已拒绝，工具没有执行"
            label = "超时" if decision == "timeout" else "拒绝"
            await self._store_tool(call_id, name, arguments, text, True, label, None, None)
            await self._set_state("running")
            if await self._after_tool() == "stop":
                return "stop"
            return "continue"
        saved_prefix = None
        if decision == "approve_always" and prefix:
            try:
                await add_rule(RULE_KIND[name], prefix)
            except DuplicateRule:
                pass
            label = "以后都允许"
            saved_prefix = prefix
        else:
            label = "本次允许"
        await self._set_state("running")
        return await self._invoke(call_id, name, arguments, label, saved_prefix)

    async def _rule_match(self, name: str, arguments: dict) -> str | None:
        rules = await list_rules()
        if name == "shell":
            patterns = [item["pattern"] for item in rules if item["kind"] == "shell_prefix"]
            return matched_pattern(str(arguments.get("command") or ""), patterns, spaced=True)
        if name in RULE_KIND and name != "shell":
            patterns = [item["pattern"] for item in rules if item["kind"] == RULE_KIND[name]]
            return matched_pattern(str(arguments.get("path") or ""), patterns, spaced=False)
        return None

    def _proposed_prefix(self, name: str, arguments: dict) -> str | None:
        if name == "shell":
            return shell_prefix(str(arguments.get("command") or ""))
        if name in {"write", "edit"}:
            text = str(arguments.get("path") or "").strip()
            return text or None
        return None

    async def _invoke(self, call_id: str, name: str, arguments: dict, decision: str, prefix: str | None) -> str:
        if self.stop_flag:
            await self._store_tool(call_id, name, arguments, "已停止", True, "拒绝", None, None)
            await self._set_state("idle")
            return "stop"
        started = time.perf_counter()
        try:
            result = await self._dispatch(call_id, name, arguments)
        except Exception:
            log.exception("tool failed")
            result = {"text": "工具执行失败", "is_error": True}
        duration = int((time.perf_counter() - started) * 1000)
        await self._store_tool(
            call_id,
            name,
            arguments,
            str(result.get("text") or ""),
            bool(result.get("is_error")),
            decision,
            prefix,
            duration,
            result.get("before"),
            result.get("after"),
            result.get("existed") if isinstance(result.get("existed"), bool) else None,
        )
        if self.stop_flag:
            await self._set_state("idle")
            return "stop"
        if await self._after_tool() == "stop":
            return "stop"
        return "continue"

    async def _after_tool(self) -> str:
        if self.stop_flag:
            await self._set_state("idle")
            return "stop"
        return await self._compact_now("auto")

    async def _dispatch(self, call_id: str, name: str, arguments: dict) -> dict:
        root = await self._root()
        if name in {"read", "write", "edit", "glob", "grep"}:
            return await asyncio.to_thread(_run_file_tool, root, name, arguments, lambda: self.stop_flag)
        if name == "shell":
            async def on_output(text: str) -> None:
                await self.broadcast({"type": "tool.progress", "tool_call_id": call_id, "text": text})

            def on_start(pid: int) -> None:
                self.shell_pid = pid

            try:
                return await run_shell(
                    root,
                    arguments.get("command"),
                    on_output=on_output,
                    on_start=on_start,
                    stop=self.stop_event,
                    timeout=self.shell_timeout,
                )
            finally:
                self.shell_pid = None
        if name == "web_search":
            found = await search_web(
                arguments.get("query"),
                arguments.get("country", "ALL"),
                arguments.get("search_lang", "zh-hans"),
            )
            if not found.get("ok"):
                return {"text": str(found.get("error") or "搜索服务失败"), "is_error": True}
            return {"text": json.dumps(found, ensure_ascii=False), "is_error": False}
        return {"text": "未知工具", "is_error": True}

    async def _abort_from(self, calls: list[ParsedToolCall], index: int) -> None:
        for offset, call in enumerate(calls[index:], start=index):
            call_id = call.call_id or f"call-{offset}"
            call.call_id = call_id
            arguments = call.arguments if isinstance(call.arguments, dict) else {}
            name = call.name or "unknown"
            await self._tool_start(call_id, name, arguments)
            await self._store_tool(call_id, name, arguments, "已停止", True, "拒绝", None, None)
        await self._set_state("idle")

    async def _tool_start(self, call_id: str, name: str, arguments: dict) -> None:
        await self.broadcast(
            {"type": "tool.start", "tool_call_id": call_id, "name": name, "arguments": arguments}
        )

    async def _append_content(self, text: str) -> None:
        if not text or not self.draft_id:
            return
        self.draft_content = f"{self.draft_content}{text}"
        await update_message(self.draft_id, content=self.draft_content)
        await self.broadcast(
            {"type": "message.delta", "id": self.draft_id, "channel": "content", "text": text}
        )

    async def _store_tool(
        self,
        call_id: str,
        name: str,
        arguments: dict,
        text: str,
        is_error: bool,
        decision: str,
        prefix: str | None,
        duration: int | None,
        before: str | None = None,
        after: str | None = None,
        existed: bool | None = None,
    ) -> None:
        visible = text if len(text) <= 32000 else clip_text(text, 32000)
        diff = None
        if before is not None:
            diff = {"before": before[:8000], "after": (after or "")[:8000]}
        row = await add_message(self.session_id, "tool", visible, tool_call_id=call_id)
        payload: dict = {"tool_call_id": call_id, "text": visible}
        if diff is not None:
            payload["before"] = diff["before"]
            payload["after"] = diff["after"]
        if prefix:
            payload["prefix"] = prefix
        raw = json.dumps(payload, ensure_ascii=False)
        while len(raw) > 32000 and len(str(payload.get("text") or "")) > 500:
            payload["text"] = clip_text(str(payload["text"]), max(500, len(str(payload["text"])) // 2))
            if "before" in payload:
                payload["before"] = str(payload["before"])[: max(200, len(str(payload["before"])) // 2)]
                payload["after"] = str(payload["after"])[: max(200, len(str(payload["after"])) // 2)]
            raw = json.dumps(payload, ensure_ascii=False)
        revert_before = before if before is not None and isinstance(existed, bool) and not is_error else None
        revert_existed = existed if revert_before is not None else None
        await add_tool_log(
            self.session_id,
            name,
            arguments,
            raw,
            is_error=is_error,
            decision=decision,
            duration_ms=duration,
            tool_call_id=call_id,
            revert_before=revert_before,
            revert_existed=revert_existed,
        )
        await self.broadcast(
            {
                "type": "tool.result",
                "id": row["id"],
                "tool_call_id": call_id,
                "name": name,
                "arguments": arguments,
                "text": visible,
                "is_error": is_error,
                "decision": decision,
                "prefix": prefix or "",
                "diff": diff,
            }
        )

    async def _compress_guard(self) -> None:
        try:
            async with self.busy:
                await self._compress_body()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("compress failed")
            await self.broadcast({"type": "error", "message": "压缩失败"})
            if self.state == "running":
                await self._set_state("idle")
            self.compressing = False

    async def _compress_body(self) -> None:
        self.stop_flag = False
        self.stop_event.clear()
        await self._set_state("running")
        try:
            await self._compact_now("manual")
        finally:
            self.compressing = False
            if self.state == "running":
                await self._set_state("idle")

    async def _context_snapshot(self) -> dict | None:
        config = await get_llm()
        session = await get_session(self.session_id)
        if session is None:
            return None
        history = history_from(await list_messages(self.session_id))
        summary = session["summary"] or ""
        boundary = session["compacted_before_id"]
        has_summary = bool(summary.strip() and boundary)
        system = system_prompt(str(await self._root()), has_summary)
        limit = int(config["context_limit"])
        occupancy = context_occupancy(
            history,
            compacted_before_id=boundary,
            system=system,
            summary=summary,
            tools=TOOLS,
        )
        return {
            "config": config,
            "history": history,
            "summary": summary,
            "boundary": boundary,
            "system": system,
            "limit": limit,
            "occupancy": occupancy,
        }

    async def _context_usage(self) -> dict | None:
        snap = await self._context_snapshot()
        if snap is None:
            return None
        return {"chars": snap["occupancy"], "limit": snap["limit"]}

    async def _publish_usage(self, chars: int, limit: int) -> None:
        await self.broadcast({"type": "context.usage", "chars": chars, "limit": limit})

    async def _pause_autocompact(self, message: str) -> None:
        self.autocompact_paused = True
        await self.broadcast({"type": "error", "message": message})

    async def _compact_now(self, mode: str) -> str:
        if mode == "auto" and self.autocompact_paused:
            snap = await self._context_snapshot()
            if snap is not None:
                await self._publish_usage(snap["occupancy"], snap["limit"])
            return "skipped"
        if self.stop_flag and mode != "manual":
            return "stop"
        snap = await self._context_snapshot()
        if snap is None:
            return "skipped"
        await self._publish_usage(snap["occupancy"], snap["limit"])
        config = snap["config"]
        if not config["base_url"].strip() or not config["model"].strip():
            if mode != "auto":
                await self.broadcast({"type": "error", "message": "请先在设置里填写模型"})
            return "failed"
        if mode == "auto" and not over_auto_threshold(snap["occupancy"], snap["limit"]):
            return "skipped"
        dropped = messages_to_fold(
            snap["history"],
            compacted_before_id=snap["boundary"],
            context_limit=snap["limit"],
            system=snap["system"],
            tools=TOOLS,
        )
        if not dropped:
            if mode == "manual":
                await self.broadcast({"type": "error", "message": "没有更早的消息可压缩"})
                return "skipped"
            if mode == "auto" and over_auto_threshold(snap["occupancy"], snap["limit"]):
                await self._pause_autocompact("当前这一轮已经超过上下文上限的 90%，无法继续压缩")
            return "blocked"
        before = snap["occupancy"]
        self.compressing = True
        try:
            if self.stop_flag:
                await self.broadcast({"type": "error", "message": "已取消压缩"})
                return "stop"
            await self.broadcast({"type": "compress.start", "auto": mode == "auto"})
            chunks: list[str] = []

            async def on_delta(delta: dict) -> None:
                if self.stop_flag:
                    raise RunStopped()
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    chunks.append(piece)
                    await self.broadcast({"type": "compress.delta", "text": piece})

            await stream_chat(
                base_url=config["base_url"],
                model=config["model"],
                api_key=config["api_key"],
                messages=[
                    {"role": "system", "content": compression_prompt()},
                    {"role": "user", "content": compression_input(dropped, snap["summary"])},
                ],
                tools=None,
                on_delta=on_delta,
                stop=self.stop_event,
            )
            summary = "".join(chunks).strip()
            if not summary:
                await self.broadcast({"type": "error", "message": "总结为空"})
                if mode != "manual":
                    self.autocompact_paused = True
                return "failed"
            await set_summary(self.session_id, summary, dropped[-1].id)
            await self.broadcast(
                {
                    "type": "compress.done",
                    "summary": summary,
                    "compacted_before_id": dropped[-1].id,
                    "auto": mode == "auto",
                }
            )
        except RunStopped:
            await self.broadcast({"type": "error", "message": "已取消压缩"})
            return "stop"
        except ModelError as exc:
            await self.broadcast({"type": "error", "message": exc.message})
            if mode != "manual":
                self.autocompact_paused = True
            return "failed"
        finally:
            self.compressing = False
        again = await self._context_snapshot()
        if again is None:
            return "done"
        await self._publish_usage(again["occupancy"], again["limit"])
        if again["occupancy"] >= before:
            if mode == "manual":
                await self.broadcast({"type": "error", "message": "压缩后占用没有下降"})
            else:
                await self._pause_autocompact("压缩后占用没有下降，已停止自动压缩")
        return "done"
