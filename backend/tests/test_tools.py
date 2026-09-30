import asyncio
import shlex
import sys
import time
from pathlib import Path

import pytest

from app.tools.files import edit_text, glob_files, grep_text, read_text, write_text
from app.tools.shell import Utf8Decoder, run_shell


def test_file_tools_stay_inside_directory(tmp_path: Path):
    root = tmp_path / "session"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(tmp_path)

    assert read_text(root, "../outside.txt")["text"] == "路径越界"
    assert read_text(root, str(outside))["text"] == "路径越界"
    assert read_text(root, "link/outside.txt")["text"] == "路径越界"
    assert read_text(root, "missing.txt")["text"] == "文件不存在"

    root.joinpath("sub").mkdir()
    assert read_text(root, "sub")["text"] == "这是目录"

    written = write_text(root, "nested/a.txt", "hello")
    assert written["is_error"] is False
    assert written["before"] == ""
    assert (root / "nested" / "a.txt").read_text(encoding="utf-8") == "hello"
    assert read_text(root, str(root / "nested" / "a.txt"))["text"] == "hello"
    absolute = write_text(root, str(root / "nested" / "abs.txt"), "abs")
    assert absolute["is_error"] is False
    assert (root / "nested" / "abs.txt").read_text(encoding="utf-8") == "abs"

    again = write_text(root, "nested/a.txt", "hello hello")
    assert again["before"] == "hello"
    assert again["after"] == "hello hello"
    assert edit_text(root, "nested/a.txt", "hello", "hi")["text"] == "要替换的文本出现了多次"
    assert edit_text(root, "nested/a.txt", "gone", "x")["text"] == "找不到要替换的文本"

    edited = edit_text(root, "nested/a.txt", "hello hello", "hi")
    assert edited["text"] == "已修改 nested/a.txt"
    assert (root / "nested" / "a.txt").read_text(encoding="utf-8") == "hi"

    long = "字" * 50001
    write_text(root, "long.txt", long)
    loaded = read_text(root, "long.txt")
    assert "已截断，共 50001 个字符" in loaded["text"]
    assert loaded["text"].startswith("字" * 100)

    (root / "one.py").write_text("print(1)\n", encoding="utf-8")
    (root / "two.py").write_text("print(2)\n", encoding="utf-8")
    listed = glob_files(root, "*.py")
    assert "one.py" in listed["text"]
    assert "two.py" in listed["text"]
    assert "nested/a.txt" not in listed["text"]

    found = grep_text(root, r"print\(", "one.py")
    assert found["text"] == "one.py:1:print(1)"
    assert grep_text(root, "[", "one.py")["text"] == "正则无法解析"
    assert grep_text(root, "print", seconds=0)["text"] == "搜索超时"
    assert grep_text(root, "print", should_stop=lambda: True)["text"] == "已停止"
    slow = root / "slow.txt"
    slow.write_text("a" * 40 + "\n", encoding="utf-8")
    started = time.monotonic()
    limited = grep_text(root, r"(a|aa)+b", "slow.txt")
    assert limited["text"] == "搜索超时"
    assert time.monotonic() - started < 3


async def test_shell_runs_in_session_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "session"
    monkeypatch.setenv("SEARCH_API_KEY", "super-secret")
    monkeypatch.setenv("MYSQL_PASSWORD", "db-secret")
    code = (
        "import os; "
        "print(os.environ.get('SEARCH_API_KEY')); "
        "print(os.environ.get('MYSQL_PASSWORD')); "
        "print(os.environ.get('HOME')); "
        "print('HAS_PATH' if os.environ.get('PATH') else 'NO_PATH')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    result = await run_shell(root, command)
    assert result["is_error"] is False
    assert "super-secret" not in result["text"]
    assert "db-secret" not in result["text"]
    assert "None" in result["text"]
    assert str(root) in result["text"]
    assert "HAS_PATH" in result["text"]

    echoed = await run_shell(root, "echo hello")
    assert echoed["text"].startswith("退出码 0")
    assert "hello" in echoed["text"]
    assert (await run_shell(root, "   "))["text"] == "命令为空"

    raw = ("中" * 2000).encode()
    decoder = Utf8Decoder()
    text = "".join(decoder.feed(raw[index : index + 4096]) for index in range(0, len(raw), 4096))
    text += decoder.feed(b"", final=True)
    assert text == "中" * 2000
    chinese = await run_shell(root, f"{shlex.quote(sys.executable)} -c {shlex.quote('print(chr(20013)*2000)')}")
    assert "\ufffd" not in chinese["text"]
    assert "中" * 100 in chinese["text"]
    flooded = await run_shell(root, f"{shlex.quote(sys.executable)} -c {shlex.quote('print(chr(20013)*40000)')}")
    assert "输出已截断" in flooded["text"]
    assert flooded["text"].count("中") < 40000


async def test_shell_timeout_and_stop(tmp_path: Path):
    root = tmp_path / "session"
    timed = await run_shell(root, "sleep 30", timeout=0.2)
    assert timed["is_error"] is True
    assert timed["text"].startswith("命令超时")

    stop = asyncio.Event()

    async def fire() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    task = asyncio.create_task(fire())
    stopped = await run_shell(root, "sleep 30", stop=stop, timeout=5)
    await task
    assert stopped["is_error"] is True
    assert stopped["text"].startswith("已停止")
