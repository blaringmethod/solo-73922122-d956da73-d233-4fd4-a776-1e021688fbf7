# 理赔明细分批提取与结算核对服务（FastAPI + PostgreSQL）

保险精算离线测算场景：按地区开启**快照会话**，按理赔 id 升序分页提取明细；
数据员收到地区结算流水后，在入仓前调用 **POST /settlement-checks**，
按同一会话快照逐笔核对流水是否足额覆盖。
会话期间保单/理赔两表固定为开启时刻的同一快照，源端的增改删照常提交、
互不影响；会话有数量上限与 TTL，可主动关闭，关闭/到期后未开始响应的取页
与核对一律失败且不返回明细/结论。

## 一、快速启动（Docker Compose）

```bash
docker compose up --build
```

- API：<http://localhost:8000>
- Swagger 交互文档：<http://localhost:8000/docs>
- OpenAPI 原文：<http://localhost:8000/openapi.json>
- PostgreSQL：localhost:55432（库 `claims`，用户/密码 `actuary/actuary`）

首次启动时 `db/init/` 下脚本按文件名顺序自动执行：

| 文件 | 作用 |
| --- | --- |
| `01_schema.sql` | 建表：`policies(id, region)`、`claims(id, policy_id, amount)`，金额为非负整数（单位：分） |
| `02_seed.sql` | 演示数据：3 个地区、260 条理赔（华北 120 / 华东 100 / 华南 40） |
| `03_sql_examples.sql` | SQL 补录示例：新增保单/理赔、改金额、删除、按地区汇总 |

## 二、核心语义与实现方式

| 需求 | 实现 |
| --- | --- |
| 两表同一快照、后页/重复页一致 | 每会话独占一条连接，开启 `START TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY`，连接保持 idle-in-transaction，快照事务全程不结束 |
| 源端增改删照常提交 | 写操作走连接池内其它自动提交连接，MVCC 下与快照只读事务互不阻塞 |
| 不预存结果全集 | 每次取页现场执行带 `LIMIT/OFFSET` 的 SQL，服务端不缓存明细 |
| claim_id 升序、每页至多 100 行 | `ORDER BY c.id ASC LIMIT n+1 OFFSET ...`，多取 1 行判断 `has_more`；`page_size` 服务端强制截断为 100 |
| 最多 2 个会话，满额拒绝 | 内存注册表 + 名额计数，超额返回 `429 capacity_reached`（可用 `MAX_SESSIONS` 调整） |
| 开启 60 秒到期 | 后台 sweeper 线程定时清理（`SESSION_TTL_SECONDS` 可调），到期取页返回 410/409 且无明细 |
| 主动关闭及时受理 | 关闭请求先把状态翻为 `closing`（新取页立即失败），等待在途取页结束后回滚事务、归还连接、释放名额，再回报 `closed` |
| 不影响另一会话/后续新会话 | 名额只统计 open/closing 会话；终态不占名额，连接归还池后立即可复用 |
| 连接丢失/服务重启后旧会话失效 | 会话状态只存进程内存；连接断裂的会话作废返回 503 且绝不换快照续取；重启后旧 id 查询 404 |
| 结算核对不缓存地区理赔全集 | `/settlement-checks` 现场执行 `unnest` 数组 + FULL OUTER JOIN，只把本次提交的 `claim_id/净额` 作为参数传入；进程内不保留全集，重复核对均重新查快照 |
| 核对期间关闭/到期/断连必须失败 | 核对在状态闸门 + 会话 `io_lock` 下三次复核 open/TTL；失败不换快照续算且不返回差异结论 |

### 会话状态机

```
open ──主动关闭──▶ closing ──回滚事务/还连接/释放名额──▶ closed
  │                                                  ▲
  └──TTL 到期（sweeper）─────────────────────────── expired
```

终态记录保留 10 分钟供状态查询，之后从注册表回收；终态不占用会话名额。

## 三、API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sessions` | 按地区开启快照会话，body：`{"region": "华北"}` |
| GET | `/sessions` | 列出会话 |
| GET | `/sessions/{id}` | 查询会话状态 |
| POST | `/sessions/{id}/close` | 主动关闭 |
| GET | `/sessions/{id}/claims?page=1&page_size=100` | 快照内分页取理赔明细 |
| POST | `/settlement-checks` | 提交结算流水，按会话快照逐笔核对（见第四节） |
| POST | `/admin/policies` | 源端补录保单 |
| POST | `/admin/claims` | 源端补录理赔 |
| PATCH | `/admin/claims/{id}?amount=999` | 源端改金额 |
| DELETE | `/admin/claims/{id}` | 源端删除理赔 |
| GET | `/health`、`/meta` | 健康检查、服务参数 |

取页失败响应只含 `detail`/`code`，不含任何明细行：

```json
HTTP 409
{"detail": "会话 ... 关闭已受理或会话已结束，取页失败且不返回明细",
 "code": "session_not_open"}
```

## 四、结算核对 POST /settlement-checks

数据员提交有效地区会话 ID 与至多 **200 条**按发生顺序排列的交易，
服务端按理赔 ID 汇总净支付，与该会话快照逐笔核对。

### 4.1 交易模型（判别联合，字段 `type` 区分）

| type | 必备字段 | 说明 |
| --- | --- | --- |
| `payment` | `transaction_id`、`claim_id`、`amount` | 支付；交易 ID 全批唯一，金额为非负整数（分） |
| `reversal` | `transaction_id`、`claim_id`、`original_transaction_id` | 全额冲正；**不携带金额**，被冲正支付全额剔除 |

冲正只能引用**本批先前**出现、**尚未被冲正**、且**同一理赔 ID** 的支付。

### 4.2 整批拒绝（HTTP 422，不返回任何部分结论）

| code | 触发条件 |
| --- | --- |
| `duplicate_transaction_id` | 任两条交易（含冲正自身）的 `transaction_id` 重复 |
| `invalid_reversal` | 冲正引用的原交易不存在/出现在其后、已被冲正、或理赔不一致 |
| 请求体校验错误 | 超过 200 条、金额为负、字段缺失/多余、未知 `type` 等 |

### 4.3 差异分类（按理赔 ID 与快照 FULL OUTER JOIN）

| difference_type | 判定 |
| --- | --- |
| `unsettled` 未结算 | 快照存在该理赔，但本批净支付为 0（未支付或支付已被全额冲正） |
| `underpaid` 少付 | 净支付 < 快照金额（`difference_amount` 为负） |
| `overpaid` 超付 | 净支付 > 快照金额（`difference_amount` 为正） |
| `orphan` 无源理赔 | 本批有净支付，但快照中不存在该理赔（含非本会话地区的理赔） |

- 回执给出四类差异的**总笔数**（SQL 窗口函数在 `LIMIT` 前统计，不受截断影响），
  以及按理赔 ID 升序的**前 30 条**差异明细和 `differences_truncated` 截断标记。
- 四类互斥并覆盖全部差异；`total_difference_count = 0` 时
  `ready_for_warehouse = true`，否则为 `false`。
- 空批（`transactions: []`）合法：快照内全部理赔判为未结算。

### 4.4 会话与快照语义（与取页完全一致）

| 需求 | 实现 |
| --- | --- |
| 源端更正不改变本次核对依据 | 核对在会话开启时冻结的 RR 只读快照上执行，源端改金额/删除/补录均不可见 |
| 不在内存缓存地区理赔全集 | 每次核对现场执行 SQL（`unnest` 数组 + FULL OUTER JOIN），进程不保留任何全集；测试 `test_no_region_claim_set_cached_in_process_memory` 守护 |
| 关闭受理/到期/连接失效须失败 | 进入前、拿到连接后、SQL 返回后三次状态/TTL 复核；失败返回 409/410/503，**不换快照续算**，响应不含 `differences` 等结论字段 |
| 未知会话 | 404 `session_not_found` |

请求示例：

```json
POST /settlement-checks
{
  "session_id": "8d2be222…",
  "transactions": [
    {"type": "payment",  "transaction_id": "t1", "claim_id": 221, "amount": 2800},
    {"type": "payment",  "transaction_id": "t2", "claim_id": 221, "amount": 500},
    {"type": "reversal", "transaction_id": "r1", "claim_id": 221,
     "original_transaction_id": "t2"}
  ]
}
```

## 五、慢取页/慢核对演示

镜像默认 `ENABLE_DEBUG_HOOKS=true`，带 `X-Debug-Sleep-Seconds` 请求头
可让本次取页或结算核对在执行 SQL 前先睡眠，用于演示"关闭及时受理"：

```bash
python3 demo/demo.py                 # 完整端到端演示（标准库即可运行）
BASE_URL=http://localhost:8000 python3 demo/demo.py
DEMO_TTL_WAIT=1 python3 demo/demo.py # 追加等待到期场景（约多花 1 分钟）
```

演示覆盖：分页与重复取页一致性、源端写入的快照隔离、名额满 429、
慢取页期间关闭（在途请求结束后失败、无明细）、到期失效，以及结算核对的
足额覆盖/四类差异/前 30 条截断、冲正剔除、重复 ID 与非法冲正整批拒绝、
慢核对与关闭交错（在途核对失败且无结论）。

## 六、本地自测（无 Docker 时）

仓库测试以真实 uvicorn + PostgreSQL 运行：

```bash
pip install -r requirements.txt
# 准备一个 PostgreSQL 15，并执行 db/init/01_schema.sql、02_seed.sql
DATABASE_URL=postgresql://actuary:actuary@127.0.0.1:5432/claims \
SESSION_TTL_SECONDS=2 ENABLE_DEBUG_HOOKS=true \
python -m unittest tests.test_api -v
```

29 个用例覆盖：分页/上限/参数校验、重复取页同快照、两表 JOIN 快照、
源端增改删不可见、名额与释放、关闭幂等、慢取页+关闭竞争、
到期清理、连接丢失作废不续取、服务重启旧会话 404，以及结算核对的
足额覆盖、四类差异分类与前 30 条截断、空批、重复 ID/非法冲正整批拒绝、
200 条上限、关闭/到期/连接失效与核对交错、源端更正不改变核对依据、
进程不缓存地区理赔全集。

## 七、配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://actuary:actuary@db:5432/claims` | 数据库连接串 |
| `MAX_SESSIONS` | `2` | 并发会话上限 |
| `SESSION_TTL_SECONDS` | `60` | 会话存活秒数 |
| `PAGE_SIZE_CAP` | `100` | 单页行数硬上限 |
| `SETTLEMENT_MAX_TRANSACTIONS` | `200` | 单次结算核对受理的交易条数上限 |
| `SETTLEMENT_DIFF_LIMIT` | `30` | 结算差异明细返回条数上限（按理赔 id 升序） |
| `ENABLE_DEBUG_HOOKS` | `false`（compose 中为 true） | 是否允许慢取页/慢核对调试头 |
| `SWEEPER_INTERVAL_SECONDS` | `0.25` | 到期检查间隔 |

> 说明：服务固定单 uvicorn worker——会话注册表与名额是进程内状态，
> 多 worker 会各自持有独立状态，破坏会话语义。
