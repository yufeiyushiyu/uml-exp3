"""
物联知图平台 —— 配置项
集中存放路径、端口、超时等可调参数，避免散落在各模块中。
"""
from pathlib import Path

# 项目根目录（prototype/）
BASE_DIR = Path(__file__).resolve().parent.parent

# SQLite 数据库文件：图谱库与时序库合库存放，便于单机演示
DB_PATH = BASE_DIR / "data" / "iotkg.db"

# 前端静态资源目录
FRONTEND_DIR = BASE_DIR / "frontend"

# 图谱默认渲染的节点上限（超过后前端改为分区加载）
GRAPH_NODE_LIMIT = 300

# 指令等待回执的超时时间与最大重试次数（与状态图中 sm03 一致）
ACK_TIMEOUT_MS = 3000
MAX_RETRY = 2

# 设备心跳超时（秒），超时判定离线（与状态图 sm01 一致）
HEARTBEAT_TIMEOUT_SEC = 90

# 告警抑制窗口（秒）：同一(节点,指标,等级)在窗口内只推送一次
ALARM_SUPPRESS_SEC = 300

# 会话有效期（秒）
SESSION_TTL_SEC = 8 * 3600

# 演示用管理员与运维账号（真实系统应由初始化脚本写入，此处便于验收）
DEMO_USERS = [
    {"account": "admin", "password": "admin123", "name": "系统管理员", "role": "ADMIN", "scope": ""},
    {"account": "ops", "password": "ops123", "name": "运维工程师", "role": "OPS", "scope": "R-301"},
    {"account": "viewer", "password": "view123", "name": "值班员", "role": "VIEWER", "scope": ""},
]
