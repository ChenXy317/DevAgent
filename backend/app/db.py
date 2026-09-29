from urllib.parse import quote_plus

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import (
    AllowRuleRecord,
    Base,
    LlmConfigRecord,
    MessageRecord,
    SearchCacheRecord,
    SessionRecord,
    ToolLogRecord,
    utcnow,
)

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def database_url() -> str:
    settings = get_settings()
    user = quote_plus(settings.mysql_user)
    password = quote_plus(settings.mysql_password)
    return (
        f"mysql+aiomysql://{user}:{password}"
        f"@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}"
        "?charset=utf8mb4"
    )


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(database_url(), pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def init_db() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_llm_config()


async def ensure_llm_config() -> None:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(LlmConfigRecord, 1)
        if row is None:
            db.add(
                LlmConfigRecord(
                    id=1,
                    base_url=settings.llm_base_url,
                    model=settings.llm_model,
                    api_key=settings.llm_api_key,
                    context_turns=20,
                    updated_at=utcnow(),
                )
            )
            await db.commit()


async def clear_rows() -> None:
    factory = get_session_factory()
    async with factory() as db:
        await db.execute(update(SessionRecord).values(compacted_before_id=None))
        await db.flush()
        await db.execute(delete(ToolLogRecord))
        await db.execute(delete(MessageRecord))
        await db.execute(delete(SessionRecord))
        await db.execute(delete(AllowRuleRecord))
        await db.execute(delete(SearchCacheRecord))
        await db.commit()


async def dispose_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
