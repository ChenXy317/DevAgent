from typing import NoReturn

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent.executor import probe_model
from app.agent.loop import peek_loop, shutdown_loop
from app.persist import (
    BadRequest,
    DuplicateRule,
    NotFound,
    add_rule,
    create_session,
    delete_rule,
    delete_session,
    get_llm,
    get_session,
    list_messages,
    list_rules,
    list_sessions,
    read_llm_public,
    update_llm,
)

router = APIRouter()


class LlmUpdate(BaseModel):
    base_url: str = ""
    model: str = ""
    api_key: str | None = None
    context_turns: int = 20


class RuleCreate(BaseModel):
    kind: str
    pattern: str


class LlmProbe(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


def _raise_known(exc: Exception) -> NoReturn:
    if isinstance(exc, NotFound):
        raise HTTPException(status_code=404, detail="找不到记录") from exc
    if isinstance(exc, DuplicateRule):
        raise HTTPException(status_code=409, detail="这条规则已存在") from exc
    if isinstance(exc, BadRequest):
        raise HTTPException(status_code=400, detail=exc.message) from exc
    raise exc


@router.get("/health")
async def health() -> dict:
    return {"ok": True}


@router.get("/api/sessions")
async def sessions_list() -> list[dict]:
    return await list_sessions()


@router.post("/api/sessions")
async def sessions_create() -> dict:
    return await create_session()


@router.get("/api/sessions/{session_id}")
async def sessions_get(session_id: str) -> dict:
    row = await get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return row


@router.delete("/api/sessions/{session_id}")
async def sessions_delete(session_id: str) -> dict:
    loop = peek_loop(session_id)
    if loop is not None:
        loop.stop_flag = True
        loop.stop_event.set()
        loop.gate.cancel_as_reject()
        loop._kill_shell()
        await loop.broadcast({"type": "session.deleted"})
    try:
        await delete_session(session_id)
    except NotFound as exc:
        await shutdown_loop(session_id)
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    await shutdown_loop(session_id)
    return {"ok": True}


@router.get("/api/sessions/{session_id}/messages")
async def sessions_messages(session_id: str) -> list[dict]:
    try:
        return await list_messages(session_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc


@router.get("/api/settings/llm")
async def llm_get() -> dict:
    try:
        return await read_llm_public()
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="模型配置不存在") from exc


@router.put("/api/settings/llm")
async def llm_put(body: LlmUpdate) -> dict:
    try:
        return await update_llm(body.base_url, body.model, body.api_key, body.context_turns)
    except (NotFound, BadRequest) as exc:
        _raise_known(exc)


@router.post("/api/settings/llm/probe")
async def llm_probe(body: LlmProbe | None = None) -> dict:
    try:
        config = await get_llm()
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="模型配置不存在") from exc
    base_url = config["base_url"]
    model = config["model"]
    api_key = config["api_key"]
    if body is not None:
        if body.base_url is not None:
            base_url = body.base_url.strip()
        if body.model is not None:
            model = body.model.strip()
        if body.api_key and body.api_key.strip():
            api_key = body.api_key.strip()
    error = await probe_model(base_url, model, api_key)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True}


@router.get("/api/settings/allow-rules")
async def rules_list() -> list[dict]:
    return await list_rules()


@router.post("/api/settings/allow-rules")
async def rules_create(body: RuleCreate) -> dict:
    try:
        return await add_rule(body.kind, body.pattern)
    except (BadRequest, DuplicateRule) as exc:
        _raise_known(exc)


@router.delete("/api/settings/allow-rules/{rule_id}")
async def rules_delete(rule_id: int) -> dict:
    try:
        await delete_rule(rule_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="规则不存在") from exc
    return {"ok": True}
