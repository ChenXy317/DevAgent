"""在会话目录里执行命令。"""

import asyncio
import codecs
import contextlib
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path


SHELL_TIMEOUT_SECONDS = 300
_OUTPUT_LIMIT = 32000
_HIDDEN = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "MYSQL_")


class Utf8Decoder:
    """按块解码。一块的末尾可能切在多字节字符中间。"""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes, *, final: bool = False) -> str:
        return self._decoder.decode(data, final)


class _OutputCapture:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.total = 0
        self.truncated = False

    def add(self, text: str) -> str:
        if not text or self.truncated:
            return ""
        room = _OUTPUT_LIMIT - self.total
        if room <= 0:
            self.truncated = True
            marker = "\n...[输出已截断]"
            self.parts.append(marker)
            return marker
        if len(text) <= room:
            self.parts.append(text)
            self.total += len(text)
            return text
        kept = text[:room]
        marker = "\n...[输出已截断]"
        self.parts.append(kept)
        self.parts.append(marker)
        self.total += room
        self.truncated = True
        return kept + marker


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
    capture = _OutputCapture()

    async def pump(stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        decoder = Utf8Decoder()
        while True:
            data = await stream.read(4096)
            if not data:
                shown = capture.add(decoder.feed(b"", final=True))
                if shown and on_output is not None:
                    await on_output(shown)
                return
            if capture.truncated:
                continue
            shown = capture.add(decoder.feed(data))
            if shown and on_output is not None:
                await on_output(shown)

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
    output = "".join(capture.parts).strip()
    if timed_out:
        text = "命令超时" if not output else f"命令超时\n{output}"
        return {"text": text, "is_error": True}
    if stopped:
        text = "已停止" if not output else f"已停止\n{output}"
        return {"text": text, "is_error": True}
    code = process.returncode if process.returncode is not None else 1
    body = f"退出码 {code}" if not output else f"退出码 {code}\n{output}"
    return {"text": body, "is_error": code != 0}
