# 物联知图（IoTKG）原型 · 运行说明

课程：面向对象分析与设计（实验三 · 选题 A：知识图谱物联网设备管理平台）
技术栈：FastAPI + SQLite + vis-network，零构建、可离线运行。

## 一、环境要求

| 项目 | 要求 |
| --- | --- |
| Python | 3.10 以上（推荐 3.11） |
| 依赖包 | fastapi、uvicorn、pydantic |
| 浏览器 | Chrome / Edge 90 以上 |
| 网络 | 单机运行无需外网；前端库已本地化 |

安装依赖：

```bash
cd prototype/backend
pip install fastapi uvicorn pydantic
```

## 二、启动

```bash
cd prototype/backend
python3 -m uvicorn main:app --host 127.0.0.1 --port 8011
```

启动后服务会：
1. 在 `prototype/data/iotkg.db` 建表并灌入演示数据（已存在则复用）；
2. 启动仿真网关，每 3 秒上报一次遥测；
3. 在 `http://127.0.0.1:8011/docs` 暴露 OpenAPI 文档，可直接调接口验证。

打开界面：浏览器直接打开 `prototype/frontend/index.html`。

## 三、演示账号

| 账号 | 密码 | 角色 | 数据范围 |
| --- | --- | --- | --- |
| admin | admin123 | 系统管理员（ADMIN） | 全域 |
| ops | ops123 | 运维工程师（OPS） | R-301（单房间） |
| viewer | view123 | 值班员（VIEWER） | 只读 |

## 四、五分钟验收路径

1. **看图**：打开首页即见 15 个节点、21 条边；位置呈层级、设备挂网关、告警为红色三角。
2. **查详情**：点“301空调”，右侧出现状态徽标、属性、动作白名单（设定温度 L2 / 开机 L1 / 关机 L2）。
3. **下发**：以 `ops` 登录，选“设定目标温度”填 24 → 提交（待确认）→ 确认 → 3 秒内出现“执行成功”，节点属性更新为 24。
4. **验拦截**：换 `viewer` 提交同一动作 → 403 无权限；用 `ops` 操控 302 的阀门 → 超范围；参数填 50 → 越上限。
5. **看同步**：`curl -X POST /api/ingest/telemetry` 上报 31.5℃，图上节点变红、告警面板出现 CRITICAL，并自动触发联动下发 24℃（`issued_by=SYSTEM`）。
6. **查留痕**：指令列表可看终态与重试次数；审计接口可见 LOGIN / COMMAND_REJECT / COMMAND_SUCCESS / ALARM_RAISE / RULE_TRIGGER。

## 五、目录说明

```
prototype/
├── backend/    11 个模块：接口、图谱、设备、操控、告警、权限、仓储、本体
├── frontend/   3 个文件：结构、交互、样式
└── data/       运行期数据库（删除后重启即恢复初始状态）
```

模块职责与关键算法见《大作业_系统设计报告》第三章；模型源文件见上级目录 `puml_source/`。

## 六、常见问题

| 现象 | 处理 |
| --- | --- |
| 页面显示“未连接” | 确认服务监听 8011 且端口未被占用（`ss -lptn 'sport = :8011'`） |
| 图谱为空 | 调 `/api/graph` 看节点数；为空说明演示数据未灌入，删除 db 后重启 |
| 指令停在“待确认” | 仿真网关未启动，检查服务日志 |
| 想恢复初始数据 | 删除 `data/iotkg.db` 后重启 |

## 七、已知限制

密码为演示便利存明文（生产须哈希）；消息通道为 HTTP 仿真（可替换 MQTT）；单机部署无 TLS。
完整清单见《系统设计报告》5.3 节。
