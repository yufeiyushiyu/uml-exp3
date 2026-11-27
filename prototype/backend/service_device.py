"""
物联知图平台 —— 设备接入与同步服务
对应类图 cd02：DeviceRegistry / IngestService / TelemetryMessage。
职责：接收遥测报文 → 校验 → 落库 → 合并节点属性 → 推送增量 → 触发告警评估。
另含一个后台仿真线程，用随机游走生成温度/湿度/CO2 读数，
使原型在没有真实网关的条件下也能完整跑通"上报—上图—告警"链路。
"""
import random
import threading
import time
import uuid
from datetime import datetime

import db
import service_alarm
from config import HEARTBEAT_TIMEOUT_SEC
from hub import hub
from service_graph import GraphService


class DeviceRegistry:
    """设备档案与状态登记表。"""

    def register(self, draft: dict) -> dict:
        """登记设备：校验位置存在性 → 匹配类型模板 → 写库 → 建图谱节点与边。"""
        loc = db.query_one("SELECT node_id FROM nodes WHERE node_id=?", (draft["locationId"],))
        if not loc:
            raise ValueError("LOCATION_NOT_FOUND")
        gateway = db.query_one("SELECT node_id FROM nodes WHERE node_id=?", (draft["gatewayId"],))
        if not gateway:
            raise ValueError("GATEWAY_NOT_FOUND")

        device_id = draft.get("deviceId") or f"DV-{uuid.uuid4().hex[:6].upper()}"
        dclass = "SENSOR" if draft.get("kind") == "sensor" else "ACTUATOR"
        db.execute(
            """INSERT OR REPLACE INTO devices(device_id, name, model, dclass, gateway_id,
               location_id, status, metrics, actions) VALUES(?,?,?,?,?,?,?,?,?)""",
            (device_id, draft["name"], draft.get("model", "GENERIC"), dclass,
             draft["gatewayId"], draft["locationId"], "REGISTERED",
             db.dumps(draft.get("metrics", [])), db.dumps(draft.get("actions", []))),
        )
        # 图谱侧同步：设备节点 + 位于 + 连接 两条边，保持"档案与图谱同源"
        gs = GraphService()
        props = {"model": draft.get("model", "GENERIC"), "status": "REGISTERED",
                 "samplingPeriod": draft.get("samplingPeriod", 5)}
        if dclass == "ACTUATOR":
            props["supportedActions"] = [a["actionId"] for a in draft.get("actions", [])]
        gs.add_node(node_id=device_id, label=draft["name"], etype=dclass, props=props,
                    state="REGISTERED", loc=draft["locationId"])
        gs.repo.upsert_edge("locatedIn", device_id, draft["locationId"])
        gs.repo.upsert_edge("connectedTo", device_id, draft["gatewayId"])
        hub.broadcast_threadsafe("graph.node.added", {"nodeId": device_id})
        return {"deviceId": device_id, "accessKey": uuid.uuid4().hex}

    def find(self, device_id: str) -> dict | None:
        row = db.query_one("SELECT * FROM devices WHERE device_id=?", (device_id,))
        if row:
            row["metrics"] = db.loads(row["metrics"], [])
            row["actions"] = db.loads(row["actions"], [])
        return row

    def list_all(self) -> list[dict]:
        rows = db.query_all("SELECT * FROM devices ORDER BY device_id")
        for r in rows:
            r["metrics"] = db.loads(r["metrics"], [])
            r["actions"] = db.loads(r["actions"], [])
        return rows

    def mark_status(self, device_id: str, status: str) -> None:
        """更新设备状态并同步到图谱节点，保证两处状态一致。"""
        db.execute("UPDATE devices SET status=? WHERE device_id=?", (status, device_id))
        GraphService().merge_props(device_id, {"status": status})


class IngestService:
    """遥测接入：解析、校验、落库、上图、告警。"""

    def __init__(self) -> None:
        self.registry = DeviceRegistry()
        self.graph = GraphService()
        self._bad_frames: dict[str, int] = {}

    def ingest(self, msg: dict) -> dict:
        """处理一帧遥测报文。msg 形如
        {"deviceId":"TH-301","gatewayId":"GW-01","ts":"...","values":{"temperature":26.4}}"""
        device = self.registry.find(msg.get("deviceId", ""))
        if not device:
            return {"ok": False, "code": "DEVICE_NOT_FOUND"}

        values = msg.get("values") or {}
        # 字段级校验：只接收该设备指标定义中声明的键，且必须落在量程内
        declared = {m["key"]: m for m in device["metrics"]}
        accepted, rejected = {}, []
        for k, v in values.items():
            if k not in declared:
                rejected.append({"key": k, "reason": "UNDECLARED_METRIC"})
                continue
            spec = declared[k]
            if not (spec.get("min", -1e9) <= float(v) <= spec.get("max", 1e9)):
                rejected.append({"key": k, "reason": "OUT_OF_RANGE"})
                continue
            accepted[k] = float(v)

        if not accepted:
            self._bad_frames[device["device_id"]] = self._bad_frames.get(device["device_id"], 0) + 1
            # 连续 3 帧异常 → 判故障（对应活动图 ad02 的异常分支）
            if self._bad_frames[device["device_id"]] >= 3:
                self.registry.mark_status(device["device_id"], "FAULT")
                service_alarm._raise(device["device_id"], "status", "CRITICAL", 1, msg.get("ts", ""))
            return {"ok": False, "code": "FRAME_REJECTED", "detail": rejected}

        self._bad_frames[device["device_id"]] = 0
        ts = msg.get("ts") or datetime.now().isoformat(timespec="seconds")
        # 1) 时序落库
        with db.connect() as conn:
            for k, v in accepted.items():
                conn.execute(
                    "INSERT INTO observations(device_id, metric_key, value, ts, quality) VALUES(?,?,?,?,0)",
                    (device["device_id"], k, v, ts))
            conn.commit()

        # 2) 图谱属性合并并取回新版本号
        version = self.graph.merge_props(device["device_id"], accepted)
        self.registry.mark_status(device["device_id"], "ONLINE")

        # 3) 推送增量事件（前端按 nodeId 局部重绘）
        hub.broadcast_threadsafe("graph.delta", {
            "nodeId": device["device_id"], "props": accepted, "version": version, "ts": ts})

        # 4) 告警评估（同步执行，保证告警与读数同批可见）
        alarms = []
        for k, v in accepted.items():
            alarms += service_alarm.on_observation(device["device_id"], k, v, ts)

        # 5) 联动规则评估：命中后自动生成指令（规则引擎在告警之后执行，
        #    保证"先告警后处置"的语义顺序与活动图 ad03 一致）
        from service_control import RuleEngine
        fired = []
        for k, v in accepted.items():
            fired += RuleEngine().evaluate(device["device_id"], k, v)
        return {"ok": True, "accepted": accepted, "rejected": rejected,
                "alarms": alarms, "rulesFired": fired}


# ------------------------- 仿真网关 -------------------------
_SIM_STATE: dict[str, float] = {}


def _walk(device_id: str, metric: str, base: float, jitter: float,
          min_v: float, max_v: float) -> float:
    """带边界的随机游走，模拟真实传感器的连续变化，避免数值跳变。"""
    key = f"{device_id}:{metric}"
    cur = _SIM_STATE.get(key, base)
    cur += random.uniform(-jitter, jitter)
    cur = max(min_v, min(max_v, cur))
    _SIM_STATE[key] = cur
    return round(cur, 2)


def start_simulator(interval: float = 3.0) -> threading.Thread:
    """启动后台仿真线程：每 interval 秒为各传感器生成一帧遥测。
    第 8 帧起把 301 教室的温度推到 30℃ 以上，用于演示阈值告警。"""
    ing = IngestService()

    def loop() -> None:
        frame = 0
        while True:
            frame += 1
            for dev_id, metric, base, jitter, lo, hi in [
                ("TH-301", "temperature", 24.0, 0.4, 15, 40),
                ("TH-301", "humidity", 55.0, 1.5, 10, 95),
                ("CO2-301", "co2", 650.0, 40, 300, 2000),
                ("TH-302", "temperature", 25.0, 0.3, 15, 40),
            ]:
                value = _walk(dev_id, metric, base, jitter, lo, hi)
                # 演示告警：第 8 帧注入一次高温
                if frame >= 8 and dev_id == "TH-301" and metric == "temperature":
                    value = 31.5
                ing.ingest({"deviceId": dev_id, "gatewayId": "GW-01",
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "values": {metric: value}})
            time.sleep(interval)

    t = threading.Thread(target=loop, name="telemetry-sim", daemon=True)
    t.start()
    return t


def check_heartbeat() -> None:
    """心跳巡检：预留接口，生产环境由定时任务扫描 last_seen。"""
    return None
