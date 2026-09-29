import uuid

from app.agent.loop import drop_loop, get_loop
from app.db import get_session_factory
from app.models import MessageRecord, SessionRecord, utcnow
from app.persist import read_llm_secret
from app.tools.files import session_directory
from app.config import get_settings


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_session_crud_and_directory(client):
    created = await client.post("/api/sessions")
    assert created.status_code == 200
    session_id = created.json()["id"]
    assert created.json()["title"] == ""

    listed = await client.get("/api/sessions")
    assert any(item["id"] == session_id for item in listed.json())

    fetched = await client.get(f"/api/sessions/{session_id}")
    assert fetched.status_code == 200

    missing = await client.get("/api/sessions/missing")
    assert missing.status_code == 404

    directory = session_directory(get_settings().workdir_path(), session_id)
    directory.mkdir(parents=True)
    (directory / "note.txt").write_text("hello", encoding="utf-8")

    deleted = await client.delete(f"/api/sessions/{session_id}")
    assert deleted.status_code == 200
    assert not directory.exists()
    assert (await client.get(f"/api/sessions/{session_id}")).status_code == 404
    drop_loop(session_id)


async def test_delete_session_with_compacted_message(client):
    created = await client.post("/api/sessions")
    session_id = created.json()["id"]
    message_id = str(uuid.uuid4())
    factory = get_session_factory()
    async with factory() as db:
        session = await db.get(SessionRecord, session_id)
        db.add(
            MessageRecord(
                id=message_id,
                session_id=session_id,
                role="user",
                content="先做这个",
                created_at=utcnow(),
            )
        )
        await db.flush()
        session.compacted_before_id = message_id
        await db.commit()

    response = await client.delete(f"/api/sessions/{session_id}")
    assert response.status_code == 200
    assert (await client.get(f"/api/sessions/{session_id}/messages")).status_code == 404


async def test_llm_hides_key_and_keeps_blank_update(client):
    saved = await client.put(
        "/api/settings/llm",
        json={"base_url": "http://127.0.0.1:9/v1", "model": "demo", "api_key": "secret", "context_turns": 20},
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["api_key_set"] is True
    assert "secret" not in saved.text
    assert await read_llm_secret() == "secret"

    kept = await client.put(
        "/api/settings/llm",
        json={"base_url": "http://127.0.0.1:9/v1", "model": "demo-2", "api_key": "", "context_turns": 8},
    )
    assert kept.status_code == 200
    assert kept.json()["model"] == "demo-2"
    assert kept.json()["context_turns"] == 8
    assert await read_llm_secret() == "secret"

    rejected = await client.put(
        "/api/settings/llm",
        json={"base_url": "http://127.0.0.1:9/v1", "model": "demo-2", "context_turns": 0},
    )
    assert rejected.status_code == 400

    probe = await client.post("/api/settings/llm/probe")
    assert probe.status_code == 200
    assert probe.json()["ok"] is False


async def test_allow_rules(client):
    created = await client.post(
        "/api/settings/allow-rules",
        json={"kind": "shell_prefix", "pattern": "echo hello"},
    )
    assert created.status_code == 200
    rule_id = created.json()["id"]

    duplicate = await client.post(
        "/api/settings/allow-rules",
        json={"kind": "shell_prefix", "pattern": " echo hello "},
    )
    assert duplicate.status_code == 409

    empty = await client.post(
        "/api/settings/allow-rules",
        json={"kind": "shell_prefix", "pattern": "  "},
    )
    assert empty.status_code == 400

    listed = await client.get("/api/settings/allow-rules")
    assert len(listed.json()) == 1

    deleted = await client.delete(f"/api/settings/allow-rules/{rule_id}")
    assert deleted.status_code == 200
    assert (await client.get("/api/settings/allow-rules")).json() == []


async def test_user_message_without_model(client):
    created = await client.post("/api/sessions")
    session_id = created.json()["id"]
    loop = get_loop(session_id)
    sent: list[dict] = []

    class Socket:
        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    loop.sockets.add(Socket())
    hello = await loop.hello()
    assert hello is not None
    assert hello["type"] == "session.hello"
    assert hello["state"] == "idle"
    await loop.on_user_message("   ")
    await loop.on_user_message("你好")
    assert sent[-1]["message"] == "请先在设置里填写模型"
    drop_loop(session_id)
