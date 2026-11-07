import json
import uuid
from pathlib import Path

import pytest

from app.agent.loop import drop_loop, get_loop
from app.config import get_settings
from app.persist import add_message, add_tool_log, set_summary
from app.tools.files import PathRejected, resolve_bound_directory, session_directory


def projects() -> Path:
    root = get_settings().projects_root_path()
    root.mkdir(parents=True, exist_ok=True)
    return root


def fresh(name: str) -> Path:
    path = projects() / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return path


async def record_change(
    session_id: str,
    call_id: str,
    *,
    tool: str,
    arguments: dict,
    before: str | None = None,
    existed: bool | None = None,
    is_error: bool = False,
) -> None:
    await add_message(session_id, "tool", tool, tool_call_id=call_id)
    await add_tool_log(
        session_id,
        tool,
        arguments,
        json.dumps({"tool_call_id": call_id, "text": tool}, ensure_ascii=False),
        is_error=is_error,
        decision="直接执行",
        tool_call_id=call_id,
        revert_before=before,
        revert_existed=existed,
    )


def test_bound_directory_must_stay_inside_projects(tmp_path: Path):
    root = projects()
    child = fresh("inside")
    assert resolve_bound_directory(str(child), root) == child.resolve()
    assert resolve_bound_directory(child.name, root) == child.resolve()
    with pytest.raises(PathRejected):
        resolve_bound_directory(str(root), root)
    with pytest.raises(PathRejected):
        resolve_bound_directory(str(tmp_path), root)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / f"escape-link-{uuid.uuid4().hex[:8]}"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathRejected):
        resolve_bound_directory(str(link), root)
    link.unlink()


async def test_bound_directory_survives_session_delete(client):
    folder = fresh("keep")
    target = folder / "note.txt"
    target.write_text("原来", encoding="utf-8")
    created = await client.post("/api/sessions", json={"workdir": str(folder)})
    assert created.status_code == 200
    session_id = created.json()["id"]
    assert created.json()["workdir"] == str(folder.resolve())
    target.write_text("改过", encoding="utf-8")
    deleted = await client.delete(f"/api/sessions/{session_id}")
    assert deleted.status_code == 200
    assert target.read_text(encoding="utf-8") == "改过"
    assert folder.is_dir()
    assert not session_directory(get_settings().workdir_path(), session_id).exists()


async def test_outside_path_cannot_create_session(client, tmp_path: Path):
    outside = await client.post("/api/sessions", json={"workdir": str(tmp_path)})
    assert outside.status_code == 400
    root = await client.post("/api/sessions", json={"workdir": str(projects())})
    assert root.status_code == 400
    missing = await client.post("/api/sessions", json={"workdir": str(projects() / "missing-demo")})
    assert missing.status_code == 400


async def test_rewind_two_edits_restores_original_and_drops_later_messages(client):
    folder = fresh("edit")
    target = folder / "main.py"
    target.write_text("最终", encoding="utf-8")
    created = await client.post("/api/sessions", json={"workdir": str(folder)})
    session_id = created.json()["id"]
    first = await add_message(session_id, "user", "第一次")
    await record_change(
        session_id,
        "e1",
        tool="edit",
        arguments={"path": "main.py"},
        before="最初" + "甲" * 9000,
        existed=True,
    )
    await add_message(session_id, "user", "第二次")
    await record_change(
        session_id,
        "e2",
        tool="edit",
        arguments={"path": "main.py"},
        before="中间",
        existed=True,
    )
    response = await client.post(f"/api/sessions/{session_id}/rewind", json={"message_id": first["id"]})
    assert response.status_code == 200
    body = response.json()
    assert body["restored"] == ["main.py"]
    assert target.read_text(encoding="utf-8") == "最初" + "甲" * 9000
    messages = (await client.get(f"/api/sessions/{session_id}/messages")).json()
    assert [item["content"] for item in messages] == ["第一次"]
    drop_loop(session_id)


async def test_rewind_lists_command_and_keeps_summary_outside_the_cut(client):
    folder = fresh("shell")
    target = folder / "main.py"
    target.write_text("改过", encoding="utf-8")
    created = await client.post("/api/sessions", json={"workdir": str(folder)})
    session_id = created.json()["id"]
    first = await add_message(session_id, "user", "改文件")
    await set_summary(session_id, "早期总结", first["id"])
    await record_change(
        session_id,
        "e1",
        tool="edit",
        arguments={"path": "main.py"},
        before="最初",
        existed=True,
    )
    await record_change(session_id, "s1", tool="shell", arguments={"command": "echo hello"})
    response = await client.post(f"/api/sessions/{session_id}/rewind", json={"message_id": first["id"]})
    assert response.status_code == 200
    body = response.json()
    assert target.read_text(encoding="utf-8") == "最初"
    assert body["skipped"] == ["命令未撤销: echo hello"]
    assert body["summary"] == "早期总结"
    assert body["compacted_before_id"] == first["id"]
    drop_loop(session_id)


async def test_rewind_clears_summary_when_the_boundary_is_deleted(client):
    folder = fresh("summary")
    created = await client.post("/api/sessions", json={"workdir": str(folder)})
    session_id = created.json()["id"]
    first = await add_message(session_id, "user", "留下")
    second = await add_message(session_id, "user", "删掉")
    await set_summary(session_id, "包含后面", second["id"])
    response = await client.post(f"/api/sessions/{session_id}/rewind", json={"message_id": first["id"]})
    assert response.status_code == 200
    assert response.json()["summary"] == ""
    assert response.json()["compacted_before_id"] is None
    messages = (await client.get(f"/api/sessions/{session_id}/messages")).json()
    assert [item["content"] for item in messages] == ["留下"]
    drop_loop(session_id)


async def test_rewind_deletes_a_file_that_did_not_exist(client):
    folder = fresh("create")
    target = folder / "new.py"
    target.write_text("新建", encoding="utf-8")
    created = await client.post("/api/sessions", json={"workdir": str(folder)})
    session_id = created.json()["id"]
    first = await add_message(session_id, "user", "新建文件")
    await record_change(
        session_id,
        "w1",
        tool="write",
        arguments={"path": "new.py"},
        before="",
        existed=False,
    )
    response = await client.post(f"/api/sessions/{session_id}/rewind", json={"message_id": first["id"]})
    assert response.status_code == 200
    assert not target.exists()
    drop_loop(session_id)


async def test_rewind_failure_keeps_files_and_messages(client, monkeypatch: pytest.MonkeyPatch):
    folder = fresh("fail")
    target = folder / "main.py"
    target.write_text("最终", encoding="utf-8")
    created = await client.post("/api/sessions", json={"workdir": str(folder)})
    session_id = created.json()["id"]
    first = await add_message(session_id, "user", "第一次")
    await record_change(
        session_id,
        "e1",
        tool="edit",
        arguments={"path": "main.py"},
        before="最初",
        existed=True,
    )
    await add_message(session_id, "user", "第二次")
    await record_change(
        session_id,
        "e2",
        tool="edit",
        arguments={"path": "main.py"},
        before="中间",
        existed=True,
    )
    real = Path.write_text
    calls = {"n": 0}

    def fail_second(self: Path, data: str, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(self)
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_second)
    response = await client.post(f"/api/sessions/{session_id}/rewind", json={"message_id": first["id"]})
    assert response.status_code == 400
    assert "消息未删除" in response.json()["detail"]
    assert target.read_text(encoding="utf-8") == "最终"
    messages = (await client.get(f"/api/sessions/{session_id}/messages")).json()
    assert [item["content"] for item in messages] == ["第一次", "edit", "第二次", "edit"]
    drop_loop(session_id)


async def test_rewind_rejected_while_running(client):
    folder = fresh("busy")
    created = await client.post("/api/sessions", json={"workdir": str(folder)})
    session_id = created.json()["id"]
    first = await add_message(session_id, "user", "忙")
    loop = get_loop(session_id)
    loop.state = "running"
    response = await client.post(f"/api/sessions/{session_id}/rewind", json={"message_id": first["id"]})
    assert response.status_code == 409
    loop.state = "idle"
    drop_loop(session_id)
