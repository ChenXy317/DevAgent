import json
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.context import MAX_CONTEXT_LIMIT, MIN_CONTEXT_LIMIT
from app.agent.rewind import RevertStep, RewindError, apply_reverts, restore_snapshots
from app.config import get_settings
from app.db import get_session_factory
from app.models import (
    AllowRuleRecord,
    LlmConfigRecord,
    MessageRecord,
    SessionRecord,
    ToolLogRecord,
    utcnow,
)
from app.tools.files import PathRejected, resolve_bound_directory, session_directory

RULE_KINDS = {"shell_prefix", "write_path", "edit_path"}
LOG_LIMIT = 32000
_seq = 0


def new_id() -> str:
    global _seq
    _seq += 1
    return f"{time.time_ns():020d}-{_seq:06d}"


class NotFound(Exception):
    pass


class DuplicateRule(Exception):
    pass


class BadRequest(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds") + "Z"


def session_dict(row: SessionRecord) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "summary": row.summary,
        "compacted_before_id": row.compacted_before_id,
        "workdir": row.workdir or "",
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def message_dict(row: MessageRecord) -> dict:
    return {
        "id": row.id,
        "role": row.role,
        "content": row.content,
        "tool_calls": row.tool_calls,
        "tool_call_id": row.tool_call_id,
        "thinking": row.thinking,
        "created_at": _iso(row.created_at),
    }


def rule_dict(row: AllowRuleRecord) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "pattern": row.pattern,
        "created_at": _iso(row.created_at),
    }


async def list_sessions() -> list[dict]:
    factory = get_session_factory()
    async with factory() as db:
        rows = (
            await db.scalars(select(SessionRecord).order_by(SessionRecord.updated_at.desc()))
        ).all()
        return [session_dict(row) for row in rows]


async def create_session(workdir: str | None = None) -> dict:
    bound = None
    if workdir and workdir.strip():
        try:
            bound = str(resolve_bound_directory(workdir, get_settings().projects_root_path()))
        except PathRejected as exc:
            raise BadRequest(exc.message) from exc
    factory = get_session_factory()
    now = utcnow()
    row = SessionRecord(
        id=str(uuid.uuid4()),
        title="",
        summary="",
        workdir=bound,
        created_at=now,
        updated_at=now,
    )
    async with factory() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return session_dict(row)


async def get_session(session_id: str) -> dict | None:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(SessionRecord, session_id)
        if row is None:
            return None
        return session_dict(row)


async def delete_session(session_id: str) -> None:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(SessionRecord, session_id)
        if row is None:
            raise NotFound()
        row.compacted_before_id = None
        await db.flush()
        logs = (
            await db.scalars(select(ToolLogRecord).where(ToolLogRecord.session_id == session_id))
        ).all()
        for log in logs:
            await db.delete(log)
        messages = (
            await db.scalars(select(MessageRecord).where(MessageRecord.session_id == session_id))
        ).all()
        for message in messages:
            await db.delete(message)
        bound = row.workdir or ""
        await db.delete(row)
        await db.commit()
    if bound:
        return
    directory = session_directory(get_settings().workdir_path(), session_id)
    if directory.exists():
        shutil.rmtree(directory)


def _clip_log(text: str) -> str:
    if len(text) <= LOG_LIMIT:
        return text
    head = LOG_LIMIT // 2
    tail = LOG_LIMIT - head
    return f"{text[:head]}\n...[已截断]...\n{text[-tail:]}"


def _log_view(row: ToolLogRecord) -> dict | None:
    try:
        payload = json.loads(row.result)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not payload.get("tool_call_id"):
        return None
    view = {
        "tool_call_id": payload["tool_call_id"],
        "decision": row.decision,
        "is_error": row.is_error,
        "tool_name": row.tool_name,
        "arguments": row.arguments,
        "prefix": payload.get("prefix") or "",
    }
    if "before" in payload and "after" in payload:
        view["diff"] = {"before": payload.get("before") or "", "after": payload.get("after") or ""}
    return view


async def list_messages(session_id: str) -> list[dict]:
    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(SessionRecord, session_id)
        if session is None:
            raise NotFound()
        rows = (
            await db.scalars(
                select(MessageRecord)
                .where(MessageRecord.session_id == session_id)
                .order_by(MessageRecord.created_at.asc(), MessageRecord.id.asc())
            )
        ).all()
        logs = (
            await db.scalars(select(ToolLogRecord).where(ToolLogRecord.session_id == session_id))
        ).all()
        by_call = {}
        for log in logs:
            view = _log_view(log)
            if view is not None:
                by_call[view["tool_call_id"]] = view
        messages = []
        for row in rows:
            item = message_dict(row)
            extra = by_call.get(row.tool_call_id or "")
            if extra is not None:
                item["decision"] = extra["decision"]
                item["is_error"] = extra["is_error"]
                item["tool_name"] = extra["tool_name"]
                item["arguments"] = extra["arguments"]
                item["prefix"] = extra["prefix"]
                if "diff" in extra:
                    item["diff"] = extra["diff"]
            messages.append(item)
        return messages


async def add_message(
    session_id: str,
    role: str,
    content: str,
    *,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
    thinking: str | None = None,
) -> dict:
    factory = get_session_factory()
    now = utcnow()
    async with factory() as db:
        session = await db.get(SessionRecord, session_id)
        if session is None:
            raise NotFound()
        if role == "user" and not session.title and content.strip():
            session.title = content.strip()[:40]
        session.updated_at = now
        row = MessageRecord(
            id=new_id(),
            session_id=session_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            thinking=thinking,
            created_at=now,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        payload = message_dict(row)
        payload["title"] = session.title
        return payload


async def update_message(
    message_id: str,
    *,
    content: str | None = None,
    thinking: str | None = None,
    tool_calls: list | None = None,
) -> None:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(MessageRecord, message_id)
        if row is None:
            raise NotFound()
        if content is not None:
            row.content = content
        if thinking is not None:
            row.thinking = thinking
        if tool_calls is not None:
            row.tool_calls = tool_calls
        await db.commit()


async def set_summary(session_id: str, summary: str, compacted_before_id: str) -> None:
    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(SessionRecord, session_id)
        if session is None:
            raise NotFound()
        session.summary = summary
        session.compacted_before_id = compacted_before_id
        session.updated_at = utcnow()
        await db.commit()


async def add_tool_log(
    session_id: str,
    tool_name: str,
    arguments: dict | None,
    result: str,
    *,
    is_error: bool,
    decision: str,
    duration_ms: int | None = None,
    tool_call_id: str | None = None,
    revert_before: str | None = None,
    revert_existed: bool | None = None,
) -> None:
    factory = get_session_factory()
    row = ToolLogRecord(
        id=new_id(),
        session_id=session_id,
        tool_name=tool_name,
        arguments=arguments,
        result=_clip_log(result),
        is_error=is_error,
        decision=decision,
        duration_ms=duration_ms,
        tool_call_id=tool_call_id,
        revert_before=revert_before,
        revert_existed=revert_existed,
        created_at=utcnow(),
    )
    async with factory() as db:
        db.add(row)
        await db.commit()


async def get_llm() -> dict:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(LlmConfigRecord, 1)
        if row is None:
            raise NotFound()
        return {
            "base_url": row.base_url,
            "model": row.model,
            "api_key": row.api_key,
            "context_limit": row.context_limit,
        }


def llm_public(row: dict) -> dict:
    return {
        "base_url": row["base_url"],
        "model": row["model"],
        "api_key_set": bool(row["api_key"]),
        "context_limit": row["context_limit"],
    }


async def read_llm_public() -> dict:
    return llm_public(await get_llm())


async def read_llm_secret() -> str:
    return (await get_llm())["api_key"]


async def update_llm(
    base_url: str,
    model: str,
    api_key: str | None,
    context_limit: int,
) -> dict:
    if not base_url.strip():
        raise BadRequest("请填写模型地址")
    if not model.strip():
        raise BadRequest("请填写模型名")
    if context_limit < MIN_CONTEXT_LIMIT or context_limit > MAX_CONTEXT_LIMIT:
        raise BadRequest(f"上下文上限需要在 {MIN_CONTEXT_LIMIT} 到 {MAX_CONTEXT_LIMIT} 之间")
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(LlmConfigRecord, 1)
        if row is None:
            raise NotFound()
        row.base_url = base_url.strip()
        row.model = model.strip()
        row.context_limit = context_limit
        row.updated_at = utcnow()
        if api_key:
            row.api_key = api_key
        await db.commit()
        return llm_public(
            {
                "base_url": row.base_url,
                "model": row.model,
                "api_key": row.api_key,
                "context_limit": row.context_limit,
            }
        )


async def list_rules() -> list[dict]:
    factory = get_session_factory()
    async with factory() as db:
        rows = (await db.scalars(select(AllowRuleRecord).order_by(AllowRuleRecord.id.asc()))).all()
        return [rule_dict(row) for row in rows]


async def add_rule(kind: str, pattern: str) -> dict:
    if kind not in RULE_KINDS:
        raise BadRequest("规则种类无效")
    text = pattern.strip()
    if not text:
        raise BadRequest("请填写前缀")
    factory = get_session_factory()
    row = AllowRuleRecord(kind=kind, pattern=text, created_at=utcnow())
    async with factory() as db:
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            raise DuplicateRule() from None
        await db.refresh(row)
        return rule_dict(row)


async def delete_rule(rule_id: int) -> None:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(AllowRuleRecord, rule_id)
        if row is None:
            raise NotFound()
        await db.delete(row)
        await db.commit()


def _logged_call_id(row: ToolLogRecord) -> str | None:
    if row.tool_call_id:
        return row.tool_call_id
    try:
        payload = json.loads(row.result)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("tool_call_id"), str) and payload["tool_call_id"]:
        return payload["tool_call_id"]
    return None


def _arg(arguments: dict | None, key: str) -> str:
    if not isinstance(arguments, dict):
        return ""
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) else ""


async def rewind_session(session_id: str, message_id: str) -> dict:
    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(SessionRecord, session_id)
        if session is None:
            raise NotFound()
        messages = (
            await db.scalars(
                select(MessageRecord)
                .where(MessageRecord.session_id == session_id)
                .order_by(MessageRecord.created_at.asc(), MessageRecord.id.asc())
            )
        ).all()
        pivot = next((item for item in messages if item.id == message_id), None)
        if pivot is None:
            raise BadRequest("找不到这条消息")
        if pivot.role != "user":
            raise BadRequest("只能回退到用户消息")
        later: list[MessageRecord] = []
        seen = False
        for item in messages:
            if seen:
                later.append(item)
            elif item.id == pivot.id:
                seen = True
        later_calls = {item.tool_call_id for item in later if item.tool_call_id}
        logs = (
            await db.scalars(
                select(ToolLogRecord)
                .where(ToolLogRecord.session_id == session_id)
                .order_by(ToolLogRecord.created_at.asc(), ToolLogRecord.id.asc())
            )
        ).all()
        matched = [log for log in logs if (call_id := _logged_call_id(log)) and call_id in later_calls]
        steps: list[RevertStep] = []
        skipped: list[str] = []
        for log in matched:
            path = _arg(log.arguments, "path")
            if log.tool_name == "shell":
                skipped.append(f"命令未撤销: {_arg(log.arguments, 'command') or '命令'}")
                continue
            if log.tool_name not in {"write", "edit"} or log.is_error:
                continue
            if log.revert_existed is None or log.revert_before is None or not path:
                label = "修改未撤销" if log.tool_name == "edit" else "写入未撤销"
                skipped.append(f"{label}: {path or '文件'}")
                continue
            steps.append(RevertStep(path=path, existed=bool(log.revert_existed), before=log.revert_before))
        steps.reverse()
        if session.workdir:
            root = Path(session.workdir)
            if not root.is_dir():
                raise BadRequest("绑定的目录不存在")
        else:
            root = session_directory(get_settings().workdir_path(), session_id)
            root.mkdir(parents=True, exist_ok=True)
        try:
            restored, snapshots = apply_reverts(root, steps)
        except RewindError as exc:
            raise BadRequest(exc.message) from exc
        deleted_ids = {item.id for item in later}
        if session.compacted_before_id and session.compacted_before_id in deleted_ids:
            session.compacted_before_id = None
            session.summary = ""
            await db.flush()
        for log in matched:
            await db.delete(log)
        for item in later:
            await db.delete(item)
        session.updated_at = utcnow()
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            restore_snapshots(snapshots)
            raise
        return {
            "ok": True,
            "restored": restored,
            "skipped": skipped,
            "summary": session.summary or "",
            "compacted_before_id": session.compacted_before_id,
        }
