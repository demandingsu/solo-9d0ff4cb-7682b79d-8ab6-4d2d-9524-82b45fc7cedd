"""手稿编序核心逻辑：证据汇总、成环检测、唯一拓扑序、转写检索。

证据分两类：
- 扫描段：CSV 行序即扫描顺序，相邻两行产生一条“先后”边；
- 补记：档案员手工登记的一对先后。

所有证据汇成有向图。任何变更（上传/重扫扫描段、新增补记、上传转写）
都先在事务内按变更后的全量证据核对，成环或整批校验失败则整体回滚。

转写检索不枚举任何可能的页序：它只对证据图做一次传递闭包，据此
判断一对页是否在“所有符合当前证据的线性页序”里都同向紧挨。
"""
from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Edge:
    """一条“先后”证据：former 先于 latter，source 为人类可读来源。"""

    former: str
    latter: str
    source: str


@dataclass(frozen=True)
class TranscriptEntry:
    """一页的转写：page_id 为纸页标识，text 为该页转写全文。"""

    page_id: str
    text: str


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


def load_transcripts(conn):
    """当前已入库的转写：{page_id: 全文}。"""
    return {r["page_id"]: r["text"] for r in conn.execute("SELECT page_id, text FROM transcripts")}


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


def _build_adj(pages, edges):
    """把边列表整理成邻接表与入度；只收 pages 内的节点。"""
    adj = {p: [] for p in pages}
    indeg = {p: 0 for p in pages}
    for e in edges:
        if e.former not in adj or e.latter not in adj:
            continue
        adj[e.former].append(e.latter)
        indeg[e.latter] += 1
    return adj, indeg


def _kahn_order(pages, edges):
    """Kahn 拓扑排序，返回 (完整页序, 所有同时可选页对)。

    同时入度为 0 的任意两页之间都没有先后证据，互换二者位置仍是
    合法页序，因此它们是“先后待定”的一对。用最小堆取页只为让输出
    稳定；返回的待定页对只用于提示，唯一性不受取页顺序影响。
    """
    adj, indeg = _build_adj(pages, edges)
    ready = [p for p in pages if indeg[p] == 0]
    heapq.heapify(ready)
    order = []
    ambiguous = []
    while ready:
        if len(ready) > 1:
            ambiguous.append((ready[0], ready[1]))  # 堆中前两页即一对待定
        node = heapq.heappop(ready)
        order.append(node)
        for nxt in adj[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                heapq.heappush(ready, nxt)
    if len(order) != len(pages):  # 有环；正常流程不会到此（变更已先拦环）
        return None, ambiguous
    return order, ambiguous


def unique_topological_order(pages, edges):
    """尝试排出唯一页序。

    返回 (order, undetermined_pair)：
    - 唯一：order 为页序列表，pair 为 None；
    - 不唯一：order 为 None，pair 给出一对先后待定的页
      （Kahn 过程中同时入度为 0 的两页必无先后关系）。
    """
    order, ambiguous = _kahn_order(pages, edges)
    if order is None or ambiguous:
        return None, (ambiguous[0] if ambiguous else None)
    return order, None


# ---------------------------------------------------------------------------
# 转写：校验与检索
# ---------------------------------------------------------------------------


def normalize_text(text):
    """检索用的字面归一：统一换行，其余字符保持原样（按字面查找）。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_transcripts(entries, known_pages):
    """整批核对转写：未知纸页或同批重号都拒绝入库。

    返回 (accepted_entries, errors)。只要有一条错，accepted 为 None，
    errors 列出全部问题，整批一条都不入库。
    """
    errors = []
    seen = {}
    accepted = []
    for idx, entry in enumerate(entries, 1):
        page_id = entry.page_id.strip()
        if not page_id:
            errors.append(f"本批第 {idx} 条：纸页标识为空")
            continue
        if page_id not in known_pages:
            errors.append(f"本批第 {idx} 条：未知纸页「{page_id}」")
            continue
        if page_id in seen:
            errors.append(
                f"本批第 {idx} 条：纸页「{page_id}」与第 {seen[page_id]} 条重复"
            )
            continue
        seen[page_id] = idx
        accepted.append(TranscriptEntry(page_id, normalize_text(entry.text)))
    if errors:
        return None, errors
    return accepted, []


def _reversed_edges(edges):
    return [Edge(e.latter, e.former, e.source) for e in edges]


def _closure_bits(nodes, edges):
    """传递闭包：返回 desc[i]，第 j 位为 1 表示 nodes[i] 可达 nodes[j]。

    只沿证据边做一次位集传播（按拓扑逆序合并后继的闭包），复杂度
    O(V·E/字长)，不枚举任何页序。有环时返回 None。
    """
    n = len(nodes)
    index = {p: i for i, p in enumerate(nodes)}
    succ = [set() for _ in range(n)]
    for e in edges:
        i = index.get(e.former)
        j = index.get(e.latter)
        if i is not None and j is not None and i != j:
            succ[i].add(j)

    indeg = [0] * n
    for i in range(n):
        for j in succ[i]:
            indeg[j] += 1
    ready = deque(i for i in range(n) if indeg[i] == 0)
    order = []
    while ready:
        i = ready.popleft()
        order.append(i)
        for j in succ[i]:
            indeg[j] -= 1
            if indeg[j] == 0:
                ready.append(j)
    if len(order) != n:  # 有环；正常流程不会到此
        return None

    desc = [0] * n
    for i in reversed(order):
        bits = 0
        for j in succ[i]:
            bits |= (1 << j) | desc[j]
        desc[i] = bits
    return desc


def _adjacency(a, b, index, desc, anc, all_mask):
    """在已算好的闭包上判断 a、b 是否在所有合法页序里同向紧挨。

    返回 (ok, reason, direction)。direction 为 "a_before_b" 或
    "b_before_a"，表示证据唯一确定的方向。
    """
    ia, ib = index[a], index[b]
    if desc[ia] >> ib & 1:
        first, second, direction = ia, ib, "a_before_b"
    elif desc[ib] >> ia & 1:
        first, second, direction = ib, ia, "b_before_a"
    else:
        return False, "证据无法确定两页的先后或方向", None

    # 某个页 c 能被排进二者之间，当且仅当 c 不早于 first、也不晚于
    # second。因此“所有合法页序里都紧挨”等价于：
    # 全体页 = (first 及其全部祖先) ∪ (second 及其全部后代)。
    cover = (anc[first] | (1 << first)) | (desc[second] | (1 << second))
    if cover != all_mask:
        return False, "存在可排到两页之间的纸页，紧邻不成立", None
    return True, None, direction


def _closure_pair(nodes, edges):
    desc = _closure_bits(nodes, edges)
    anc = _closure_bits(nodes, _reversed_edges(edges))
    if desc is None or anc is None:
        return None, None
    return desc, anc


def adjacent_in_all_orders(a, b, pages, edges):
    """判断 a、b 是否在所有符合证据的线性页序里都同向紧挨。

    返回 (ok, reason, direction)。判定不枚举页序：一次传递闭包即可
    给出充要条件——设证据唯一确定 first 先于 second，则二者在所有
    合法页序里紧挨，当且仅当“first 及其祖先”与“second 及其后代”
    覆盖全书所有纸页；否则必有某页能插到二者之间。
    """
    nodes = sorted(set(pages))
    if a == b or a not in nodes or b not in nodes:
        return False, "两页需为同册中不同的纸页", None
    desc, anc = _closure_pair(nodes, edges)
    if desc is None:
        return False, "证据成环，无法判定页序", None
    index = {p: i for i, p in enumerate(nodes)}
    all_mask = (1 << len(nodes)) - 1
    return _adjacency(a, b, index, desc, anc, all_mask)


def _snippet(text, start, end, context):
    left = max(0, start - context)
    right = min(len(text), end + context)
    return (
        ("…" if left else "")
        + text[left:right]
        + ("…" if right < len(text) else "")
    )


def search_transcripts(query, pages, edges, transcripts, context=24):
    """按字面检索纸页转写，返回页内命中与确定的跨页命中。

    - 页内命中：query 在某页转写里完整出现（按字面，不做大小写折叠）；
    - 跨页命中：前页末尾接后页开头恰好拼出 query（只在页界处切分），
      且这两页在所有符合当前证据的页序里都同向紧挨。
    传给 pages 的纸页（即使没有转写）都参与页序判定：它们可能正把
    候选的两页隔开。全册页序不唯一时，仍照常给出页内命中与确定的
    跨页命中。
    """
    if not query:
        return []
    query = normalize_text(query)
    pages = set(pages)
    nodes = sorted(pages)
    results = []

    for page in nodes:
        text = transcripts.get(page)
        if text is None:
            continue
        start = text.find(query)
        while start != -1:
            results.append(
                {
                    "kind": "page",
                    "page_id": page,
                    "pages": [page],
                    "snippet": _snippet(text, start, start + len(query), context),
                }
            )
            start = text.find(query, start + 1)

    if len(query) < 2:
        return results

    desc, anc = _closure_pair(nodes, edges)
    if desc is None:  # 有环；正常流程不会到此
        return results
    index = {p: i for i, p in enumerate(nodes)}
    all_mask = (1 << len(nodes)) - 1

    seen_pairs = set()
    for split in range(1, len(query)):
        head, tail = query[:split], query[split:]
        ending = [p for p in nodes if transcripts.get(p, "").endswith(head)]
        starting = [p for p in nodes if transcripts.get(p, "").startswith(tail)]
        for a in ending:
            for b in starting:
                if a == b or (a, b) in seen_pairs:
                    continue
                ok, _, direction = _adjacency(a, b, index, desc, anc, all_mask)
                if not ok or direction != "a_before_b":
                    continue
                seen_pairs.add((a, b))
                a_text, b_text = transcripts[a], transcripts[b]
                a_start = len(a_text) - len(head)
                left = max(0, a_start - context)
                right = min(len(b_text), len(tail) + context)
                results.append(
                    {
                        "kind": "cross_page",
                        "page_id": a,
                        "pages": [a, b],
                        "direction": "a_before_b",
                        "snippet": (
                            ("…" if left else "")
                            + a_text[left:]
                            + " ⟨页界⟩ "
                            + b_text[:right]
                            + ("…" if right < len(b_text) else "")
                        ),
                    }
                )
    return results
