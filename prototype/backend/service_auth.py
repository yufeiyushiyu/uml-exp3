"""
物联知图平台 —— 认证、鉴权与审计
对应类图 cd05：User / Role / Permission / AuthService / AuditLog。
设计要点：
1. 令牌存内存字典（原型阶段），生产应放 Redis 并支持续期；
2. 鉴权分两级：功能级（读/写）与数据级（位置范围 scope）；
3. 所有敏感操作（登录、下发指令、告警处置、权限变更）强制写审计。
"""
import uuid
from datetime import datetime, timedelta

import db
from config import DEMO_USERS, SESSION_TTL_SEC


# 角色 → 功能权限集合，体现"最小权限"
ROLE_PERMISSIONS = {
    "ADMIN": {"graph:read", "graph:write", "device:write", "command:confirm", "alarm:ack", "audit:read"},
    "OPS": {"graph:read", "command:confirm", "alarm:ack"},
    "VIEWER": {"graph:read"},
}


class AuthError(Exception):
    """认证/鉴权失败的统一异常，由路由层转成 401/403。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# 内存会话表：token -> 会话信息
_SESSIONS: dict[str, dict] = {}


def seed_users() -> None:
    """写入演示账号（幂等）。"""
    for u in DEMO_USERS:
        exists = db.query_one("SELECT user_id FROM users WHERE account=?", (u["account"],))
        if exists:
            continue
        db.execute(
            "INSERT INTO users(user_id, account, password, name, role, scope) VALUES(?,?,?,?,?,?)",
            (f"U-{uuid.uuid4().hex[:6]}", u["account"], u["password"], u["name"], u["role"], u["scope"]),
        )


def login(account: str, password: str, client_ip: str = "") -> dict:
    """账号密码登录，成功后生成会话令牌。"""
    user = db.query_one("SELECT * FROM users WHERE account=?", (account,))
    if not user or user["password"] != password:
        record_audit(user["user_id"] if user else "-", "LOGIN", account, "失败")
        raise AuthError("BAD_CREDENTIALS", "账号或密码错误")
    token = uuid.uuid4().hex
    expires = datetime.now() + timedelta(seconds=SESSION_TTL_SEC)
    _SESSIONS[token] = {
        "user_id": user["user_id"], "account": user["account"], "name": user["name"],
        "role": user["role"], "scope": user["scope"], "expires": expires, "ip": client_ip,
    }
    record_audit(user["user_id"], "LOGIN", account, "成功")
    return {"token": token, "user": {k: _SESSIONS[token][k] for k in ("user_id", "account", "name", "role", "scope")},
            "expiresAt": expires.isoformat()}


def get_session(token: str) -> dict:
    """取会话并校验有效期。"""
    s = _SESSIONS.get(token or "")
    if not s:
        raise AuthError("NO_SESSION", "未登录或会话已失效")
    if s["expires"] < datetime.now():
        _SESSIONS.pop(token, None)
        raise AuthError("SESSION_EXPIRED", "会话已过期，请重新登录")
    return s


def require(token: str, permission: str) -> dict:
    """功能级鉴权：请求的权限点是否在角色权限集合中。"""
    s = get_session(token)
    if permission not in ROLE_PERMISSIONS.get(s["role"], set()):
        record_audit(s["user_id"], "AUTHZ", permission, "拒绝")
        raise AuthError("NO_PERMISSION", f"缺少权限：{permission}")
    return s


def in_scope(session: dict, location_id: str) -> bool:
    """数据级鉴权：设备位置是否落在用户的可管范围前缀内。"""
    scope = session.get("scope") or ""
    return scope == "" or location_id.startswith(scope)


def record_audit(user_id: str, op: str, target: str, result: str) -> None:
    """写审计日志；审计本身不允许失败影响主流程。"""
    try:
        db.execute("INSERT INTO audit(user_id, op, target, result, ts) VALUES(?,?,?,?,?)",
                   (user_id, op, target, result, datetime.now().isoformat(timespec="seconds")))
    except Exception:
        pass


def list_audit(limit: int = 50) -> list[dict]:
    """查询最近审计记录，仅 ADMIN 可调用。"""
    return db.query_all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
