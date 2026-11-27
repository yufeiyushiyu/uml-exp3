"""
物联知图平台 —— 知识图谱本体定义
对应类图 cd01（Ontology / EntityType / RelationType）。
本体是"图谱的元模型"：它规定允许出现哪些实体类型、哪些关系类型，
以及类型之间是否允许相连，从源头抑制脏数据。
"""

# 实体类型表：code -> 展示属性
ENTITY_TYPES = {
    "LOCATION": {"label": "位置", "color": "#8E9AAF", "shape": "box", "props": ["name", "level", "parentId"]},
    "SITE": {"label": "园区", "color": "#6C7A89", "shape": "box", "props": ["name", "level"]},
    "BUILDING": {"label": "楼栋", "color": "#7B8FA1", "shape": "box", "props": ["name", "level"]},
    "FLOOR": {"label": "楼层", "color": "#93A5B7", "shape": "box", "props": ["name", "level"]},
    "ROOM": {"label": "房间", "color": "#A9B8C7", "shape": "box", "props": ["name", "level"]},
    "GATEWAY": {"label": "网关", "color": "#E4A11B", "shape": "hexagon", "props": ["protocol", "state", "capacity"]},
    "SENSOR": {"label": "传感器", "color": "#2E86AB", "shape": "ellipse", "props": ["model", "status", "samplingPeriod"]},
    "ACTUATOR": {"label": "执行器", "color": "#6A994E", "shape": "ellipse", "props": ["model", "status", "supportedActions"]},
    "USER": {"label": "用户", "color": "#BC4B51", "shape": "diamond", "props": ["name", "role"]},
    "ALARM": {"label": "告警", "color": "#C1121F", "shape": "triangle", "props": ["level", "state", "metricKey"]},
}

# 关系类型表：code -> (源类型集合, 目标类型集合, 是否有效)
# 与类图 cd01 的 RelationType.isValidPair 一致，用于写入边时的一致性校验
LOCATION_TYPES = {"LOCATION", "SITE", "BUILDING", "FLOOR", "ROOM"}
DEVICE_TYPES = {"SENSOR", "ACTUATOR"}

RELATION_TYPES = {
    # 位置包含下级位置或设备：园区→楼栋→楼层→房间；房间→设备
    "locatedIn": {"label": "位于", "src": LOCATION_TYPES | DEVICE_TYPES, "dst": LOCATION_TYPES, "directed": True},
    # 网关连接设备
    "connectedTo": {"label": "连接", "src": DEVICE_TYPES, "dst": {"GATEWAY"}, "directed": True},
    # 网关承载位置下的设备（房间包含网关）
    "contains": {"label": "包含", "src": LOCATION_TYPES, "dst": LOCATION_TYPES | DEVICE_TYPES | {"GATEWAY"}, "directed": True},
    # 用户控制设备（权限范围在图谱上的显式表达）
    "controls": {"label": "控制", "src": {"USER"}, "dst": DEVICE_TYPES, "directed": True},
    # 设备产生告警
    "raises": {"label": "产生告警", "src": DEVICE_TYPES, "dst": {"ALARM"}, "directed": True},
    # 设备属于某个网关（与 connectedTo 同源不同视角，用于统计聚合）
    "belongsTo": {"label": "属于", "src": DEVICE_TYPES | {"GATEWAY"}, "dst": {"GATEWAY"}, "directed": True},
}


def is_valid_edge(rtype: str, src_type: str, dst_type: str) -> bool:
    """校验一条边是否符合本体约束（防止出现"房间位于设备"这类非法关系）。"""
    spec = RELATION_TYPES.get(rtype)
    if not spec:
        return False
    return src_type in spec["src"] and dst_type in spec["dst"]


def color_of(etype: str) -> str:
    """取实体类型的展示颜色，未知类型统一用灰色。"""
    return ENTITY_TYPES.get(etype, {}).get("color", "#999999")


# 各角色在"操控"这一动作上允许的设备类型，体现最小权限原则
ROLE_ACTION_POLICY = {
    "ADMIN": {"can_control": True, "read_only": False},
    "OPS": {"can_control": True, "read_only": False},
    "VIEWER": {"can_control": False, "read_only": True},
}

# 每条动作的风险等级（1 低、2 中、3 高），高风险动作要求 ADMIN 角色
ACTION_RISK_LEVEL = {
    "setTargetTemp": 2,
    "setBrightness": 1,
    "switchOn": 1,
    "switchOff": 2,
    "openValve": 3,
    "lockDoor": 3,
}
