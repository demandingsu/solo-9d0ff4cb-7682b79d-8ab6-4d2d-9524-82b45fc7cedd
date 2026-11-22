"""端到端测试：段上传/重扫、补记幂等、成环退回、目录供给、并发核对。"""
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
