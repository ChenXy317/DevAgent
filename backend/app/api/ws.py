from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.agent.loop import get_loop

router = APIRouter()


@router.websocket("/ws")
async def socket(ws: WebSocket, session_id: str = "") -> None:
    await ws.accept()
    if not session_id:
        await ws.send_json({"type": "error", "message": "会话不存在"})
        await ws.close()
        return
    loop = get_loop(session_id)
    hello = await loop.hello()
    if hello is None:
        await ws.send_json({"type": "error", "message": "会话不存在"})
        await ws.close()
        return
    loop.sockets.add(ws)
    await ws.send_json(hello)
    try:
        while True:
            try:
                data = await ws.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                await ws.send_json({"type": "error", "message": "消息无法解析"})
                continue
            if not isinstance(data, dict):
                await ws.send_json({"type": "error", "message": "消息无法解析"})
                continue
            kind = data.get("type")
            if kind == "user_message":
                await loop.on_user_message(str(data.get("text") or ""))
            elif kind == "stop":
                await loop.on_stop()
            elif kind == "compress":
                await loop.on_compress()
            elif kind == "approval.response":
                await loop.on_approval(str(data.get("decision") or ""))
            else:
                await ws.send_json({"type": "error", "message": "未知事件"})
    finally:
        loop.sockets.discard(ws)
