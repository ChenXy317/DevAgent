"""在会话目录里执行命令。"""

import asyncio
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path


SHELL_TIMEOUT_SECONDS = 300
_HIDDEN = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "MYSQL_")


def command_env(root: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(marker in upper for marker in _HIDDEN):
            continue
        env[key] = value
    path = str(root)
    env["HOME"] = path
    env["PWD"] = path
    return env


def kill_group(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        return


async def run_shell(
    root: Path,
    command: object,
    *,
    on_output: Callable[[str], Awaitable[None]] | None = None,
    on_start: Callable[[int], None] | None = None,
    stop: asyncio.Event | None = None,
    timeout: float = SHELL_TIMEOUT_SECONDS,
) -> dict:
    if not isinstance(command, str) or not command.strip():
        return {"text": "命令为空", "is_error": True}
    root.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(root),
        env=command_env(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if on_start and process.pid:
        on_start(process.pid)
    parts: list[str] = []

    async def pump(stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while True:
            data = await stream.read(4096)
            if not data:
                return
            text = data.decode("utf-8", errors="replace")
            parts.append(text)
            if on_output is not None:
                await on_output(text)

    async def watch_stop() -> None:
        if stop is None:
            return
        await stop.wait()
        if process.pid:
            kill_group(process.pid)

    watcher = asyncio.create_task(watch_stop()) if stop is not None else None
    timed_out = False
    try:
        await asyncio.wait_for(
            asyncio.gather(pump(process.stdout), pump(process.stderr)),
            timeout,
        )
    except TimeoutError:
        timed_out = True
        if process.pid:
            kill_group(process.pid)
    stopped = stop is not None and stop.is_set()
    if stopped and process.pid:
        kill_group(process.pid)
    if watcher is not None:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    try:
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        if process.pid:
            kill_group(process.pid, signal.SIGKILL)
        await process.wait()
    output = "".join(parts).strip()
    if timed_out:
        text = "命令超时" if not output else f"命令超时\n{output}"
        return {"text": text, "is_error": True}
    if stopped:
        text = "已停止" if not output else f"已停止\n{output}"
        return {"text": text, "is_error": True}
    code = process.returncode if process.returncode is not None else 1
    body = f"退出码 {code}" if not output else f"退出码 {code}\n{output}"
    return {"text": body, "is_error": code != 0}
