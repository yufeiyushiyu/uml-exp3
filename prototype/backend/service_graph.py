"""
物联知图平台 —— 图谱服务（应用服务层）
对应类图 cd01 的 KnowledgeGraph / GraphService：
把仓储返回的行数据组装为前端可直接渲染的视图模型（GraphNode/GraphEdge）。
"""
import ontology
import repo_graph
from config import GRAPH_NODE_LIMIT


class GraphService:
    """图谱查询与增量更新的统一入口。"""

    def __init__(self) -> None:
        self.repo = repo_graph.SqliteGraphRepository()

    # 把数据库行转成 vis-network 需要的节点结构
    def to_view(self, subgraph: dict) -> dict:
        """视图转换：补上颜色、形状、标签等纯展示属性。
        这一步刻意放在服务层而非前端，保证"同一份图在不同客户端表现一致"。"""
        nodes = []
        for n in subgraph["nodes"]:
            spec = ontology.ENTITY_TYPES.get(n["etype"], {})
            props = n.get("props", {}) or {}
            nodes.append({
                "id": n["node_id"],
                "label": n["label"],
                "group": n["etype"],
                "color": spec.get("color", "#999999"),
                "shape": spec.get("shape", "dot"),
                "state": n.get("state", ""),
                "version": n.get("version", 1),
                "props": props,
                "title": _tooltip(n, props),
            })
        edges = [{
            "id": e["edge_id"],
            "from": e["src"],
            "to": e["dst"],
            "label": ontology.RELATION_TYPES.get(e["rtype"], {}).get("label", e["rtype"]),
            "rtype": e["rtype"],
        } for e in subgraph["edges"]]
        return {"nodes": nodes, "edges": edges, "truncated": subgraph.get("truncated", False)}

    def query(self, entity_types=None, location_id="", keyword="", relation_types=None,
              limit=GRAPH_NODE_LIMIT) -> dict:
        """按类型、位置、关键词加载子图。"""
        sub = self.repo.load_subgraph(entity_types, location_id, keyword, relation_types, limit)
        return self.to_view(sub)

    def node_detail(self, node_id: str) -> dict | None:
        """节点详情：本体属性 + 实时读数 + 可用动作 + 邻接关系。"""
        node = self.repo.get_node(node_id)
        if not node:
            return None
        neighbors = self.repo.neighbors(node_id, 1)
        return {
            "node": node,
            "neighbors": self.to_view(neighbors),
            "device": _device_of(node_id),
        }

    def merge_props(self, node_id: str, values: dict) -> int:
        """按属性模式合并遥测值并自增版本号，返回新版本号。
        只允许写入本体声明过的属性，防止设备上报字段污染图谱。"""
        node = self.repo.get_node(node_id)
        if not node:
            return 0
        # 允许写入的属性 = 本体声明属性 ∪ 该设备声明的指标键 ∪ 其动作的效果键
        allowed = set(ontology.ENTITY_TYPES.get(node["etype"], {}).get("props", []))
        import db as _db
        dev = _db.query_one("SELECT metrics, actions FROM devices WHERE device_id=?", (node_id,))
        if dev:
            allowed |= {m["key"] for m in _db.loads(dev["metrics"], [])}
            allowed |= {e for a in _db.loads(dev["actions"], []) for e in a.get("effects", [])}
        merged = dict(node["props"])
        for k, v in values.items():
            if k in allowed or not allowed:
                merged[k] = v
        # 状态既写属性也写节点 state 字段，保证图谱着色与详情徽标一致
        new_state = merged.get("status", node.get("state", ""))
        self.repo.upsert_node(node_id, node["label"], node["etype"], merged,
                              state=new_state, loc=node.get("loc", ""))
        return self.repo.get_node(node_id)["version"]

    def add_node(self, **kwargs) -> None:
        """新增节点（供设备注册流程调用）。"""
        self.repo.upsert_node(**kwargs)


def _tooltip(node: dict, props: dict) -> str:
    """生成鼠标悬停提示，避免前端拼接字符串。"""
    parts = [f"{node['label']}（{node['etype']}）"]
    for k, v in props.items():
        parts.append(f"{k}: {v}")
    if node.get("state"):
        parts.append(f"状态: {node['state']}")
    return "\n".join(parts)


def _device_of(node_id: str) -> dict | None:
    """设备节点附带其档案信息（动作白名单来自 devices 表）。"""
    import db
    row = db.query_one("SELECT * FROM devices WHERE device_id=?", (node_id,))
    if not row:
        return None
    row["metrics"] = db.loads(row["metrics"], [])
    row["actions"] = db.loads(row["actions"], [])
    return row
