"""
物联知图平台 —— 操控服务与规则引擎
对应类图 cd03：Command / CommandExecutor / PermissionChecker / GatewayClient / Rule / RuleEngine。
这是本系统的核心用例"通过图谱下发动作"的实现，采用**命令模式**：
把"一次下发"封装成对象，使校验、下发、回执、重试、撤销、审计
围绕同一对象推进，状态流转严格按状态图 sm03 执行。
"""
import uuid
from datetime import datetime

import db
import ontology
from config import ACK_TIMEOUT_MS, MAX_RETRY
from hub import hub
from service_auth import ROLE_PERMISSIONS, record_audit, get_session
from service_graph import GraphService
from service_device import DeviceRegistry, IngestService


class CommandError(Exception):
    """指令校验阶段失败，携带业务码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CommandExecutor:
    def __init__(self) -> None:
        self.registry = DeviceRegistry()
        self.graph = GraphService()

    # ---------- 阶段一：创建与校验 ----------
    def submit(self, token: str, device_id: str, action_id: str, params: dict) -> dict:
        """创建指令并完成四项校验，通过后停在"待撤销"状态等待用户确认。"""
        session = get_session(token)
        device = self.registry.find(device_id)
        if not device:
            raise CommandError("DEVICE_NOT_FOUND", "设备不存在")

        # 校验 1：动作必须在设备动作白名单内（防止越权动作）
        allowed_actions = {a["actionId"]: a for a in device["actions"]}
        if action_id not in allowed_actions:
            raise CommandError("ACTION_NOT_ALLOWED", "该设备不支持此动作")

        # 校验 2：角色是否具备操控权限
        if "command:confirm" not in ROLE_PERMISSIONS.get(session["role"], set()):
            record_audit(session["user_id"], "COMMAND_REJECT", f"{device_id}/{action_id}", "角色无操控权限")
            raise CommandError("NO_PERMISSION", "当前角色无操控权限")

        # 校验 3：数据范围（位置前缀）
        if not self._in_scope(session, device["location_id"]):
            record_audit(session["user_id"], "COMMAND_REJECT", f"{device_id}/{action_id}", "超出管理范围")
            raise CommandError("OUT_OF_SCOPE", "目标设备不在你的管理范围内")

        # 校验 4：参数模式与区间
        spec = allowed_actions[action_id]
        self._validate_params(spec, params)

        cmd_id = f"CMD-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now().isoformat(timespec="seconds")
        db.execute(
            """INSERT INTO commands(command_id, device_id, action_id, params, state,
               issued_by, issued_at, retry_count, message) VALUES(?,?,?,?,?,?,?,0,'')""",
            (cmd_id, device_id, action_id, db.dumps(params), "PENDING_CONFIRM",
             session["user_id"], now),
        )
        record_audit(session["user_id"], "COMMAND_SUBMIT", cmd_id, "待确认")
        return {"commandId": cmd_id, "state": "PENDING_CONFIRM",
                "message": "指令已通过校验，请确认下发"}

    # ---------- 阶段二：确认与下发 ----------
    def confirm(self, token: str, command_id: str) -> dict:
        """确认下发：投递到网关、等待回执、重试，最终落到成功或失败。"""
        session = get_session(token)
        cmd = db.query_one("SELECT * FROM commands WHERE command_id=?", (command_id,))
        if not cmd:
            raise CommandError("COMMAND_NOT_FOUND", "指令不存在")
        if cmd["state"] != "PENDING_CONFIRM":
            raise CommandError("ILLEGAL_STATE", f"当前状态 {cmd['state']} 不可下发")

        device = self.registry.find(cmd["device_id"])
        # 状态：下发中
        self._set_state(command_id, "SENDING")
        # 网关可用性：离线设备直接进入失败分支（模拟超时）
        if device["status"] in ("OFFLINE", "FAULT"):
            self._set_state(command_id, "FAILED", "设备离线，下发失败")
            record_audit(session["user_id"], "COMMAND_FAILED", command_id, "设备离线")
            return self._result(command_id, False, "DEVICE_OFFLINE", "设备离线，指令未送达")

        # 状态：等待回执（原型中用同步模拟代替真实 MQTT 往返）
        self._set_state(command_id, "WAIT_ACK")
        ack = self._simulate_ack(device, cmd["action_id"], db.loads(cmd["params"], {}))

        if not ack["success"] and ack.get("retryable"):
            # 超时重试分支：最多 MAX_RETRY 次
            for i in range(MAX_RETRY):
                db.execute("UPDATE commands SET retry_count=? WHERE command_id=?",
                           (i + 1, command_id))
                ack = self._simulate_ack(device, cmd["action_id"], db.loads(cmd["params"], {}))
                if ack["success"]:
                    break

        if ack["success"]:
            self._set_state(command_id, "SUCCEEDED")
            # 执行成功后刷新图谱节点属性并广播增量，前端立即看到变化
            self.graph.merge_props(cmd["device_id"], ack["effect"])
            hub.broadcast_threadsafe("graph.delta", {
                "nodeId": cmd["device_id"], "props": ack["effect"],
                "version": self.graph.repo.get_node(cmd["device_id"])["version"]})
            record_audit(session["user_id"], "COMMAND_SUCCESS", command_id, db.dumps(ack["effect"]))
            return self._result(command_id, True, "OK", "执行成功", ack["effect"])

        self._set_state(command_id, "FAILED", ack.get("message", "执行失败"))
        record_audit(session["user_id"], "COMMAND_FAILED", command_id, ack.get("message", ""))
        return self._result(command_id, False, "EXEC_FAILED", ack.get("message", "设备未响应"))

    def revoke(self, token: str, command_id: str) -> dict:
        """撤销：仅允许在 PENDING_CONFIRM 阶段撤销，体现"可撤销命令对象"。"""
        cmd = db.query_one("SELECT * FROM commands WHERE command_id=?", (command_id,))
        if not cmd:
            raise CommandError("COMMAND_NOT_FOUND", "指令不存在")
        if cmd["state"] != "PENDING_CONFIRM":
            raise CommandError("ILLEGAL_STATE", "指令已下发，不可撤销")
        self._set_state(command_id, "REVOKED")
        record_audit("-", "COMMAND_REVOKE", command_id, "用户撤销")
        return {"commandId": command_id, "state": "REVOKED"}

    # ---------- 内部辅助 ----------
    def _in_scope(self, session: dict, location_id: str) -> bool:
        scope = session.get("scope") or ""
        return scope == "" or location_id.startswith(scope)

    def _validate_params(self, spec: dict, params: dict) -> None:
        """按动作的 paramsSchema 校验必填项与数值区间。"""
        for key, rule in (spec.get("params") or {}).items():
            if key not in params:
                raise CommandError("PARAM_MISSING", f"缺少参数：{key}")
            if rule.get("type") == "number":
                try:
                    v = float(params[key])
                except (TypeError, ValueError):
                    raise CommandError("PARAM_TYPE", f"参数 {key} 必须为数值")
                if "min" in rule and v < rule["min"]:
                    raise CommandError("PARAM_RANGE", f"参数 {key} 小于下限 {rule['min']}")
                if "max" in rule and v > rule["max"]:
                    raise CommandError("PARAM_RANGE", f"参数 {key} 超过上限 {rule['max']}")

    def _simulate_ack(self, device: dict, action_id: str, params: dict) -> dict:
        """模拟网关回执：按动作语义返回"执行后的属性变化"。
        真实实现应替换为 MQTT 请求/应答，此处保持接口形状一致。"""
        if action_id == "setTargetTemp":
            return {"success": True, "effect": {"targetTemp": params.get("value")}, "retryable": False}
        if action_id == "setBrightness":
            return {"success": True, "effect": {"brightness": params.get("value")}, "retryable": False}
        if action_id in ("switchOn", "switchOff"):
            return {"success": True, "effect": {"power": action_id == "switchOn"}, "retryable": False}
        if action_id == "openValve":
            return {"success": True, "effect": {"valveOpen": True}, "retryable": False}
        return {"success": False, "retryable": True, "message": "网关未回执（模拟超时）"}

    def _set_state(self, command_id: str, state: str, message: str = "") -> None:
        db.execute("UPDATE commands SET state=?, message=? WHERE command_id=?",
                   (state, message, command_id))

    def _result(self, command_id: str, success: bool, code: str, message: str,
                effect: dict | None = None) -> dict:
        return {"commandId": command_id, "success": success, "code": code,
                "message": message, "effect": effect or {},
                "state": db.query_one("SELECT state FROM commands WHERE command_id=?",
                                      (command_id,))["state"]}


# 联动触发冷却：同一规则在 RULE_COOLDOWN_SEC 秒内只触发一次，
# 否则"高温持续 10 分钟"会生成几十条等价指令——这是实现时真实踩到的坑。
RULE_COOLDOWN_SEC = 120
_LAST_FIRED: dict[str, datetime] = {}


class RuleEngine:
    """联动规则引擎：读数命中条件即自动生成指令，无需人工确认。
    规则示例：301 教室温度 > 28℃ → 自动把空调设为目标 24℃。
    自动指令以"系统"身份下发，同样走命令对象与审计，只是跳过人工确认环节。"""

    def __init__(self) -> None:
        self.executor = CommandExecutor()

    def evaluate(self, device_id: str, metric_key: str, value: float) -> list[dict]:
        """对每条启用的联动规则做条件匹配，命中则生成并执行指令。"""
        fired = []
        for r in db.query_all("SELECT * FROM link_rules WHERE enabled=1 AND cond_device=? AND cond_metric=?",
                              (device_id, metric_key)):
            if not _compare(r["cond_op"], value, r["cond_threshold"]):
                continue
            target = self.executor.registry.find(r["target_device"])
            if not target or target["status"] != "ONLINE":
                continue  # 目标设备不可用则跳过，避免无效指令刷屏
            params = db.loads(r["target_params"], {})
            # 冷却判断：同规则短时间内的重复触发直接忽略
            last = _LAST_FIRED.get(r["rule_id"])
            if last and (datetime.now() - last).total_seconds() < RULE_COOLDOWN_SEC:
                continue
            # 幂等判断：目标属性已等于目标值则无需再下发
            node = self.executor.graph.repo.get_node(r["target_device"])
            if node and all(node["props"].get(k) == v for k, v in params.items()):
                continue
            _LAST_FIRED[r["rule_id"]] = datetime.now()
            cmd_id = f"CMD-{uuid.uuid4().hex[:8].upper()}"
            db.execute(
                """INSERT INTO commands(command_id, device_id, action_id, params, state,
                   issued_by, issued_at, retry_count, message) VALUES(?,?,?,?,?,?,?,0,'')""",
                (cmd_id, r["target_device"], r["target_action"], db.dumps(params),
                 "SENDING", "SYSTEM", datetime.now().isoformat(timespec="seconds")))
            self.executor._set_state(cmd_id, "WAIT_ACK")
            ack = self.executor._simulate_ack(target, r["target_action"], params)
            if ack["success"]:
                self.executor._set_state(cmd_id, "SUCCEEDED")
                self.executor.graph.merge_props(r["target_device"], ack["effect"])
                hub.broadcast_threadsafe("graph.delta", {
                    "nodeId": r["target_device"], "props": ack["effect"],
                    "version": self.executor.graph.repo.get_node(r["target_device"])["version"]})
                record_audit("SYSTEM", "RULE_TRIGGER", r["rule_id"], f"{r['name']} → {cmd_id}")
            else:
                self.executor._set_state(cmd_id, "FAILED", ack.get("message", "联动执行失败"))
            fired.append({"ruleId": r["rule_id"], "name": r["name"], "commandId": cmd_id,
                          "success": ack["success"]})
        return fired


def _compare(op: str, value: float, threshold: float) -> bool:
    """规则条件比较，与告警规则保持同一套运算语义。"""
    return {
        ">": value > threshold, ">=": value >= threshold,
        "<": value < threshold, "<=": value <= threshold,
    }.get(op, False)


def list_commands() -> list[dict]:
    """查询指令执行记录，供前端"指令跟踪"面板使用。"""
    rows = db.query_all("SELECT * FROM commands ORDER BY issued_at DESC LIMIT 100")
    for r in rows:
        r["params"] = db.loads(r["params"], {})
    return rows
