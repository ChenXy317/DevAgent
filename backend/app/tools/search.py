"""向 Brave 要标题、链接和片段。"""

import hashlib
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from app.config import get_settings
from app.db import get_session_factory
from app.models import SearchCacheRecord

log = logging.getLogger("app.tools.search")

BRAVE_HOST = "api.search.brave.com"
USER_AGENT = "CodingAgent/0.1 (single-user local demo)"
LLM_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"
WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
LLM_CONTEXT_CAPS = {
    "count": 5,
    "maximum_number_of_urls": 5,
    "maximum_number_of_tokens": 2048,
    "maximum_number_of_snippets_per_url": 3,
    "context_threshold_mode": "balanced",
}
SEARCH_TTL = 24 * 3600
_TAG = re.compile(r"<[^>]+>")
_LANG = re.compile(r"^[a-z]{2}(-[a-z]{2,8})?$")
_transport: httpx.AsyncBaseTransport | None = None


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


_now = _default_now


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


def set_now(fn) -> None:
    global _now
    _now = fn


def cache_key(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _naive(value: datetime) -> datetime:
    return _aware(value).replace(tzinfo=None)


def clean_snippet(text: str) -> str:
    return html.unescape(_TAG.sub("", text or "")).strip()[:400]


def http_url(url: object) -> bool:
    if not isinstance(url, str) or not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _title_or_host(title: object, url: str) -> str:
    if isinstance(title, str) and title.strip():
        return title.strip()
    return urlparse(url).hostname or url


def _collect(rows: object) -> list[dict]:
    results = []
    if not isinstance(rows, list):
        return results
    for item in rows:
        if not isinstance(item, dict) or not http_url(item.get("url")):
            continue
        url = str(item["url"])
        raw = item.get("snippets") or []
        if isinstance(raw, str):
            raw = [raw]
        snippets = []
        if isinstance(raw, list):
            for part in raw:
                if len(snippets) >= 3:
                    break
                cleaned = clean_snippet(str(part))
                if cleaned:
                    snippets.append(cleaned)
        if not snippets:
            continue
        results.append({"title": _title_or_host(item.get("title"), url), "url": url, "snippets": snippets})
        if len(results) >= 5:
            break
    return results


def normalize_llm_context(payload: dict) -> list[dict]:
    generic = ((payload.get("grounding") or {}).get("generic") or []) if isinstance(payload, dict) else []
    return _collect(generic)


def normalize_web_search(payload: dict) -> list[dict]:
    rows = ((payload.get("web") or {}).get("results") or []) if isinstance(payload, dict) else []
    results = []
    if not isinstance(rows, list):
        return results
    for item in rows:
        if not isinstance(item, dict) or not http_url(item.get("url")):
            continue
        url = str(item["url"])
        snippet = clean_snippet(str(item.get("description") or ""))
        if not snippet:
            continue
        results.append({"title": _title_or_host(item.get("title"), url), "url": url, "snippets": [snippet]})
        if len(results) >= 5:
            break
    return results


def _plan_denied(status: int, text: str) -> bool:
    if status == 404:
        return True
    if status not in {200, 402, 403}:
        return False
    lowered = text.lower()
    if any(token in lowered for token in ("rate limit", "too many", "过于频繁")):
        return False
    return any(token in lowered for token in ("plan", "subscription", "not subscribed", "llm context"))


def _flag_key() -> str:
    return cache_key({"op": "brave-no-llm-context"})


def _result_key(query: str, country: str, search_lang: str) -> str:
    return cache_key(
        {"query": query, "country": country, "search_lang": search_lang, "caps": LLM_CONTEXT_CAPS}
    )


def _validate(query: object, country: object, search_lang: object) -> tuple[str, str, str] | dict:
    cleaned = query.strip() if isinstance(query, str) else ""
    if not cleaned:
        return {"ok": False, "error": "查询不能为空"}
    if len(cleaned) > 600 or len(cleaned.split()) > 75:
        return {"ok": False, "error": "查询过长"}
    normalized_country = country.strip().upper() if isinstance(country, str) and country.strip() else "ALL"
    if normalized_country != "ALL" and not re.fullmatch(r"[A-Z]{2}", normalized_country):
        return {"ok": False, "error": "国家代码不合法"}
    normalized_lang = search_lang.strip().lower() if isinstance(search_lang, str) and search_lang.strip() else "zh-hans"
    if not _LANG.fullmatch(normalized_lang):
        return {"ok": False, "error": "语言代码不合法"}
    return cleaned, normalized_country, normalized_lang


async def _read_cache(key: str) -> dict | None:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(SearchCacheRecord, key)
        if row is None:
            return None
        if _now() - _aware(row.fetched_at) > timedelta(seconds=SEARCH_TTL):
            await db.delete(row)
            await db.commit()
            return None
        return json.loads(row.payload_json)


async def _write_cache(key: str, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(SearchCacheRecord, key)
        if row is None:
            db.add(SearchCacheRecord(cache_key=key, payload_json=text, fetched_at=_naive(_now())))
        else:
            row.payload_json = text
            row.fetched_at = _naive(_now())
        await db.commit()


def _json_body(response: httpx.Response) -> dict | None:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _headers(api_key: str) -> dict[str, str]:
    return {
        "X-Subscription-Token": api_key,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


async def _get(client: httpx.AsyncClient, url: str, params: dict, api_key: str) -> httpx.Response:
    return await client.get(url, params=params, headers=_headers(api_key))


def _status_error(status: int) -> dict | None:
    if status == 401:
        return {"ok": False, "error": "搜索未授权"}
    if status == 429:
        return {"ok": False, "error": "搜索请求过于频繁"}
    return None


async def search_web(query: object, country: object = "ALL", search_lang: object = "zh-hans") -> dict:
    checked = _validate(query, country, search_lang)
    if isinstance(checked, dict):
        return checked
    cleaned, normalized_country, normalized_lang = checked
    api_key = get_settings().search_api_key.strip()
    log.info("search_key_set=%s host=%s", "true" if api_key else "false", BRAVE_HOST)
    if not api_key:
        return {"ok": False, "error": "未配置 SEARCH_API_KEY"}
    key = _result_key(cleaned, normalized_country, normalized_lang)
    hit = await _read_cache(key)
    if hit is not None and isinstance(hit.get("results"), list):
        return {"ok": True, "results": hit["results"]}
    skip_primary = await _read_cache(_flag_key()) is not None
    client_kwargs: dict = {"timeout": 15}
    if _transport is not None:
        client_kwargs["transport"] = _transport
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = None
            if not skip_primary:
                response = await _get(
                    client,
                    LLM_CONTEXT_URL,
                    {"q": cleaned, "country": normalized_country, "search_lang": normalized_lang, **LLM_CONTEXT_CAPS},
                    api_key,
                )
                failed = _status_error(response.status_code)
                if failed:
                    return failed
                if _plan_denied(response.status_code, response.text):
                    await _write_cache(_flag_key(), {"endpoint": "web_search"})
                    response = None
                elif response.status_code != 200:
                    return {"ok": False, "error": "搜索服务失败"}
                else:
                    results = normalize_llm_context(_json_body(response) or {})
                    if not results:
                        return {"ok": False, "error": "搜索无结果"}
                    await _write_cache(key, {"results": results})
                    return {"ok": True, "results": results}
            response = await _get(
                client,
                WEB_SEARCH_URL,
                {"q": cleaned, "country": normalized_country, "search_lang": normalized_lang, "count": 5},
                api_key,
            )
    except httpx.TimeoutException:
        return {"ok": False, "error": "搜索超时"}
    except httpx.HTTPError:
        return {"ok": False, "error": "搜索服务失败"}
    failed = _status_error(response.status_code)
    if failed:
        return failed
    if response.status_code != 200:
        return {"ok": False, "error": "搜索服务失败"}
    results = normalize_web_search(_json_body(response) or {})
    if not results:
        return {"ok": False, "error": "搜索无结果"}
    await _write_cache(key, {"results": results})
    return {"ok": True, "results": results}
