"""
物联知图平台 —— 应用入口
启动顺序：建表 → 灌演示数据 → 绑定事件循环 → 启动仿真网关 → 挂载静态前端。
"""
import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import db
import seed as seed_mod
import service_device
from api import router
from config import FRONTEND_DIR
from hub import hub


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化，关闭时无需特殊清理（守护线程随进程退出）。"""
    db.init_db()
    seed_mod.seed()
    hub.bind_loop(asyncio.get_running_loop())
    # 仿真网关仅在演示模式启动；接入真实 MQTT 时注释掉本行即可
    service_device.start_simulator(interval=3.0)
    yield


app = FastAPI(title="物联知图平台 IoT Knowledge Graph Platform", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def index():
    """返回前端首页。"""
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/health")
def health():
    """健康检查，便于部署与验收时快速判断服务是否活着。"""
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
