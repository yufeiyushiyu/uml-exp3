"""
物联知图平台 —— 演示数据初始化
构造一个可讲完整故事的小型现场：
园区A → A栋 → 3F → 301/302 教室；
2 台网关；5 台设备（3 传感器 + 2 执行器）；
1 条联动规则（高温自动开空调）；温度告警规则。
所有数据都通过 GraphService / DeviceRegistry 写入，
即"演示数据也走业务接口"，避免直接 INSERT 造成图谱与档案不一致。
"""
from datetime import datetime

import db
import service_auth
from service_device import DeviceRegistry
from service_graph import GraphService


def is_seeded() -> bool:
    """以位置节点是否存在作为初始化判据，保证可重复启动。"""
    return bool(db.query_one("SELECT node_id FROM nodes WHERE node_id='R-301'"))


def seed() -> None:
    if is_seeded():
        return
    db.init_db()
    service_auth.seed_users()
    gs = GraphService()
    reg = DeviceRegistry()

    # ---------- 位置树 ----------
    locations = [
        ("SITE-A", "园区A", "SITE", {"name": "园区A", "level": 0}),
        ("B-A", "A栋", "BUILDING", {"name": "A栋教学楼", "level": 1}),
        ("F-A3", "A栋3F", "FLOOR", {"name": "A栋3层", "level": 2}),
        ("R-301", "301教室", "ROOM", {"name": "301教室", "level": 3}),
        ("R-302", "302教室", "ROOM", {"name": "302教室", "level": 3}),
    ]
    for node_id, label, etype, props in locations:
        gs.add_node(node_id=node_id, label=label, etype=etype, props=props,
                    state="", loc=node_id)
    # 位置层级：用 locatedIn 边表达父子关系，与对象图 obj01 一致
    gs.repo.upsert_edge("locatedIn", "B-A", "SITE-A")
    gs.repo.upsert_edge("locatedIn", "F-A3", "B-A")
    gs.repo.upsert_edge("locatedIn", "R-301", "F-A3")
    gs.repo.upsert_edge("locatedIn", "R-302", "F-A3")
    gs.repo.upsert_edge("contains", "F-A3", "B-A")

    # ---------- 网关 ----------
    for gw_id, name, protocol, state, cap in [
        ("GW-01", "301网关", "MQTT", "CONNECTED", 32),
        ("GW-02", "302网关", "MQTT", "CONNECTED", 32),
    ]:
        gs.add_node(node_id=gw_id, label=name, etype="GATEWAY",
                    props={"protocol": protocol, "state": state, "capacity": cap},
                    state=state, loc="R-301" if gw_id == "GW-01" else "R-302")
    gs.repo.upsert_edge("locatedIn", "GW-01", "R-301")
    gs.repo.upsert_edge("locatedIn", "GW-02", "R-302")

    # ---------- 设备 ----------
    sensor_metrics = [
        {"key": "temperature", "name": "温度", "unit": "℃", "min": 15, "max": 40},
        {"key": "humidity", "name": "湿度", "unit": "%", "min": 10, "max": 95},
    ]
    co2_metrics = [{"key": "co2", "name": "二氧化碳", "unit": "ppm", "min": 300, "max": 2000}]

    # 传感器：登记后状态为 REGISTERED，首帧上报后转 ONLINE
    reg.register({"deviceId": "TH-301", "name": "301温度湿度传感器", "model": "SHT-20",
                  "kind": "sensor", "locationId": "R-301", "gatewayId": "GW-01",
                  "samplingPeriod": 3, "metrics": sensor_metrics})
    reg.register({"deviceId": "CO2-301", "name": "301二氧化碳传感器", "model": "MH-Z19",
                  "kind": "sensor", "locationId": "R-301", "gatewayId": "GW-01",
                  "samplingPeriod": 5, "metrics": co2_metrics})
    reg.register({"deviceId": "TH-302", "name": "302温度传感器", "model": "SHT-20",
                  "kind": "sensor", "locationId": "R-302", "gatewayId": "GW-02",
                  "samplingPeriod": 5, "metrics": sensor_metrics[:1]})

    # 执行器：动作白名单 + 参数模式 + 执行效果键
    reg.register({"deviceId": "AC-301", "name": "301空调", "model": "GREE-KFR35",
                  "kind": "actuator", "locationId": "R-301", "gatewayId": "GW-01",
                  "actions": [
                      {"actionId": "setTargetTemp", "name": "设定目标温度",
                       "risk": 2, "effects": ["targetTemp"],
                       "params": {"value": {"type": "number", "min": 16, "max": 30}}},
                      {"actionId": "switchOn", "name": "开机", "risk": 1, "effects": ["power"], "params": {}},
                      {"actionId": "switchOff", "name": "关机", "risk": 2, "effects": ["power"], "params": {}},
                  ]})
    reg.register({"deviceId": "LIGHT-301", "name": "301智能灯", "model": "Philips-HUE",
                  "kind": "actuator", "locationId": "R-301", "gatewayId": "GW-01",
                  "actions": [
                      {"actionId": "setBrightness", "name": "调节亮度",
                       "risk": 1, "effects": ["brightness"],
                       "params": {"value": {"type": "number", "min": 0, "max": 100}}},
                      {"actionId": "switchOn", "name": "开灯", "risk": 1, "effects": ["power"], "params": {}},
                  ]})
    reg.register({"deviceId": "VALVE-302", "name": "302水管阀门", "model": "DN25",
                  "kind": "actuator", "locationId": "R-302", "gatewayId": "GW-02",
                  "actions": [
                      {"actionId": "openValve", "name": "开阀", "risk": 3,
                       "effects": ["valveOpen"], "params": {}},
                  ]})

    # 执行器登记后默认在线（真实系统中应由网关心跳驱动）
    for dev_id in ("AC-301", "LIGHT-301", "VALVE-302"):
        reg.mark_status(dev_id, "ONLINE")

    # ---------- 用户节点与"控制"关系 ----------
    gs.add_node(node_id="U-OPS", label="运维工程师", etype="USER",
                props={"name": "运维工程师", "role": "OPS"}, loc="R-301")
    for dev in ("AC-301", "LIGHT-301", "VALVE-302"):
        gs.repo.upsert_edge("controls", "U-OPS", dev)

    # ---------- 规则 ----------
    db.execute("""INSERT OR REPLACE INTO alarm_rules(rule_id, device_id, metric_key, op,
                 threshold, level, enabled) VALUES(?,?,?,?,?,?,1)""",
               ("AR-01", "TH-301", "temperature", ">", 30.0, "CRITICAL"))
    db.execute("""INSERT OR REPLACE INTO alarm_rules(rule_id, device_id, metric_key, op,
                 threshold, level, enabled) VALUES(?,?,?,?,?,?,1)""",
               ("AR-02", "TH-301", "humidity", ">", 80.0, "WARN"))
    db.execute("""INSERT OR REPLACE INTO alarm_rules(rule_id, device_id, metric_key, op,
                 threshold, level, enabled) VALUES(?,?,?,?,?,?,1)""",
               ("AR-03", "CO2-301", "co2", ">", 1500.0, "WARN"))
    db.execute("""INSERT OR REPLACE INTO link_rules(rule_id, name, cond_device, cond_metric,
                 cond_op, cond_threshold, target_device, target_action, target_params, enabled)
                 VALUES(?,?,?,?,?,?,?,?,?,1)""",
               ("LR-01", "高温自动制冷", "TH-301", "temperature", ">", 28.0,
                "AC-301", "setTargetTemp", db.dumps({"value": 24})))

    # 初始读数：让首屏就有内容，不至于全是空值
    now = datetime.now().isoformat(timespec="seconds")
    for dev_id, key, val in [("TH-301", "temperature", 24.5), ("TH-301", "humidity", 52.0),
                             ("CO2-301", "co2", 640.0), ("TH-302", "temperature", 25.1)]:
        db.execute("INSERT INTO observations(device_id, metric_key, value, ts, quality) VALUES(?,?,?,?,0)",
                   (dev_id, key, val, now))
        registry_status = "ONLINE"
        db.execute("UPDATE devices SET status=? WHERE device_id=?", (registry_status, dev_id))
        gs.merge_props(dev_id, {key: val, "status": registry_status})
