"""手稿编序核心逻辑：证据汇总、成环检测、唯一拓扑序。

证据分两类：
- 扫描段：CSV 行序即扫描顺序，相邻两行产生一条“先后”边；
- 补记：档案员手工登记的一对先后。

所有证据汇成有向图。任何变更（上传/重扫扫描段、新增补记）都先在
事务内把变更后的全量证据建好图并查环，成环则整体回滚。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Edge:
    """一条“先后”证据：former 先于 latter，source 为人类可读来源。"""

    former: str
    latter: str
    source: str


def segment_edges(name, pages):
    """一个扫描段的相邻行产生的先后证据，pages 为该段行序。"""
    edges = []
    for i in range(len(pages) - 1):
        edges.append(
            Edge(
                pages[i],
                pages[i + 1],
                f"扫描段「{name}」第{i + 1}–{i + 2}行（{pages[i]} 先于 {pages[i + 1]}）",
            )
        )
    return edges


def note_edge(note_id, page_a, page_b):
    label = f"补记#{note_id}" if note_id is not None else "补记（本次提交）"
    return Edge(page_a, page_b, f"{label}（{page_a} 先于 {page_b}）")


def load_pages(conn):
    """全部已知纸页：扫描段出现过的 + 补记提到的。"""
    pages = set()
    for row in conn.execute("SELECT page_id FROM segment_rows"):
        pages.add(row["page_id"])
    for row in conn.execute("SELECT page_a, page_b FROM notes"):
        pages.add(row["page_a"])
        pages.add(row["page_b"])
    return pages


def load_edges(conn, exclude_segment_id=None, extra_segment=None):
    """汇总当前全部证据边。

    exclude_segment_id: 重扫时剔除本段旧证据（只替换本段，他段保留）；
    extra_segment: (name, [page_id, ...]) 待入库的新段证据，先参与核对。
    """
    edges = []
    for row in conn.execute("SELECT id, page_a, page_b FROM notes ORDER BY id"):
        edges.append(note_edge(row["id"], row["page_a"], row["page_b"]))
    for seg in conn.execute("SELECT id, name FROM segments ORDER BY name"):
        if seg["id"] == exclude_segment_id:
            continue
        rows = [
            r["page_id"]
            for r in conn.execute(
                "SELECT page_id FROM segment_rows WHERE segment_id = ? ORDER BY position",
                (seg["id"],),
            )
        ]
        edges.extend(segment_edges(seg["name"], rows))
    if extra_segment is not None:
        name, pages = extra_segment
        edges.extend(segment_edges(name, pages))
    return edges


def find_cycle(edges):
    """若有环，返回构成环的 Edge 列表（含来源）；否则 None。迭代式三色 DFS。"""
    adj = {}
    for e in edges:
        adj.setdefault(e.former, []).append(e)
        adj.setdefault(e.latter, [])

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in adj}

    for root in adj:
        if color[root] != WHITE:
            continue
        color[root] = GRAY
        stack = [(root, iter(adj[root]))]
        path_nodes = [root]
        path_edges = []
        while stack:
            node, it = stack[-1]
            descended = False
            for e in it:
                state = color[e.latter]
                if state == WHITE:
                    color[e.latter] = GRAY
                    stack.append((e.latter, iter(adj[e.latter])))
                    path_nodes.append(e.latter)
                    path_edges.append(e)
                    descended = True
                    break
                if state == GRAY:  # 回边 → 环
                    idx = path_nodes.index(e.latter)
                    return path_edges[idx:] + [e]
            if not descended:
                color[node] = BLACK
                stack.pop()
                path_nodes.pop()
                if path_edges:
                    path_edges.pop()
    return None


def unique_topological_order(pages, edges):
    """尝试排出唯一页序。

    返回 (order, undetermined_pair)：
    - 唯一：order 为页序列表，pair 为 None；
    - 不唯一：order 为 None，pair 给出一对先后待定的页
      （Kahn 过程中同时入度为 0 的两页必无先后关系）。
    """
    indeg = {p: 0 for p in pages}
    adj = {p: [] for p in pages}
    for e in edges:
        adj[e.former].append(e.latter)
        indeg[e.latter] += 1

    ready = sorted(p for p in pages if indeg[p] == 0)
    order = []
    while ready:
        if len(ready) > 1:
            return None, (ready[0], ready[1])
        node = ready.pop(0)
        order.append(node)
        for nxt in adj[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort()
    if len(order) != len(pages):  # 有环；正常流程不会到此（变更已先拦环）
        return None, None
    return order, None
