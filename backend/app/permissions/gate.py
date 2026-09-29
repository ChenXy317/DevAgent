"""进程内的一次审批等待。"""

import asyncio
from typing import Any


class ApprovalGate:
    def __init__(self) -> None:
        self.future: asyncio.Future[str] | None = None
        self.request: dict[str, Any] | None = None

    async def wait(self, request: dict[str, Any], timeout: float = 120) -> str:
        loop = asyncio.get_running_loop()
        self.future = loop.create_future()
        self.request = request
        try:
            return await asyncio.wait_for(self.future, timeout)
        except TimeoutError:
            return "timeout"
        finally:
            self.future = None
            self.request = None

    def resolve(self, decision: str) -> bool:
        if self.future is None or self.future.done():
            return False
        self.future.set_result(decision)
        return True

    def cancel_as_reject(self) -> None:
        self.resolve("reject")
