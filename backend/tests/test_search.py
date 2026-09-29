from datetime import datetime, timedelta, timezone

import httpx

from app.config import get_settings
from app.tools import search as search_tools


LLM = {
    "grounding": {
        "generic": [
            {
                "title": "示例",
                "url": "https://example.com/a",
                "snippets": ["<b>片段</b>"],
            },
            {"title": "坏链接", "url": "javascript:alert(1)", "snippets": ["不要"]},
        ]
    }
}
WEB = {
    "web": {
        "results": [
            {"title": "网页", "url": "https://example.com/b", "description": "说明"},
        ]
    }
}


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 29, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current


def install(handler) -> list[str]:
    calls: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return handler(request)

    search_tools.set_transport(httpx.MockTransport(wrapped))
    return calls


async def test_llm_context_success_and_cache(client):
    del client
    settings = get_settings()
    settings.search_api_key = "test-key"
    clock = Clock()
    search_tools.set_now(clock)
    calls = install(lambda _request: httpx.Response(200, json=LLM))

    first = await search_tools.search_web("青岛博物馆", "all", "zh-Hans")
    assert first["ok"] is True
    assert first["results"] == [
        {"title": "示例", "url": "https://example.com/a", "snippets": ["片段"]}
    ]
    assert len(calls) == 1
    assert "llm/context" in calls[0]

    second = await search_tools.search_web("青岛博物馆", "ALL", "zh-hans")
    assert second == first
    assert len(calls) == 1

    clock.current += timedelta(hours=25)
    await search_tools.search_web("青岛博物馆", "ALL", "zh-hans")
    assert len(calls) == 2


async def test_plan_error_falls_back_and_auth_errors_do_not(client):
    del client
    get_settings().search_api_key = "test-key"

    denied = install(lambda _request: httpx.Response(401, json={"message": "no"}))
    unauthorized = await search_tools.search_web("甲", "ALL", "zh-hans")
    assert unauthorized["error"] == "搜索未授权"
    assert len(denied) == 1
    assert "llm/context" in denied[0]

    limited = install(lambda _request: httpx.Response(429, text="rate limit"))
    frequent = await search_tools.search_web("乙", "ALL", "zh-hans")
    assert frequent["error"] == "搜索请求过于频繁"
    assert len(limited) == 1
    assert "web/search" not in limited[0]

    def fallback(request: httpx.Request) -> httpx.Response:
        if "llm/context" in str(request.url):
            return httpx.Response(403, text="subscription does not include llm context")
        return httpx.Response(200, json=WEB)

    calls = install(fallback)
    result = await search_tools.search_web("套餐", "ALL", "zh-hans")
    assert result["ok"] is True
    assert result["results"][0]["title"] == "网页"
    assert "llm/context" in calls[0]
    assert "web/search" in calls[1]


async def test_empty_result_missing_key_and_overlong_query_do_not_wander(client):
    del client
    settings = get_settings()
    settings.search_api_key = "test-key"
    calls = install(lambda _request: httpx.Response(200, json={"grounding": {"generic": []}}))
    empty = await search_tools.search_web("空", "ALL", "zh-hans")
    assert empty == {"ok": False, "error": "搜索无结果"}
    assert len(calls) == 1

    settings.search_api_key = ""
    missing = await search_tools.search_web("丁", "ALL", "zh-hans")
    assert missing["error"] == "未配置 SEARCH_API_KEY"
    assert len(calls) == 1

    settings.search_api_key = "test-key"
    long_query = await search_tools.search_web("x" * 601, "ALL", "zh-hans")
    assert long_query["error"] == "查询过长"
    assert await search_tools.search_web("查询", "CHN", "zh-hans") == {
        "ok": False,
        "error": "国家代码不合法",
    }
    assert len(calls) == 1
