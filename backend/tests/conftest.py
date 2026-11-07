import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
os.environ["MYSQL_DATABASE"] = "coding_agent_test"
os.environ["WORKDIR"] = str(ROOT / ".test-workspace")
os.environ["PROJECTS_ROOT"] = str(ROOT / ".test-projects")

from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response

from app.agent.executor import set_transport as set_model_transport
from app.db import clear_rows, dispose_db, get_session_factory, init_db
from app.main import app
from app.models import LlmConfigRecord
from app.tools import search as search_tools


async def reset_llm() -> None:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.get(LlmConfigRecord, 1)
        assert row is not None
        row.base_url = ""
        row.model = ""
        row.api_key = ""
        row.context_turns = 20
        row.context_limit = 100_000
        await db.commit()


@pytest.fixture(autouse=True)
def block_external_network():
    def deny(_request: Request) -> Response:
        raise AssertionError("测试不应访问网络")

    blocked = MockTransport(deny)
    set_model_transport(blocked)
    search_tools.set_transport(blocked)
    yield
    set_model_transport(None)
    search_tools.set_transport(None)
    search_tools.set_now(lambda: datetime.now(timezone.utc))


@pytest.fixture
async def client():
    try:
        await init_db()
        await clear_rows()
        await reset_llm()
        Path(os.environ["WORKDIR"]).mkdir(parents=True, exist_ok=True)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
    finally:
        await dispose_db()
