"""
物联知图平台 —— WebSocket 增量推送中心
对应组件图 cmp01 中的 WebSocketHub。
作用：把"图谱变更/告警产生"两类事件只推给已连接客户端，
避免前端轮询整张图。线程安全的实现是难点：仿真线程不是协程，
因此用 run_coroutine_threadsafe 投递到事件循环。
"""
import asyncio
import json
from typing import Any

from fastapi import WebSocket


class WebSocketHub:
    def __init__(self) -> None:
        self._clients: list[WebSocket] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """在应用启动时绑定主事件循环，供后台线程使用。"""
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, event: str, payload: dict) -> None:
        """向所有在线客户端广播一条事件；逐个发送并剔除失效连接。"""
        message = json.dumps({"event": event, "data": payload}, ensure_ascii=False)
        dead = []
        for ws in self._clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def broadcast_threadsafe(self, event: str, payload: dict) -> None:
        """给后台仿真线程用的入口：把协程调度回主循环。"""
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(event, payload), self._loop)


hub = WebSocketHub()
