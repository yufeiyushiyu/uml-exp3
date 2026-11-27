"""
物联知图平台 —— 数据访问层
负责建库建表、连接管理与通用查询封装。
设计要点：
1. 图谱实体（nodes/edges）与时序读数（observations）分表存放，
   与部署图 dp01 中"kg 与 iot 分库"的思想一致，此处用同一文件的两个表组模拟；
2. 所有写操作使用同一个事务，保证"读数入库 + 节点属性更新"的原子性。
"""
import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

from config import DB_PATH

# 建表脚本：字段与类图 cd01/cd02/cd03/cd04/cd05 中的属性一一对应
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS nodes(
        node_id TEXT PRIMARY KEY,      -- 图谱节点标识，与领域对象 ID 一致
        label   TEXT NOT NULL,         -- 展示名称
        etype   TEXT NOT NULL,         -- 实体类型，取值见 ontology.ENTITY_TYPES
        props   TEXT NOT NULL DEFAULT '{}',  -- 属性袋（JSON），模式由本体校验
        version INTEGER NOT NULL DEFAULT 1,  -- 版本号，供增量推送去重
        state   TEXT NOT NULL DEFAULT '',    -- 运行态（在线/离线/故障）
        loc     TEXT NOT NULL DEFAULT ''     -- 所属位置，用于按位置子树筛选
    )""",
    """CREATE TABLE IF NOT EXISTS edges(
        edge_id TEXT PRIMARY KEY,
        rtype   TEXT NOT NULL,         -- 关系类型，取值见 ontology.RELATION_TYPES
        src     TEXT NOT NULL,         -- 源节点
        dst     TEXT NOT NULL,         -- 目标节点
        weight  REAL NOT NULL DEFAULT 1.0
    )""",
    """CREATE TABLE IF NOT EXISTS devices(
        device_id TEXT PRIMARY KEY,
        name      TEXT NOT NULL,
        model     TEXT NOT NULL,
        dclass    TEXT NOT NULL,       -- SENSOR / ACTUATOR
        gateway_id TEXT NOT NULL,
        location_id TEXT NOT NULL,
        status    TEXT NOT NULL DEFAULT 'REGISTERED',
        metrics   TEXT NOT NULL DEFAULT '[]',  -- 指标定义（JSON 数组）
        actions   TEXT NOT NULL DEFAULT '[]'   -- 动作白名单（JSON 数组）
    )""",
    """CREATE TABLE IF NOT EXISTS observations(
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        metric_key TEXT NOT NULL,
        value     REAL NOT NULL,
        ts        TEXT NOT NULL,
        quality   INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS commands(
        command_id TEXT PRIMARY KEY,
        device_id  TEXT NOT NULL,
        action_id  TEXT NOT NULL,
        params     TEXT NOT NULL DEFAULT '{}',
        state      TEXT NOT NULL,
        issued_by  TEXT NOT NULL,
        issued_at  TEXT NOT NULL,
        retry_count INTEGER NOT NULL DEFAULT 0,
        message    TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS alarm_rules(
        rule_id   TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        metric_key TEXT NOT NULL,
        op        TEXT NOT NULL,       -- > >= < <=
        threshold REAL NOT NULL,
        level     TEXT NOT NULL,       -- INFO / WARN / CRITICAL
        enabled   INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS alarms(
        alarm_id    TEXT PRIMARY KEY,
        node_id     TEXT NOT NULL,
        metric_key  TEXT NOT NULL,
        level       TEXT NOT NULL,
        state       TEXT NOT NULL,
        triggered_at TEXT NOT NULL,
        value       REAL NOT NULL DEFAULT 0,
        acked_by    TEXT NOT NULL DEFAULT '',
        resolution  TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS link_rules(
        rule_id       TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        cond_device   TEXT NOT NULL,   -- 条件设备
        cond_metric   TEXT NOT NULL,   -- 条件指标
        cond_op       TEXT NOT NULL,   -- > >= < <=
        cond_threshold REAL NOT NULL,  -- 阈值
        target_device TEXT NOT NULL,   -- 被联动设备
        target_action TEXT NOT NULL,   -- 联动动作
        target_params TEXT NOT NULL DEFAULT '{}',  -- 动作参数（JSON）
        enabled       INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS users(
        user_id  TEXT PRIMARY KEY,
        account  TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,        -- 演示用明文；生产应存 pbkdf2/bcrypt 哈希
        name     TEXT NOT NULL,
        role     TEXT NOT NULL,
        scope    TEXT NOT NULL DEFAULT ''  -- 权限位置前缀，空表示全站
    )""",
    """CREATE TABLE IF NOT EXISTS audit(
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        op      TEXT NOT NULL,
        target  TEXT NOT NULL,
        result  TEXT NOT NULL,
        ts      TEXT NOT NULL
    )""",
    # 常用查询的索引：按设备+指标+时间查历史，是读多写少的典型场景
    "CREATE INDEX IF NOT EXISTS idx_obs ON observations(device_id, metric_key, ts)",
    "CREATE INDEX IF NOT EXISTS idx_node_loc ON nodes(loc)",
]


def init_db() -> None:
    """初始化数据库结构（幂等，可重复调用）。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.commit()


@contextmanager
def connect():
    """获取一个自动提交/回滚的连接上下文。"""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def query_all(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    """执行查询并返回字典列表，屏蔽 sqlite3.Row 的差异。"""
    with connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def query_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    """执行查询并返回单行。"""
    rows = query_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    """执行写操作并提交，返回受影响行数。"""
    with connect() as conn:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur.rowcount


def loads(text: str, default: Any) -> Any:
    """容错的 JSON 反序列化：字段为空时返回默认值。"""
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def dumps(obj: Any) -> str:
    """序列化为 JSON，保留中文可读性（便于直接查看数据库）。"""
    return json.dumps(obj, ensure_ascii=False)
