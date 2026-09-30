"""对模型地址做短请求，以及流式读取对话。"""

import asyncio
import contextlib
import json

import httpx


class ModelError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class RunStopped(Exception):
    pass


_transport: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


async def probe_model(base_url: str, model: str, api_key: str) -> str | None:
    if not base_url.strip() or not model.strip():
        return "请先填写模型"
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {
        "model": model,
        "stream": False,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    client_kwargs: dict = {"timeout": 20}
    if _transport is not None:
        client_kwargs["transport"] = _transport
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        return "连接超时"
    except httpx.HTTPError as exc:
        return f"连接失败：{exc.__class__.__name__}"
    if response.status_code >= 400:
        detail = response.text.strip().replace("\n", " ")[:200]
        if detail:
            return f"接口返回 {response.status_code}：{detail}"
        return f"接口返回 {response.status_code}"
    return None


async def _next_line(iterator) -> str | None:
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return None


async def stream_chat(
    *,
    base_url: str,
    model: str,
    api_key: str,
    messages: list[dict],
    tools: list | None,
    on_delta,
    stop=None,
) -> None:
    if not base_url.strip() or not model.strip():
        raise ModelError("请先在设置里填写模型")
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict = {"model": model, "stream": True, "messages": messages}
    if tools is not None:
        payload["tools"] = tools
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    client_kwargs: dict = {"timeout": httpx.Timeout(300.0, connect=20.0)}
    if _transport is not None:
        client_kwargs["transport"] = _transport
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace").strip().replace("\n", " ")[:200]
                    if body:
                        raise ModelError(f"接口返回 {response.status_code}：{body}")
                    raise ModelError(f"接口返回 {response.status_code}")
                lines = response.aiter_lines()
                while True:
                    if stop is not None and stop.is_set():
                        raise RunStopped()
                    read_task = asyncio.create_task(_next_line(lines))
                    stop_task = asyncio.create_task(stop.wait()) if stop is not None else None
                    waited = {read_task}
                    if stop_task is not None:
                        waited.add(stop_task)
                    done, pending = await asyncio.wait(waited, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    for task in pending:
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                    if stop is not None and stop.is_set():
                        if read_task not in done:
                            raise RunStopped()
                    if read_task not in done:
                        raise RunStopped()
                    line = read_task.result()
                    if line is None:
                        break
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    choices = event.get("choices") or []
                    if not choices:
                        error = event.get("error")
                        if error:
                            message = error.get("message") if isinstance(error, dict) else str(error)
                            raise ModelError(message or "模型请求失败")
                        continue
                    delta = choices[0].get("delta") or {}
                    if isinstance(delta, dict):
                        await on_delta(delta)
    except (ModelError, RunStopped):
        raise
    except httpx.TimeoutException as exc:
        raise ModelError("模型请求超时") from exc
    except httpx.HTTPError as exc:
        raise ModelError(f"模型请求失败：{exc.__class__.__name__}") from exc
