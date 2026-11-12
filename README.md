# 联合目录补采采集服务

联合目录管理员对单馆书目进行补采的 OAI-PMH 采集服务。核心保证：**翻页失败绝不让半批记录入库**。

## 需求与实现对照

| 需求 | 实现 |
| --- | --- |
| FastAPI 接收截止日，首次另填起始日 | `POST /api/v1/harvest`，请求体 `until` 必填、`from` 选填 |
| 之后从上次成功采集的截止日接着采，起止日都包含 | `from` 留空时取 `harvest_progress.last_until` 作为起始日（含当天） |
| OAI-PMH ListRecords 按日期拉取 oai_dc | 首请求 `verb=ListRecords&metadataPrefix=oai_dc&from=...&until=...` |
| 续页用 resumptionToken 替代日期和格式参数 | 续页请求只带 `verb` + `resumptionToken`；样例源对混用参数返回 `badArgument` |
| 令牌失效丢弃已取页，按原区间重抓一次 | 捕获 `badResumptionToken` 后清空已取页，用原始 from/until 重新拉取；只重抓一次 |
| 按标识更新 SQLite 书目 | `bib_records.identifier` 主键 upsert，重复采集幂等 |
| 遇删除标记就撤下 | header `status=deleted` 的记录置为 `withdrawn`（库中不存在则写墓碑） |
| 整批采完才提交书目变更和截止日 | 单事务 `BEGIN IMMEDIATE`，全部页取完才写库并提交；异常整体回滚 |
| 无匹配也推进截止日 | `noRecordsMatch` 不写书目，同事务推进 `last_until` |
| 撤销 API：传入采集运行编号，只能撤销最近一次成功补采 | `POST /api/v1/runs/{run_id}/revoke`，失败批次不参与判定，不满足条件返回 409 |
| 改写书目恢复批前内容/状态，新增记录移除 | 每批落库前在 `bib_snapshots` 保存批前整行；撤销时整行写回，本批新增（pre_image 为 NULL）删除 |
| 续采进度退回批前，撤销首次补采显示未采集 | 同事务恢复 `harvest_progress`；批前无进度则删除进度行，`last_until` 返回 null |
| 无匹配但推进日期的批次也能撤销 | 无匹配批次同样记录批前进度快照，撤销仅回退截止日 |
| 撤销在单事务内恢复目录与进度并标记运行，失败则都不改 | 单个 `BEGIN IMMEDIATE` 事务完成恢复/删除/进度回退/`revoked` 标记，异常整体回滚 |
| 撤销后不可再撤销本批或更早批次 | 已撤销批次再撤销返回 409；更早批次因不是“最近一次成功批次”返回 409 |
| 采集与撤销全程互斥，重叠拒绝后发请求 | 单进程共用一把 `threading.Lock`，从读取进度到结束持锁，冲突返回 409 |
| 升级时保留已有目录和进度 | `init_db` 只 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN`，不改动既有数据；旧批次无快照不可撤销 |
| Docker Compose 启动服务和本地分页样例源 | `docker compose up --build` 启动 `harvester-api` + `oai-source` |
| Swagger 发起采集并查询目录和进度 | http://localhost:8000/docs |

## 目录结构

```
app/                  FastAPI 采集服务
  main.py             路由（采集/撤销/目录/进度/运行历史/健康检查）
  harvester.py        采集与撤销编排：翻页、重抓、整批事务、进程互斥锁
  oai_client.py       OAI-PMH 2.0 客户端（httpx + xml.etree）
  db.py               SQLite 表结构、增量迁移、快照与恢复事务
  schemas.py          Pydantic 请求/响应模型
  Dockerfile
sample-oai/           本地分页样例 OAI-PMH 源（纯标准库）
  server.py           27 条样例记录（rec-013 为删除记录），PAGE_SIZE=10，3 页
  Dockerfile
tests/                pytest 端到端测试（28 个用例，含撤销场景）
scripts/              辅助脚本（故障回滚演示）
docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集服务 API / Swagger：http://localhost:8000/docs
- 样例 OAI 源：http://localhost:8080/oai?verb=Identify
- SQLite 数据保存在命名卷 `harvester-data`（容器内 `/data/harvester.db`）

## 操作流程（Swagger 或 curl）

### 1. 首次采集：另填起始日，起止日都包含

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-15"}'
```

响应：

```json
{
  "status": "success",
  "from_date": "2026-10-01",
  "until_date": "2026-10-15",
  "pages": 2, "fetched": 15,
  "inserted": 14, "updated": 0, "withdrawn": 1,
  "retries": 0, "run_id": 1
}
```

### 2. 后续采集：只填截止日，自动从上次截止日（含）续采

```bash
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"until": "2026-10-27"}'
# 实际区间：2026-10-15 ~ 2026-10-27（last_until 含当天，避免边界漏采）
```

### 3. 查询目录与进度

```bash
curl 'http://localhost:8000/api/v1/catalog?status=active'   # 在架（默认）
curl 'http://localhost:8000/api/v1/catalog?status=all'      # 含已撤下
curl 'http://localhost:8000/api/v1/catalog/oai:sample.lib:rec-013'
curl 'http://localhost:8000/api/v1/progress'                # 上次成功截止日
curl 'http://localhost:8000/api/v1/runs'                    # 采集运行历史
```

### 4. 无匹配也推进截止日

对没有任何记录的区间发起采集，返回 `fetched=0` 且 `last_until` 照常推进。

### 5. 撤销最近一次成功补采

源馆误发书目时，管理员传入采集运行编号撤回**最近一次成功的补采**（之后失败的批次不影响撤销）：

```bash
# 先在 /api/v1/runs 中查到要撤销的运行编号（第二批 run_id=2）
curl -X POST http://localhost:8000/api/v1/runs/2/revoke
```

响应：

```json
{
  "run_id": 2,
  "status": "revoked",
  "restored": 1,
  "deleted": 5,
  "prev_last_until": "2026-10-15",
  "revoked_at": "2026-11-12T00:05:54Z"
}
```

撤销在**同一个 SQLite 事务**内完成：

- `restored`：本批改写过的书目整行恢复到批前的内容与状态（含 active↔withdrawn）；
- `deleted`：本批新增的记录（含 withdrawn 墓碑）直接移除；
- 续采进度回退到批前；撤销**首次**补采后进度行被删除，`GET /progress` 返回
  `{"last_until": null, "last_harvest_at": null}`，即显示未采集；
- 无匹配但推进了日期的批次同样可撤销，此时 `restored=0、deleted=0`，只回退截止日；
- 运行历史中该批次标记为 `revoked`。任一步失败整体回滚，目录、进度、历史都不改。

撤销的限制（均返回 `409`，运行不存在返回 `404`）：

| 场景 | 结果 |
| --- | --- |
| 撤销的不是最近一次成功批次 | 409，提示最近可撤销批次编号 |
| 该批次已撤销，再次撤销 | 409 |
| 撤销升级前的旧批次（无快照） | 409 |
| 采集/撤销正在进行时又发采集或撤销 | 409，后发请求被拒（单进程互斥） |

撤销之后源馆修正书目，可重新发起补采；新批次成为新的最近成功批次，仍可撤销。


## 令牌失效重抓的验证方法

样例源提供故障注入端点：

```bash
# 令下一次 resumptionToken 请求返回 badResumptionToken
curl -X POST http://localhost:8080/admin/fail-next
curl http://localhost:8080/admin/state

# 全区间 27 条共 3 页：第 1 页取到后续页令牌失效 -> 丢弃该页 -> 原区间重抓成功
curl -X POST http://localhost:8000/api/v1/harvest \
  -H 'Content-Type: application/json' \
  -d '{"from": "2026-10-01", "until": "2026-10-27"}'
# 响应中 retries=1；运行历史与服务日志可查
curl -X POST http://localhost:8080/admin/reset   # 清除故障开关
```

若重抓过程中令牌**再次**失效（不可恢复），接口返回 `502`，本批所有书目变更回滚、
截止日保持不变，`harvest_runs` 中记录一条 `failed` 历史——任何半批数据都不会进库。

## 事务与一致性说明

- 采集过程先在内存中收集全部页面，**不向 SQLite 写任何数据**；
- 全部页成功后，在单个 `BEGIN IMMEDIATE` 事务内完成批前快照、upsert、撤下、
  截止日推进、运行历史更新；事务中任何一步失败都会 rollback，库状态回到采集前；
- 撤销同样在单个 `BEGIN IMMEDIATE` 事务内完成书目恢复/删除、进度回退、
  运行标记 `revoked`、快照清理，任一步失败整体回滚；
- 进程内一把互斥锁同时保护采集与撤销（单进程部署）：补采从读取进度到结束
  全程持锁，与撤销互斥，重叠请求后发者返回 `409`；
- 升级旧库时只做 `CREATE TABLE IF NOT EXISTS` 与 `ALTER TABLE ADD COLUMN`，
  已有目录与进度原样保留；升级前写入的批次 `snapshot_version` 为空，不可撤销。

## 本地开发（无 Docker）

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# 终端 1：样例 OAI 源
python3 sample-oai/server.py

# 终端 2：API
APP_DB_PATH=./data/harvester.db OAI_ENDPOINT=http://127.0.0.1:8080/oai \
  uvicorn app.main:app --reload

# 测试
pytest
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OAI_ENDPOINT` | `http://oai-source:8080/oai` | OAI-PMH 端点 |
| `APP_DB_PATH` | `/data/harvester.db` | SQLite 数据库路径 |
| `PAGE_SIZE`（样例源） | `10` | 样例源每页记录数 |
| `TOKEN_TTL`（样例源） | `3600` | 样例源 resumptionToken 有效期秒数 |
