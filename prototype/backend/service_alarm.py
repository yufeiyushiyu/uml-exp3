"""
物联知图平台 —— 告警服务
对应类图 cd04：AlarmRule / Alarm / AlarmService / Notifier。
关键业务规则：
1. 阈值规则按"设备 + 指标"匹配，支持 > >= < <= 四种比较；
2. 同一(节点, 指标, 等级)在抑制窗口内只推送一次，避免告警风暴；
3. CRITICAL 告警会在图谱中生成一个 ALARM 节点并以"产生告警"边相连，
   使告警在图上可追溯。
"""
import uuid
from datetime import datetime, timedelta

import db
from config import ALARM_SUPPRESS_SEC
from hub import hub
from service_auth import record_audit

# 抑制窗口缓存：key=(node_id, metric_key, level)，value=上次推送时间
_LAST_PUSH: dict[tuple, datetime] = {}


def _compare(op: str, value: float, threshold: float) -> bool:
    """执行比较运算，未知运算符按不命中处理。"""
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
    }.get(op, False)


def on_observation(device_id: str, metric_key: str, value: float, ts: str) -> list[dict]:
    """每收到一条读数就评估一次规则，返回本次新产生的告警列表。"""
    rules = db.query_all(
        "SELECT * FROM alarm_rules WHERE enabled=1 AND device_id=? AND metric_key=?",
        (device_id, metric_key))
    raised = []
    for r in rules:
        if not _compare(r["op"], value, r["threshold"]):
            continue
        key = (device_id, metric_key, r["level"])
        now = datetime.now()
        last = _LAST_PUSH.get(key)
        # 抑制窗口内：只累加计数，不重复推送
        if last and (now - last).total_seconds() < ALARM_SUPPRESS_SEC:
            continue
        _LAST_PUSH[key] = now
        raised.append(_raise(device_id, metric_key, r["level"], value, ts))
    return raised


def _raise(device_id: str, metric_key: str, level: str, value: float, ts: str) -> dict:
    """创建告警实体，推送通知，并在图谱中建立告警节点。"""
    alarm_id = f"AL-{uuid.uuid4().hex[:8]}"
    now = datetime.now().isoformat(timespec="seconds")
    db.execute(
        """INSERT INTO alarms(alarm_id, node_id, metric_key, level, state, triggered_at, value)
           VALUES(?,?,?,?,?,?,?)""",
        (alarm_id, device_id, metric_key, level, "RAISED", now, value),
    )
    # CRITICAL 才上图，避免 INFO 级刷屏把图谱撑大
    if level == "CRITICAL":
        from service_graph import GraphService
        gs = GraphService()
        node = gs.repo.get_node(device_id)
        alarm_node = f"ALN-{alarm_id}"
        gs.add_node(node_id=alarm_node, label=f"{metric_key} 越限告警", etype="ALARM",
                    props={"level": level, "state": "RAISED", "metricKey": metric_key, "value": value},
                    state="RAISED", loc=node["loc"] if node else "")
        gs.repo.upsert_edge("raises", device_id, alarm_node)

    alarm = {
        "alarmId": alarm_id, "nodeId": device_id, "metricKey": metric_key,
        "level": level, "state": "RAISED", "triggeredAt": now, "value": value,
    }
    record_audit("-", "ALARM_RAISE", alarm_id, f"{device_id}/{metric_key}={value}")
    hub.broadcast_threadsafe("alarm.new", alarm)
    return alarm


def list_alarms(state: str = "") -> list[dict]:
    """查询告警列表，可按状态过滤。"""
    if state:
        return db.query_all("SELECT * FROM alarms WHERE state=? ORDER BY triggered_at DESC", (state,))
    return db.query_all("SELECT * FROM alarms ORDER BY triggered_at DESC LIMIT 200")


def ack_alarm(alarm_id: str, user_id: str) -> dict:
    """确认告警：状态置为 ACKNOWLEDGED 并记录确认人。"""
    alarm = db.query_one("SELECT * FROM alarms WHERE alarm_id=?", (alarm_id,))
    if not alarm:
        raise KeyError("告警不存在")
    db.execute("UPDATE alarms SET state='ACKNOWLEDGED', acked_by=? WHERE alarm_id=?",
               (user_id, alarm_id))
    record_audit(user_id, "ALARM_ACK", alarm_id, "成功")
    return {**alarm, "state": "ACKNOWLEDGED", "acked_by": user_id}


def close_alarm(alarm_id: str, resolution: str, user_id: str) -> dict:
    """关闭告警：填写处置结论，保留完整处理链路。"""
    db.execute("UPDATE alarms SET state='CLOSED', resolution=? WHERE alarm_id=?",
               (resolution, alarm_id))
    record_audit(user_id, "ALARM_CLOSE", alarm_id, resolution)
    return db.query_one("SELECT * FROM alarms WHERE alarm_id=?", (alarm_id,))
