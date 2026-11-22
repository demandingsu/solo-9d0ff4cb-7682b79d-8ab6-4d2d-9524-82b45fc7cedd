"""HTTP 路由：网页、JSON API、阅览目录 CSV、转写上传与检索。

并发正确性：所有变更在同一事务内“先核对、后落库”。
BEGIN IMMEDIATE 让并发写者在 SQLite 写锁上排队，核对读到的就是
实际入库时刻的证据状态，杜绝两个窗口同时提交时的核对竞态（TOCTOU）。

检索是纯读，但同样包在一个读事务里：转写与页序证据必须取自同一个
已提交快照，避免查询过程中恰好有重扫/补记提交而读到拼接的状态。
"""
import csv
import io

from flask import Blueprint, jsonify, render_template, request

from .db import get_db
from .logic import (
    TranscriptEntry,
    find_cycle,
    load_edges,
    load_pages,
    load_transcripts,
    note_edge,
    search_transcripts,
    unique_topological_order,
    validate_transcripts,
)

bp = Blueprint("main", __name__)

# CSV 首行若是这些表头则跳过
HEADER_ALIASES = {"page_id", "page", "id", "pageid", "纸页标识", "页标识", "页面"}
TEXT_ALIASES = {"text", "transcript", "content", "全文", "转写", "转写全文", "正文"}


def parse_manifest(text):
    """解析 CSV 清单：取每行第一列作为纸页标识，忽略空行与表头。"""
    pages = []
    first = True
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        cell = row[0].strip()
        if not cell:
            continue
        if first:
            first = False
            if cell.lower() in HEADER_ALIASES:
                continue
        pages.append(cell)
    return pages


def parse_transcripts(text):
    """解析转写 CSV：第一列纸页标识，其余列按逗号接回为转写全文。

    首行若形如表头（第一列像页标识、第二列像转写）则跳过。
    正文含逗号时即使未加引号也能原样保留；含换行需按 CSV 引号规则。
    """
    entries = []
    problems = []
    first = True
    for line_no, row in enumerate(csv.reader(io.StringIO(text)), 1):
        if not row or all(not cell.strip() for cell in row):
            continue
        if first:
            first = False
            if (
                row[0].strip().lower() in HEADER_ALIASES
                and len(row) > 1
                and row[1].strip().lower() in TEXT_ALIASES
            ):
                continue
        if len(row) < 2:
            problems.append(f"第 {line_no} 条记录：需为「纸页标识,转写全文」两列")
            continue
        entries.append(TranscriptEntry(row[0], ",".join(row[1:])))
    return entries, problems


def cycle_payload(cycle):
    return [
        {"edge": f"{e.former} 先于 {e.latter}", "source": e.source} for e in cycle
    ]


def current_state(db):
    segments = []
    for seg in db.execute(
        "SELECT id, name, uploaded_at FROM segments ORDER BY name"
    ):
        rows = [
            r["page_id"]
            for r in db.execute(
                "SELECT page_id FROM segment_rows WHERE segment_id = ? ORDER BY position",
                (seg["id"],),
            )
        ]
        segments.append(
            {"name": seg["name"], "uploaded_at": seg["uploaded_at"], "pages": rows}
        )
    notes = [
        {"id": n["id"], "a": n["page_a"], "b": n["page_b"], "created_at": n["created_at"]}
        for n in db.execute("SELECT * FROM notes ORDER BY id")
    ]
    transcripts = [
        {"page_id": t["page_id"], "chars": t["chars"], "updated_at": t["updated_at"]}
        for t in db.execute(
            "SELECT page_id, length(text) AS chars, updated_at FROM transcripts ORDER BY page_id"
        )
    ]
    order, pair = unique_topological_order(load_pages(db), load_edges(db))
    return segments, notes, transcripts, order, pair


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/api/state")
def api_state():
    db = get_db()
    segments, notes, transcripts, order, pair = current_state(db)
    return jsonify(
        ok=True,
        segments=segments,
        notes=notes,
        transcripts=transcripts,
        unique=order is not None,
        order=order,
        undetermined_pair=list(pair) if pair else None,
    )


@bp.post("/api/transcripts")
def upload_transcripts():
    """上传转写 CSV，按页替换本批涉及的纸页。

    先整批校验（未知纸页 / 同批重号 / 列数不足），任一条不合则整批
    退回，一条都不入库；全部通过才在同一事务里逐页覆盖。
    """
    upload = request.files.get("file")
    if upload is None:
        return jsonify(ok=False, error="缺少 CSV 文件"), 400
    try:
        text = upload.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify(ok=False, error="CSV 需为 UTF-8 编码"), 400

    entries, problems = parse_transcripts(text)
    if problems:
        return jsonify(ok=False, error="转写 CSV 格式有误，已整批退回", problems=problems), 400
    if not entries:
        return jsonify(ok=False, error="CSV 为空：每行「纸页标识,转写全文」"), 400

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        accepted, errors = validate_transcripts(entries, load_pages(db))
        if errors:
            db.execute("ROLLBACK")
            return (
                jsonify(
                    ok=False,
                    error="转写批次含未知纸页或同批重号，已整批退回",
                    problems=errors,
                ),
                400,
            )
        db.executemany(
            """INSERT INTO transcripts (page_id, text) VALUES (?, ?)
               ON CONFLICT(page_id) DO UPDATE SET
                 text = excluded.text,
                 updated_at = datetime('now')""",
            [(e.page_id, e.text) for e in accepted],
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return jsonify(ok=True, pages=[e.page_id for e in accepted], rows=len(accepted)), 200


@bp.get("/api/search")
def search():
    """按字面检索转写，返回页内命中与确定的跨页命中。

    整个查询（转写 + 页序证据）在一个只读事务快照里完成，保证二者
    来自同一个已提交状态；即使全册页序未定也照常检索。
    """
    query = request.args.get("q", "")
    if not query:
        return jsonify(ok=False, error="请给出检索词"), 400

    db = get_db()
    db.execute("BEGIN")
    try:
        pages = load_pages(db)
        edges = load_edges(db)
        transcripts = load_transcripts(db)
    finally:
        db.execute("COMMIT")  # 只读事务，提交即结束快照

    # 同一事务快照内读齐转写与页序证据，再在快照外做纯计算
    hits = search_transcripts(query, pages, edges, transcripts)
    page_hits = [h for h in hits if h["kind"] == "page"]
    cross_hits = [h for h in hits if h["kind"] == "cross_page"]
    return jsonify(
        ok=True,
        query=query,
        page_hits=page_hits,
        cross_page_hits=cross_hits,
        total=len(hits),
    )


@bp.post("/api/segments")
def upload_segment():
    """上传或重扫一个扫描段。同名段只替换本段证据，他段与他证保留。"""
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="缺少扫描段名称"), 400
    upload = request.files.get("file")
    if upload is None:
        return jsonify(ok=False, error="缺少 CSV 文件"), 400
    try:
        text = upload.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify(ok=False, error="CSV 需为 UTF-8 编码"), 400
    pages = parse_manifest(text)
    if not pages:
        return jsonify(ok=False, error="清单为空：每行一个纸页标识"), 400

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        seg = db.execute("SELECT id FROM segments WHERE name = ?", (name,)).fetchone()
        exclude = seg["id"] if seg else None
        # 用“剔除本段旧证据 + 新证据”的全量图核对，成环则整笔退回
        edges = load_edges(db, exclude_segment_id=exclude, extra_segment=(name, pages))
        cycle = find_cycle(edges)
        if cycle is not None:
            db.execute("ROLLBACK")
            return (
                jsonify(
                    ok=False,
                    error="该变更会使先后关系成环，已退回本次变更",
                    cycle=cycle_payload(cycle),
                ),
                409,
            )
        if seg is None:
            cur = db.execute("INSERT INTO segments (name) VALUES (?)", (name,))
            seg_id = cur.lastrowid
            replaced = False
        else:
            seg_id = seg["id"]
            db.execute("DELETE FROM segment_rows WHERE segment_id = ?", (seg_id,))
            db.execute(
                "UPDATE segments SET uploaded_at = datetime('now') WHERE id = ?",
                (seg_id,),
            )
            replaced = True
        db.executemany(
            "INSERT INTO segment_rows (segment_id, position, page_id) VALUES (?, ?, ?)",
            [(seg_id, i + 1, p) for i, p in enumerate(pages)],
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return (
        jsonify(ok=True, segment=name, replaced=replaced, rows=len(pages)),
        200 if replaced else 201,
    )


@bp.post("/api/notes")
def add_note():
    """补记两页先后。重报只记一次（幂等）；成环则退回。"""
    data = request.get_json(silent=True) or request.form
    a = (data.get("a") or "").strip()
    b = (data.get("b") or "").strip()
    if not a or not b:
        return jsonify(ok=False, error="需给出两页标识"), 400
    if a == b:
        return jsonify(ok=False, error="同一页无需补记先后"), 400

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        dup = db.execute(
            "SELECT id FROM notes WHERE page_a = ? AND page_b = ?", (a, b)
        ).fetchone()
        if dup:
            db.execute("COMMIT")  # 无变更，直接放行
            return (
                jsonify(
                    ok=True,
                    duplicate=True,
                    id=dup["id"],
                    message="该补记已存在，重报只记一次",
                ),
                200,
            )
        edges = load_edges(db)
        cycle = find_cycle(edges + [note_edge(None, a, b)])
        if cycle is not None:
            db.execute("ROLLBACK")
            return (
                jsonify(
                    ok=False,
                    error="该补记会使先后关系成环，已退回本次变更",
                    cycle=cycle_payload(cycle),
                ),
                409,
            )
        cur = db.execute("INSERT INTO notes (page_a, page_b) VALUES (?, ?)", (a, b))
        note_id = cur.lastrowid
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return jsonify(ok=True, duplicate=False, id=note_id), 201


@bp.get("/catalog.csv")
def catalog():
    """阅览目录：随证据实时计算。排不出唯一页序时停供（409），并指出待定页对。"""
    db = get_db()
    order, pair = unique_topological_order(load_pages(db), load_edges(db))
    if order is None:
        return (
            jsonify(
                ok=False,
                error="现有证据排不出唯一页序，阅览目录暂停提供",
                undetermined_pair=list(pair) if pair else None,
            ),
            409,
        )
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["position", "page_id"])
    for i, page in enumerate(order, 1):
        writer.writerow([i, page])
    return (
        out.getvalue(),
        200,
        {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="catalog.csv"',
        },
    )
