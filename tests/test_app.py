"""端到端测试：段上传/重扫、补记幂等、成环退回、目录供给、并发核对、
转写上传与检索（页内命中、跨页紧邻、整批退回、快照一致）。"""
import csv
import io
import threading

import pytest

from app import create_app


@pytest.fixture()
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.db")})


@pytest.fixture()
def client(app):
    return app.test_client()


def upload(client, name, pages, header=True):
    lines = (["page_id"] if header else []) + list(pages)
    data = {
        "name": name,
        "file": (io.BytesIO("\n".join(lines).encode("utf-8")), f"{name}.csv"),
    }
    return client.post("/api/segments", data=data, content_type="multipart/form-data")


def upload_transcripts(client, rows, header=True):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    if header:
        writer.writerow(["page_id", "text"])
    for row in rows:
        writer.writerow(row)
    data = {"file": (io.BytesIO(out.getvalue().encode("utf-8")), "transcripts.csv")}
    return client.post("/api/transcripts", data=data, content_type="multipart/form-data")


def search(client, q):
    return client.get("/api/search", query_string={"q": q})


def note(client, a, b):
    return client.post("/api/notes", json={"a": a, "b": b})


def state(client):
    return client.get("/api/state").get_json()


# ---------- 基本流程 ----------

def test_upload_and_unique_catalog(client):
    r = upload(client, "S1", ["A", "B", "C"])
    assert r.status_code == 201 and r.get_json()["rows"] == 3

    s = state(client)
    assert s["unique"] is True and s["order"] == ["A", "B", "C"]

    r = client.get("/catalog.csv")
    assert r.status_code == 200
    assert r.text == "position,page_id\n1,A\n2,B\n3,C\n"


def test_csv_without_header_and_blank_lines(client):
    data = {"name": "S1", "file": (io.BytesIO(b"A\n\nB\n"), "s1.csv")}
    r = client.post("/api/segments", data=data, content_type="multipart/form-data")
    assert r.status_code == 201
    assert state(client)["order"] == ["A", "B"]


def test_non_unique_blocks_catalog_and_names_pair(client):
    upload(client, "S1", ["A", "B"])
    upload(client, "S2", ["C", "D"])

    s = state(client)
    assert s["unique"] is False
    assert s["undetermined_pair"] == ["A", "C"]

    r = client.get("/catalog.csv")
    assert r.status_code == 409  # 停供旧目录
    assert r.get_json()["undetermined_pair"] == ["A", "C"]


def test_note_resolves_order_and_catalog_updates(client):
    upload(client, "S1", ["A", "B"])
    upload(client, "S2", ["C", "D"])
    assert client.get("/catalog.csv").status_code == 409

    r = note(client, "B", "C")
    assert r.status_code == 201

    r = client.get("/catalog.csv")  # 目录随证据更新
    assert r.status_code == 200
    assert r.text == "position,page_id\n1,A\n2,B\n3,C\n4,D\n"


def test_duplicate_note_recorded_once(client):
    assert note(client, "A", "B").status_code == 201
    r = note(client, "A", "B")
    assert r.status_code == 200 and r.get_json()["duplicate"] is True
    assert len(state(client)["notes"]) == 1


def test_note_same_page_rejected(client):
    assert note(client, "A", "A").status_code == 400


# ---------- 成环退回 ----------

def test_note_cycle_rejected_with_sources(client):
    upload(client, "S1", ["A", "B", "C"])
    r = note(client, "C", "A")
    assert r.status_code == 409
    payload = r.get_json()
    assert "成环" in payload["error"]
    sources = [c["source"] for c in payload["cycle"]]
    assert any("扫描段「S1」" in s for s in sources)
    assert any("本次提交" in s for s in sources)
    assert state(client)["notes"] == []  # 已退回


def test_segment_cycle_rejected_and_old_evidence_kept(client):
    upload(client, "S1", ["A", "B"])
    note(client, "B", "C")
    r = upload(client, "S2", ["C", "A"])  # C→A 与 A→B→C 成环
    assert r.status_code == 409
    sources = [c["source"] for c in r.get_json()["cycle"]]
    assert any("扫描段「S1」" in s for s in sources)
    assert any("补记#" in s for s in sources)
    assert any("扫描段「S2」" in s for s in sources)

    s = state(client)  # S2 未入库，旧证据原样
    assert [g["name"] for g in s["segments"]] == ["S1"]
    assert len(s["notes"]) == 1


def test_rescan_cycle_rolls_back_and_keeps_old_rows(client):
    upload(client, "S1", ["A", "B"])
    note(client, "B", "C")  # A→B→C
    upload(client, "S2", ["X", "Y"])
    r = upload(client, "S2", ["C", "A"])  # 新边 C→A 与他证成环 C→A→B→C
    assert r.status_code == 409
    segs = {g["name"]: g["pages"] for g in state(client)["segments"]}
    assert segs["S2"] == ["X", "Y"]  # 旧段证据原样保留


# ---------- 重扫：只替换本段证据 ----------

def test_rescan_replaces_only_own_segment(client):
    upload(client, "S1", ["A", "B", "C"])
    upload(client, "S2", ["B", "D"])
    assert state(client)["unique"] is False  # C、D 待定

    note(client, "C", "D")
    assert state(client)["order"] == ["A", "B", "C", "D"]

    r = upload(client, "S1", ["A", "X", "B", "C"])  # 重扫：补页 X、改序
    assert r.status_code == 200 and r.get_json()["replaced"] is True

    s = state(client)
    segs = {g["name"]: g["pages"] for g in s["segments"]}
    assert segs["S1"] == ["A", "X", "B", "C"]
    assert segs["S2"] == ["B", "D"]  # 他段原页保留
    assert s["order"] == ["A", "X", "B", "C", "D"]  # D 仍在序中

    r = client.get("/catalog.csv")
    assert r.status_code == 200
    assert r.text == "position,page_id\n1,A\n2,X\n3,B\n4,C\n5,D\n"


# ---------- 并发：按实际入库时的证据核对 ----------

def test_concurrent_conflicting_notes_exactly_one_commits(app):
    """两窗口同时补记：各自单独看都合法，合起来成环 —— 必须只入一笔。"""
    client = app.test_client()
    upload(client, "S1", ["A", "B"])  # 现有 A→B

    barrier = threading.Barrier(2)
    results = []

    def worker(a, b):
        c = app.test_client()
        barrier.wait()
        results.append(c.post("/api/notes", json={"a": a, "b": b}).status_code)

    t1 = threading.Thread(target=worker, args=("C", "A"))  # C→A 单独合法
    t2 = threading.Thread(target=worker, args=("B", "C"))  # B→C 单独合法
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [201, 409]  # 合起来 A→B→C→A 成环，后到者被退回
    assert len(state(client)["notes"]) == 1


def test_concurrent_duplicate_notes_recorded_once(app):
    """两窗口同时重报同一补记：只记一次。"""
    client = app.test_client()
    upload(client, "S1", ["A", "B"])

    barrier = threading.Barrier(2)
    results = []

    def worker():
        c = app.test_client()
        barrier.wait()
        results.append(c.post("/api/notes", json={"a": "B", "b": "C"}).status_code)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 201]
    assert len(state(client)["notes"]) == 1


def test_concurrent_rescans_of_same_segment_serialize(app):
    """两窗口同时重扫同名段：串行生效，最终为其中一版，且证据无环。"""
    client = app.test_client()
    upload(client, "S1", ["A", "B"])

    barrier = threading.Barrier(2)
    results = []

    def worker(pages):
        c = app.test_client()
        barrier.wait()
        results.append(upload(c, "S1", pages).status_code)

    t1 = threading.Thread(target=worker, args=(["A", "X", "B"],))
    t2 = threading.Thread(target=worker, args=(["A", "Y", "B"],))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 200]  # 均为重扫替换
    segs = state(client)["segments"]
    assert len(segs) == 1
    assert segs[0]["pages"] in (["A", "X", "B"], ["A", "Y", "B"])


# ---------- 转写上传：按页替换、整批退回 ----------

def test_transcript_upload_and_page_hit(client):
    upload(client, "S1", ["A", "B"])
    r = upload_transcripts(client, [["A", "山高月小，水落石出"], ["B", "清风徐来"]])
    assert r.status_code == 200 and r.get_json()["pages"] == ["A", "B"]

    s = state(client)
    assert [t["page_id"] for t in s["transcripts"]] == ["A", "B"]

    r = search(client, "月小")
    assert r.status_code == 200
    hits = r.get_json()["page_hits"]
    assert len(hits) == 1 and hits[0]["page_id"] == "A"
    assert "山高月小，水落石出" in hits[0]["snippet"]


def test_transcript_is_literal_and_case_sensitive(client):
    upload(client, "S1", ["A"])
    upload_transcripts(client, [["A", "Abc ABC"]])
    assert search(client, "abc").get_json()["total"] == 0
    assert search(client, "ABC").get_json()["total"] == 1


def test_transcript_upload_replaces_only_listed_pages(client):
    upload(client, "S1", ["A", "B"])
    upload_transcripts(client, [["A", "旧文"], ["B", "乙页"]])
    upload_transcripts(client, [["A", "新文"]])
    assert search(client, "旧文").get_json()["total"] == 0
    assert search(client, "新文").get_json()["total"] == 1
    assert search(client, "乙页").get_json()["total"] == 1  # 未列出的页保留


def test_transcript_unknown_page_rejects_whole_batch(client):
    upload(client, "S1", ["A", "B"])
    r = upload_transcripts(client, [["A", "甲"], ["ZZ", "未知"]])
    assert r.status_code == 400
    assert any("未知纸页「ZZ」" in p for p in r.get_json()["problems"])
    assert state(client)["transcripts"] == []  # A 也未入库


def test_transcript_duplicate_page_rejects_whole_batch(client):
    upload(client, "S1", ["A", "B"])
    r = upload_transcripts(client, [["A", "第一次"], ["A", "第二次"], ["B", "乙"]])
    assert r.status_code == 400
    assert any("重复" in p for p in r.get_json()["problems"])
    assert state(client)["transcripts"] == []


def test_transcript_malformed_row_rejects_whole_batch(client):
    upload(client, "S1", ["A", "B"])
    data = {"file": (io.BytesIO(b"A\nB,x\n"), "t.csv")}
    r = client.post("/api/transcripts", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    assert state(client)["transcripts"] == []


def test_transcript_text_with_commas_and_quoted_newlines(client):
    upload(client, "S1", ["A", "B"])
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["page_id", "text"])
    writer.writerow(["A", "第一行\n第二行，带逗号"])
    writer.writerow(["B", "乙"])
    data = {"file": (io.BytesIO(out.getvalue().encode("utf-8")), "t.csv")}
    assert client.post(
        "/api/transcripts", data=data, content_type="multipart/form-data"
    ).status_code == 200
    hit = search(client, "第二行，带逗号").get_json()["page_hits"]
    assert len(hit) == 1 and hit[0]["page_id"] == "A"


# ---------- 跨页检索 ----------

def test_cross_page_hit_requires_forced_adjacency(client):
    upload(client, "S1", ["A", "B"])
    upload_transcripts(client, [["A", "……山高月"], ["B", "小，水落石出……"]])
    payload = search(client, "月小").get_json()
    assert len(payload["cross_page_hits"]) == 1
    hit = payload["cross_page_hits"][0]
    assert hit["pages"] == ["A", "B"]
    assert "山高月" in hit["snippet"] and "小，水落石出" in hit["snippet"]


def test_cross_page_reverse_direction_not_reported(client):
    upload(client, "S1", ["A", "B"])  # A 先于 B
    upload_transcripts(client, [["A", "cd开头"], ["B", "结尾ab"]])
    payload = search(client, "abcd").get_json()  # B 的末尾接 A 的开头
    assert payload["cross_page_hits"] == []
    assert payload["page_hits"] == []


def test_cross_page_needs_order_not_full_catalog(client):
    """全册页序不唯一，但两页在所有页序里都紧挨 → 跨页命中照供。"""
    upload(client, "S1", ["A", "B"])
    upload(client, "S2", ["C"])  # 单页段，本身不产生先后边
    upload(client, "S3", ["D"])
    note(client, "B", "C")
    note(client, "B", "D")  # C、D 都固定在 B 之后，但二者可互换
    assert state(client)["unique"] is False
    upload_transcripts(client, [["A", "前文ab"], ["B", "cd后文"]])
    payload = search(client, "abcd").get_json()
    assert len(payload["cross_page_hits"]) == 1
    assert payload["cross_page_hits"][0]["pages"] == ["A", "B"]


def test_untranscribed_page_between_blocks_cross_page(client):
    upload(client, "S1", ["A", "X", "B"])  # X 未转写但强制夹在中间
    upload_transcripts(client, [["A", "前文ab"], ["B", "cd后文"]])
    payload = search(client, "abcd").get_json()
    assert payload["page_hits"] == []
    assert payload["cross_page_hits"] == []


def test_unrelated_untranscribed_page_can_be_inserted(client):
    """A、B 有先后但 X 与二者无关，可被排到 A、B 之间 → 不算紧挨。"""
    upload(client, "S1", ["A", "B"])
    upload(client, "S2", ["X", "Y"])  # X、Y 与 A、B 无先后
    upload_transcripts(client, [["A", "前文ab"], ["B", "cd后文"]])
    assert search(client, "abcd").get_json()["cross_page_hits"] == []


def test_unrelated_page_outside_pair_does_not_block(client):
    upload(client, "S1", ["A", "B", "X"])  # X 确定在 B 之后
    upload_transcripts(client, [["A", "前文ab"], ["B", "cd后文"]])
    payload = search(client, "abcd").get_json()
    assert len(payload["cross_page_hits"]) == 1


def test_page_hit_works_when_order_undetermined(client):
    upload(client, "S1", ["A", "B"])
    upload(client, "S2", ["C", "D"])
    upload_transcripts(client, [["C", "内有月小二字"]])
    payload = search(client, "月小").get_json()
    assert payload["page_hits"][0]["page_id"] == "C"


def test_rescan_without_transcript_change_updates_cross_page(client):
    """重扫与补记即使没改转写，也要让检索反映新的相邻关系。"""
    upload(client, "S1", ["A", "B"])
    upload(client, "S2", ["X", "Y"])  # X、Y 可插到 A、B 之间
    upload_transcripts(client, [["A", "前文ab"], ["B", "cd后文"]])
    assert search(client, "abcd").get_json()["cross_page_hits"] == []

    note(client, "B", "X")  # A→B→X→Y：X 被推到 B 之后
    payload = search(client, "abcd").get_json()
    assert len(payload["cross_page_hits"]) == 1

    upload(client, "S2", ["X", "A"])  # 重扫：X→A→B→Y，A、B 仍紧挨
    payload = search(client, "abcd").get_json()
    assert len(payload["cross_page_hits"]) == 1


def test_rescan_can_remove_adjacency(client):
    upload(client, "S1", ["A", "B"])
    upload_transcripts(client, [["A", "前文ab"], ["B", "cd后文"]])
    assert len(search(client, "abcd").get_json()["cross_page_hits"]) == 1

    upload(client, "S1", ["A", "X", "B"])  # 重扫插入未转写页
    assert search(client, "abcd").get_json()["cross_page_hits"] == []


def test_search_snapshot_sees_one_committed_state(app, monkeypatch):
    """查询中途有提交：本次查询仍用开始时的同一快照，不拼两个状态。"""
    import sqlite3

    from app import routes

    c = app.test_client()
    upload(c, "S1", ["A", "B"])
    upload_transcripts(c, [["A", "前文ab"], ["B", "cd后文"]])
    assert len(search(c, "abcd").get_json()["cross_page_hits"]) == 1

    original = routes.load_edges

    def load_edges_then_commit(db, *a, **kw):
        edges = original(db, *a, **kw)
        if not getattr(load_edges_then_commit, "done", False):
            load_edges_then_commit.done = True
            # 另一连接提交新证据：A→X→B，X 会把 A、B 隔开
            other = sqlite3.connect(app.config["DATABASE"], isolation_level=None)
            other.execute("BEGIN IMMEDIATE")
            cur = other.execute("INSERT INTO segments (name) VALUES ('S9')")
            seg_id = cur.lastrowid
            other.executemany(
                "INSERT INTO segment_rows (segment_id, position, page_id) VALUES (?, ?, ?)",
                [(seg_id, i + 1, p) for i, p in enumerate(["A", "X", "B"])],
            )
            other.execute("COMMIT")
            other.close()
        return edges

    monkeypatch.setattr(routes, "load_edges", load_edges_then_commit)
    payload = search(c, "abcd").get_json()
    # 快照里 X 尚未入库，A、B 仍紧挨；若不包事务则会看到 X 夹在中间
    assert len(payload["cross_page_hits"]) == 1

    monkeypatch.setattr(routes, "load_edges", original)
    assert search(c, "abcd").get_json()["cross_page_hits"] == []


# ---------- 紧邻判定：与枚举页序的结果逐一对照 ----------

def test_adjacency_matches_exhaustive_enumeration():
    import itertools
    import random

    from app.logic import Edge, adjacent_in_all_orders

    def linear_extensions(nodes, edges):
        result = []
        for perm in itertools.permutations(nodes):
            pos = {p: i for i, p in enumerate(perm)}
            if all(pos[e.former] < pos[e.latter] for e in edges):
                result.append(perm)
        return result

    rng = random.Random(20261122)
    for n in range(2, 7):
        nodes = [f"P{i}" for i in range(n)]
        pairs = list(itertools.combinations(nodes, 2))
        for _ in range(60):
            chosen = rng.sample(pairs, rng.randint(0, min(len(pairs), 7)))
            edges = [Edge(a, b, f"{a}<{b}") for a, b in chosen]
            exts = linear_extensions(nodes, edges)
            if not exts:
                continue
            for a, b in pairs:
                # 枚举：所有页序里 a、b 都须同向紧挨；方向也须一致
                dirs = set()
                for ext in exts:
                    ia, ib = ext.index(a), ext.index(b)
                    if abs(ia - ib) != 1:
                        dirs = set()
                        break
                    dirs.add("a_before_b" if ia < ib else "b_before_a")
                direction = next(iter(dirs)) if len(dirs) == 1 else None
                ok, _, got = adjacent_in_all_orders(a, b, nodes, edges)
                assert ok == (direction is not None), (nodes, edges, a, b)
                if ok:
                    assert got == direction, (nodes, edges, a, b, got, direction)
