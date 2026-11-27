"""
物联知图平台 —— 接口层（API）
对应组件图 cmp01：一个 FastAPI 进程内按模块划分控制器。
路由只做三件事：取参、调服务、转错误；不含业务判断。
"""
import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import db
import service_alarm
import service_auth
import service_control
import service_device
from hub import hub
from service_graph import GraphService

router = APIRouter(prefix="/api")
_graph = GraphService()
_executor = service_control.CommandExecutor()


# ---------------- 鉴权依赖 ----------------
def current_user(authorization: str = Header(default="")) -> dict:
    """从 Authorization: Bearer <token> 中解析会话。"""
    token = authorization.replace("Bearer ", "").strip()
    try:
        return {**service_auth.get_session(token), "token": token}
    except service_auth.AuthError as e:
        raise HTTPException(status_code=401, detail={"code": e.code, "message": e.message})


def require_perm(permission: str):
    """生成一个"要求某权限点"的依赖，避免每个路由重复写判断。"""
    def _checker(user: dict = Depends(current_user)) -> dict:
        try:
            service_auth.require(user["token"], permission)
        except service_auth.AuthError as e:
            raise HTTPException(status_code=403, detail={"code": e.code, "message": e.message})
        return user
    return _checker


# ---------------- 请求模型 ----------------
class LoginReq(BaseModel):
    account: str
    password: str


class CommandReq(BaseModel):
    deviceId: str
    actionId: str
    params: dict = Field(default_factory=dict)


class DeviceReq(BaseModel):
    name: str
    model: str = "GENERIC"
    kind: str = "sensor"          # sensor / actuator
    locationId: str
    gatewayId: str
    metrics: list[dict] = []
    actions: list[dict] = []
    samplingPeriod: int = 5


class AckReq(BaseModel):
    resolution: str = ""


class TelemetryReq(BaseModel):
    deviceId: str
    gatewayId: str
    ts: str = ""
    values: dict[str, float]


# ---------------- 认证 ----------------
@router.post("/auth/login")
def login(req: LoginReq):
    try:
        return service_auth.login(req.account, req.password)
    except service_auth.AuthError as e:
        raise HTTPException(status_code=401, detail={"code": e.code, "message": e.message})


@router.get("/auth/me")
def me(user: dict = Depends(current_user)):
    return {k: user[k] for k in ("user_id", "account", "name", "role", "scope")}


# ---------------- 图谱 ----------------
@router.get("/graph")
def get_graph(entityTypes: str = "", locationId: str = "", keyword: str = "",
              relationTypes: str = "", limit: int = 300,
              user: dict = Depends(require_perm("graph:read"))):
    """加载子图：类型与关系均以逗号分隔的多值参数传入。"""
    def split(s: str) -> list[str]:
        return [x for x in s.split(",") if x]
    return _graph.query(split(entityTypes), locationId, keyword, split(relationTypes), limit)


@router.get("/graph/node/{node_id}")
def node_detail(node_id: str, user: dict = Depends(require_perm("graph:read"))):
    detail = _graph.node_detail(node_id)
    if not detail:
        raise HTTPException(status_code=404, detail="节点不存在")
    return detail


@router.get("/locations")
def locations(user: dict = Depends(require_perm("graph:read"))):
    """位置树，供筛选下拉框使用。"""
    rows = db.query_all("SELECT node_id, label, etype FROM nodes WHERE etype IN "
                        "('SITE','BUILDING','FLOOR','ROOM') ORDER BY node_id")
    return rows


# ---------------- 设备 ----------------
@router.get("/devices")
def list_devices(user: dict = Depends(require_perm("graph:read"))):
    return service_device.DeviceRegistry().list_all()


@router.post("/devices")
def create_device(req: DeviceReq, user: dict = Depends(require_perm("device:write"))):
    """登记新设备；位置不存在时返回 422，与顺序图 sd06 一致。"""
    try:
        return service_device.DeviceRegistry().register(req.model_dump())
    except ValueError as e:
        code = str(e)
        raise HTTPException(status_code=422, detail={"code": code, "message": "位置或网关不存在"})


# ---------------- 遥测接入 ----------------
@router.post("/ingest/telemetry")
def ingest(req: TelemetryReq):
    """网关上报入口；生产环境由 MQTT 订阅回调调用，此处开放 HTTP 便于演示。"""
    return service_device.IngestService().ingest(req.model_dump())


# ---------------- 指令 ----------------
@router.post("/commands")
def submit_command(req: CommandReq, user: dict = Depends(require_perm("graph:read"))):
    try:
        return _executor.submit(user["token"], req.deviceId, req.actionId, req.params)
    except service_control.CommandError as e:
        status = 403 if e.code in ("NO_PERMISSION", "OUT_OF_SCOPE", "ACTION_NOT_ALLOWED") else 400
        raise HTTPException(status_code=status, detail={"code": e.code, "message": e.message})


@router.post("/commands/{command_id}/confirm")
def confirm_command(command_id: str, user: dict = Depends(require_perm("command:confirm"))):
    try:
        return _executor.confirm(user["token"], command_id)
    except service_control.CommandError as e:
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})


@router.post("/commands/{command_id}/revoke")
def revoke_command(command_id: str, user: dict = Depends(require_perm("command:confirm"))):
    try:
        return _executor.revoke(user["token"], command_id)
    except service_control.CommandError as e:
        raise HTTPException(status_code=400, detail={"code": e.code, "message": e.message})


@router.get("/commands")
def commands(user: dict = Depends(require_perm("graph:read"))):
    return service_control.list_commands()


# ---------------- 告警 ----------------
@router.get("/alarms")
def alarms(state: str = "", user: dict = Depends(require_perm("graph:read"))):
    return service_alarm.list_alarms(state)


@router.post("/alarms/{alarm_id}/ack")
def ack_alarm(alarm_id: str, user: dict = Depends(require_perm("alarm:ack"))):
    try:
        return service_alarm.ack_alarm(alarm_id, user["user_id"])
    except KeyError:
        raise HTTPException(status_code=404, detail="告警不存在")


@router.post("/alarms/{alarm_id}/close")
def close_alarm(alarm_id: str, req: AckReq, user: dict = Depends(require_perm("alarm:ack"))):
    return service_alarm.close_alarm(alarm_id, req.resolution, user["user_id"])


# ---------------- 历史数据 ----------------
@router.get("/history")
def history(deviceId: str, metric: str, frm: str = "", to: str = "",
            agg: int = 0, user: dict = Depends(require_perm("graph:read"))):
    """按时间范围查询读数；agg>0 时按分钟聚合，用于大范围降采样。"""
    sql = "SELECT ts, value FROM observations WHERE device_id=? AND metric_key=?"
    params: list[Any] = [deviceId, metric]
    if frm:
        sql += " AND ts>=?"
        params.append(frm)
    if to:
        sql += " AND ts<=?"
        params.append(to)
    sql += " ORDER BY ts"
    points = db.query_all(sql, params)
    if agg > 0 and points:
        # 按 agg 分钟分桶求均值，减小前端渲染压力
        buckets: dict[str, list[float]] = {}
        for p in points:
            t = datetime.fromisoformat(p["ts"])
            key = t.strftime(f"%Y-%m-%d %H:{t.minute // agg * agg:02d}")
            buckets.setdefault(key, []).append(p["value"])
        points = [{"ts": k, "value": round(sum(v) / len(v), 2)} for k, v in buckets.items()]
    return {"deviceId": deviceId, "metric": metric, "points": points}


@router.get("/audit")
def audit(user: dict = Depends(require_perm("audit:read"))):
    return service_auth.list_audit()


# ---------------- WebSocket ----------------
@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await hub.connect(ws)
    try:
        while True:
            # 保持连接：接收心跳文本，超时由客户端重连
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(ws)
