"""
物联知图平台 —— 图谱仓储（GraphRepository 的 SQLite 实现）
对应类图 cd01 中的 SqliteGraphRepository：把领域对象翻译成 SQL，
让上层服务不关心存储形态，将来换 Neo4j 只需替换本文件。
"""
import uuid

import db
import ontology


class SqliteGraphRepository:
    """基于 SQLite 的图谱读写实现。"""

    # ---------- 写入 ----------
    def upsert_node(self, node_id: str, label: str, etype: str, props: dict,
                    state: str = "", loc: str = "") -> None:
        """插入或更新节点；已存在时保留原 version 并自增，供增量推送使用。
        使用 ON CONFLICT 实现"存在则更新"，避免先查后写的竞态。"""
        with db.connect() as conn:
            row = conn.execute("SELECT version FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            new_version = (row["version"] + 1) if row else 1
            conn.execute(
                """INSERT INTO nodes(node_id, label, etype, props, version, state, loc)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(node_id) DO UPDATE SET
                     label=excluded.label, etype=excluded.etype, props=excluded.props,
                     version=excluded.version, state=excluded.state, loc=excluded.loc""",
                (node_id, label, etype, db.dumps(props), new_version, state, loc),
            )
            conn.commit()

    def upsert_edge(self, rtype: str, src: str, dst: str, weight: float = 1.0) -> str | None:
        """写入一条边；写入前用本体校验源/目标类型是否允许相连。
        非法关系直接拒绝并返回 None，这是"本体约束先行"的体现。"""
        src_node = self.get_node(src)
        dst_node = self.get_node(dst)
        if not src_node or not dst_node:
            return None
        if not ontology.is_valid_edge(rtype, src_node["etype"], dst_node["etype"]):
            return None
        # 同类型同端点的边视为同一条，做幂等处理
        exists = db.query_one(
            "SELECT edge_id FROM edges WHERE rtype=? AND src=? AND dst=?", (rtype, src, dst))
        if exists:
            return exists["edge_id"]
        edge_id = f"E-{uuid.uuid4().hex[:8]}"
        db.execute("INSERT INTO edges(edge_id, rtype, src, dst, weight) VALUES(?,?,?,?,?)",
                   (edge_id, rtype, src, dst, weight))
        return edge_id

    def delete_node(self, node_id: str) -> None:
        """删除节点及其关联边，保证图谱不出现悬空边。"""
        db.execute("DELETE FROM edges WHERE src=? OR dst=?", (node_id, node_id))
        db.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))

    # ---------- 读取 ----------
    def get_node(self, node_id: str) -> dict | None:
        """读取单个节点，并把属性 JSON 还原为字典。"""
        row = db.query_one("SELECT * FROM nodes WHERE node_id=?", (node_id,))
        if not row:
            return None
        row["props"] = db.loads(row["props"], {})
        return row

    def _descendant_locations(self, location_id: str) -> list[str]:
        """取某位置的全部下级位置（含自身），用于"按位置筛选子图"。
        位置树通过 locatedIn 边表达，这里做一次宽度优先遍历。"""
        result = {location_id}
        frontier = [location_id]
        while frontier:
            cur = frontier.pop()
            children = db.query_all(
                "SELECT dst FROM edges WHERE rtype='locatedIn' AND src=?", (cur,))
            for c in children:
                if c["dst"] not in result:
                    result.add(c["dst"])
                    frontier.append(c["dst"])
        return list(result)

    def load_subgraph(self, entity_types: list[str] | None = None,
                      location_id: str = "", keyword: str = "",
                      relation_types: list[str] | None = None,
                      limit: int = 300) -> dict:
        """按条件加载子图：先筛节点，再只保留两端都在结果集内的边。
        这样前端拿到的一定是"闭合"的图，不会出现孤立边。"""
        sql = "SELECT * FROM nodes WHERE 1=1"
        params: list = []
        if entity_types:
            sql += " AND etype IN (%s)" % ",".join("?" * len(entity_types))
            params += entity_types
        if keyword:
            sql += " AND (label LIKE ? OR props LIKE ?)"
            params += [f"%{keyword}%", f"%{keyword}%"]
        if location_id:
            children = self._descendant_locations(location_id)
            sql += " AND (node_id IN (%s) OR loc IN (%s))" % (
                ",".join("?" * len(children)), ",".join("?" * len(children)))
            params += children + children
        sql += " ORDER BY etype LIMIT ?"
        params.append(limit)

        nodes = db.query_all(sql, params)
        ids = [n["node_id"] for n in nodes]
        for n in nodes:
            n["props"] = db.loads(n["props"], {})

        edges: list[dict] = []
        if ids:
            esql = "SELECT * FROM edges WHERE src IN (%s) AND dst IN (%s)" % (
                ",".join("?" * len(ids)), ",".join("?" * len(ids)))
            eparams = ids + ids
            if relation_types:
                esql += " AND rtype IN (%s)" % ",".join("?" * len(relation_types))
                eparams += relation_types
            edges = db.query_all(esql, eparams)
        return {"nodes": nodes, "edges": edges, "truncated": len(nodes) >= limit}

    def neighbors(self, node_id: str, depth: int = 1) -> dict:
        """取某节点的一度/多度邻居，用于节点详情面板的"关联关系"展示。"""
        visited = {node_id}
        frontier = [node_id]
        for _ in range(max(1, depth)):
            nxt = []
            for cur in frontier:
                rows = db.query_all(
                    "SELECT * FROM edges WHERE src=? OR dst=?", (cur, cur))
                for e in rows:
                    for end in (e["src"], e["dst"]):
                        if end not in visited:
                            visited.add(end)
                            nxt.append(end)
            frontier = nxt
        nodes = [self.get_node(i) for i in visited]
        nodes = [n for n in nodes if n]
        ids = [n["node_id"] for n in nodes]
        edges = db.query_all(
            "SELECT * FROM edges WHERE src IN (%s) AND dst IN (%s)" % (
                ",".join("?" * len(ids)), ",".join("?" * len(ids))), ids + ids)
        return {"nodes": nodes, "edges": edges}
