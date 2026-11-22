# 手稿编序

档案员按段上传重叠扫描的 CSV 清单，系统汇总各段扫描顺序与手工补记的
两页先后，核对无环后排出手稿页序，并提供阅览目录 CSV。

## 启动

```bash
docker compose up --build   # 打开 http://localhost:8000
```

本地开发（无需 Docker）：

```bash
pip install -r requirements-dev.txt
python run.py               # 或 flask --app run:app run
python -m pytest tests/ -v  # 测试
```

数据存 SQLite，路径由环境变量 `MSO_DB` 指定（容器内默认
`/data/manuscript.db`，本地默认 `instance/manuscript.db`）。

## 规则

- **扫描段 CSV**：每行一个纸页标识（取第一列，首行可为 `page_id` 等表头），
  行序即该段扫描顺序；同一页在各段沿用同一标识。相邻行产生“先后”证据。
- **重扫**：同名段重传只替换本段证据（补页、改序均可），他段与他证保留；
  若新证据与保留证据成环，整笔退回，旧段原样。
- **补记**：登记一对页的先后；同一对重报只记一次（幂等）；成环则退回，
  并列出环上每条边的来源（扫描段行号 / 补记编号）。
- **并发**：所有变更在 `BEGIN IMMEDIATE` 事务内“先核对、后落库”，
  SQLite 写锁使并发提交串行化，核对读到的即实际入库时刻的证据。
- **阅览目录** `GET /catalog.csv`：随证据实时计算。排不出唯一页序时
  返回 409 停供（不给旧目录），并指出一对先后待定的页。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页 |
| GET | `/api/state` | 段、补记、唯一页序或待定页对 |
| POST | `/api/segments` | 上传/重扫段（multipart：`name` + `file`） |
| POST | `/api/notes` | 补记（JSON：`{"a": "...", "b": "..."}`） |
| GET | `/catalog.csv` | 唯一时 200 给 CSV；否则 409 给待定页对 |

## 结构

- `app/logic.py` — 证据图、成环检测（三色 DFS）、唯一拓扑序（Kahn）
- `app/routes.py` — 路由与事务边界
- `app/db.py` — SQLite 连接与模式
- `tests/test_app.py` — 含重扫回滚、并发冲突补记、并发重报等 13 项测试
