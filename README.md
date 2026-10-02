# 港口潮汐通航行程规划服务

基于 **Python + FastAPI + SQLite** 的潮汐窗口航路规划服务。录入港口节点、单向航段
（正整数分钟航时、静水深度、可选桥底净空）与逐分钟潮位后，给定船舶吃水、水面以上
高度、安全余量及不超过 1440 分钟的规划窗口，计算**最早抵达**行程；同一抵达时刻按
**航段编号序列字典序**择优。规划同时执行**航段独占**：两艘已确认船舶不得在同一航段
的同一分钟占用（每段从进入分钟 `d` 到抵达前一分钟 `d+t-1` 独占；节点等待不占航段），
既有已确认快照即使彼此重叠也原样保留、作为后续规划必须避开的占用。潮位以**修订号**
版本化管理，分两类：

- **名义潮位更正**（`POST /api/tide/corrections`）：以某修订为基础全量继承名义潮位
  **与误差**，仅覆盖给定分钟的名义潮位（被覆盖分钟的误差仍继承，新分钟误差为 0），
  生成新修订；
- **预测误差修订**（`POST /api/tide/error-revisions`）：潮位预测存在**逐分钟误差**，
  调度员按「非负整分钟 → 非负误差（米）」提交更改；**名义潮位保持不变**，未列出分钟
  继承基础修订误差；旧修订一律视为误差 **0**。

修订号自增、内容**不可变**。核验水深/净空时按修订的**误差带取保守极值**：

- 水深：`静水深度 + 名义潮位 − 误差 ≥ 吃水 + 安全余量`（真实潮位可能偏低）
- 桥高：`桥底净空 − 名义潮位 − 误差 ≥ 水面以上高度 + 安全余量`（真实潮位可能偏高）
- 等待不占用航段，不校验水深/净空。

确认行程时在同一数据库事务中复核修订号与行程，过期即冲突且不保存，已确认行程作为
不可变快照长期保留。潮位更正（或误差修订）后，调度员可凭**已确认行程号 + 目标潮位
修订号**复核原航路：按快照原占用分钟逐段用误差带校验水深与桥高；原行程不安全时，
在保持航段编号顺序、船舶参数、原起始分钟与规划窗口不变、仅允许节点重新等待的前提下
求**改期行程**（变动航段最少 → 抵达最早 → 出发分钟序列字典序），窗口内无解则只返回
风险与无解标记，**绝不修改已确认快照**。

**本轮新增同修订批次**：调度员可把**同一潮位修订**下按优先顺序排列的 **2～5 艘船**
作为一个批次**预排**（`POST /api/trips/batch/plan`，只读不占位）——逐艘沿用单船
最早抵达 + 同刻航段序列字典序择优，后船同时避开**全部已确认占用**与**本批前船的拟
占用**，任一船无解即返回首个失败序号；随后用**批次幂等键**一次性**确认整批**
（`POST /api/trips/batch/confirm`）：单事务内核订最新修订与当时占用、逐艘复算并
逐段比对，过期 `REVISION_STALE`、任一船无解或不一致 `PLAN_MISMATCH` 且**全批不
保存**；通过则一次写入全部不可变快照。同键同内容重试返回原结果（`replayed=true`，
即使修订已更新），同键异内容 `BATCH_KEY_CONFLICT`。旧单船接口语义完全不变。

---

## 1. 启动

### 方式一：Docker Compose（推荐，一键启动并初始化数据库）

```bash
docker compose up --build -d
```

服务启动时自动：

1. 在命名卷 `tide-data` 中创建 SQLite 数据库 `/data/tide.db`；
2. 建表；
3. 若库为空（`SEED_DEMO=true`，默认），写入演示数据：
   - 节点 `A（外锚地）、B、C、D（内港码头）`
   - 航段 `1:A→B(30min,深6m)`、`2:B→C(40min,深4m,净空8m)`、`3:C→D(20min,深5m)`、`4:B→D(90min,深3.5m)`
   - 潮位修订 `#1`：分钟 `0..1439`，名义潮位 `2.0 + 1.5·sin(2π·m/720)`（米），
     预测误差 0（可随后经 `/api/tide/error-revisions` 登记逐分钟误差）

查看日志 / 停止：

```bash
docker compose logs -f
docker compose down            # 停止（保留数据卷）
docker compose down -v         # 停止并删除数据库卷（下次启动重新初始化）
```

### 方式二：Docker

```bash
docker build -t tide-planner .
docker run -d --name tide-planner -p 8000:8000 \
  -v "$PWD/data:/data" -e DB_PATH=/data/tide.db tide-planner
```

### 方式三：本地 Python（3.11+）

```bash
pip install -r requirements.txt
DB_PATH=./tide.db SEED_DEMO=true uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 地址与端口

| 项目 | 值 |
| --- | --- |
| 服务地址 | http://localhost:8000 |
| 健康检查 | http://localhost:8000/health |
| Swagger UI（可在线调用） | http://localhost:8000/docs |
| OpenAPI 文档 | http://localhost:8000/openapi.json |

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/tide.db` | SQLite 数据库文件路径，所在目录自动创建 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `SEED_DEMO` | `true` | 空库启动时是否写入演示节点/航段/初始潮位修订 |
| `MAX_WINDOW` | `1440` | 规划窗口上限（分钟） |

---

## 2. 领域规则

- **节点**：港口位置点。**航段为单向**，航时必须为**正整数**分钟；可设桥底净空（不设表示无桥）。
- **时间轴**：绝对"分钟"（非负整数），与潮位表的 `minute` 对齐。规划窗口为
  `[start_minute, start_minute + window_minutes)`，长度 ≤ 1440。
- **等待**：船可在任一节点的任意整分钟等待（等待不占用水深/净空资源，**不参与校验**，
  也**不占用航段**）。
- **航段占用（独占）**：于整分钟 `d` 进入、航时 `t`，则占用 `d, d+1, …, d+t-1` 共
  `t` 分钟，于 `d+t` 抵达；**抵达分钟 `d+t` 已不占用该航段**（后船可在该分钟进入）。
  - 同一 `(航段, 分钟)` 同一时刻只允许一艘已确认船舶占用；不同航段互不相干。
  - **已确认快照即占用事实**：规划与确认重算必须避开所有既有已确认快照的占用；
    既有快照**即使彼此重叠也原样保留**（历史数据不清理、不改写），重叠分钟对后续
    规划仍然封闭。
  - **规划接口只读、不占位**：调用 `/api/trips/plan` 不产生任何占用，只有
    `/api/trips/confirm` 成功后才生成占用。
  每个修订的逐分钟数据含**名义潮位 `level`** 与**非负预测误差
  `error`（米）**，真实潮位落在 `[level−error, level+error]`。占用的**每一分钟**都
  必须同时满足（按误差带取对应极值）：
  - 水深：`静水深度 + 名义潮位 − 误差 ≥ 吃水 + 安全余量`
  - 桥高（仅当该航段有桥）：`桥底净空 − 名义潮位 − 误差 ≥ 水面以上高度 + 安全余量`
  - 独占：该 `(航段, 分钟)` 未被其他已确认快照占用
- **择优**：到达分钟最小；同一到达分钟，按航段编号（`seg_id`，自增）组成的序列做
  字典序比较，序列小者优先（先比较首段编号，相同再比较下一段）。
- **潮位修订**：修订号自增、内容不可变，分两种提交方式（均可省略 `base_revision`
  而默认基于**最新**修订；未知基础修订报 `TIDE_REVISION_NOT_FOUND`）：
  - **名义潮位更正** `/api/tide/corrections`：全量继承基础修订的名义潮位与误差，
    仅覆盖给定分钟的名义潮位（被覆盖分钟**继承误差**；基础修订没有的分钟作为新
    分钟补入、误差取 0）；
  - **预测误差修订** `/api/tide/error-revisions`：载荷为 `{分钟: 非负误差}`，
    **名义潮位不变**；未列出分钟**继承**基础修订的误差，列出分钟覆盖为给定误差；
    误差必须非负（否则 `422 VALIDATION_ERROR`）；所提交分钟在基础修订中必须已有
    名义潮位，否则 `422 MISSING_TIDE_DATA`（误差修订不能凭空新增名义潮位）。
  - **旧修订视为误差 0**：早于误差功能的修订（含旧版数据库升级后的既有行）均无
    误差数据，按误差 0 处理；规划/复核目标窗口内任一分钟缺**名义潮位**即报
    `MISSING_TIDE_DATA`。
- **规划**：默认使用**最新**修订，也可显式指定修订号。窗口内任何一分钟缺少名义
  潮位即报错。水深/净空均按所据修订的误差带保守极值核验，且每个出发区间都不得与
  既有已确认快照的航段占用重叠（重叠即视为该进入分钟不可行，等价于低潮/净空不足）。
- **确认**：请求必须携带规划所据 `revision`。服务在单个 SQLite 事务（`BEGIN IMMEDIATE`）中：
  1. 检查该修订是否仍为最新——否则 `409 REVISION_STALE`，**不保存**；
  2. 依据该修订与**当时全部已确认占用**（同一事务内读取，既有快照即使重叠也计入；
     本行程尚未落库故不包含自身）**重新计算**——若窗口内因占用变化已无可行行程，
     或重算结果与提交行程逐段不一致，均为 `409 PLAN_MISMATCH`，**不保存**；
  3. 复核通过后保存**当时的结果快照**（修订号、参数、到达分钟、完整行程 JSON）。
     之后再发生潮位更正不影响已确认行程；快照的占用也随之永久生效，后续规划/确认
     必须避开（快照彼此重叠亦原样保留）。
- **批次预排**（同一潮位修订下 2～5 艘船）：
  - `ships` 按**优先顺序**给出（2～5 艘，数量越界或任一船字段非法均
    `422 VALIDATION_ERROR`）；整批共用一个 `revision`（可省略，省略时解析为
    **最新**修订，预排响应回带实际所据修订号）。
  - 逐艘调用与单船完全相同的规划（最早抵达 + 同刻航段编号序列字典序）；第 i 艘船
    须避开的占用 = 窗口覆盖区间内的**全部已确认快照占用** ∪ 本批第 0..i-1 艘船的
    **拟占用**（与已确认占用同口径：进入分钟 `d` 占 `d..d+t-1`，抵达分钟不占）。
  - 预排**只读、不占位**：不写库、不产生占用，可任意重复调用；每次调用内部从第 0 艘
    重新串行规划。
  - 任一船规划失败时按**首个失败序号**（`details.ship_index`，0 起）返回，沿用既有
    错误码与状态：`NODE_NOT_FOUND`(404) / `MISSING_TIDE_DATA`(422) /
    `NO_FEASIBLE_ITINERARY`(404)。
- **批次确认**：请求携带**预排所据修订号**（必填）、各船按预排结果原样回填的
  `itinerary` 与**批次幂等键** `idempotency_key`（非空字符串，≤128 字符）。服务在
  单个 SQLite 事务（`BEGIN IMMEDIATE`）中：
  1. 幂等键已存在且内容（修订号 + 各船参数与回填行程的规范化哈希）一致 ->
     **直接返回首次结果**（HTTP 200、`replayed=true`），**不重新计算、不新增快照**，
     即使其间修订已更新或占用已变化；同键异内容（含不同修订号）->
     `409 BATCH_KEY_CONFLICT`；
  2. 新键：先核订修订仍为最新，否则 `409 REVISION_STALE`，全批不保存；
  3. 同一事务读取**当时全部已确认占用**，按请求顺序逐艘复算：窗口内无解 ->
     `409 PLAN_MISMATCH`（`details.ship_index` 指出首个失败序号、
     `reason=no_feasible_itinerary`）；复算行程与提交行程逐段不一致（或抵达分钟
     不一致）-> `409 PLAN_MISMATCH`（`reason=itinerary_mismatch`）；前船的**复算**
     行程随即并入后船的当时占用；任一船失败**全批回滚、不保存任何快照**；
  4. 全部通过后在同一事务内一次写入各船不可变快照（与单船确认写入同一张
     `confirmed_trips` 表，立即可经 `/api/trips` 读取、占用立即生效）并登记幂等记录，
     返回各船 `index / trip_id / arrival_minute / itinerary`。
- **更正后复核 / 改期**（只读，不改写快照、建议不占位）：携带已确认行程号与目标
  修订号（名义更正或误差修订均可），
  1. **原航路复核**：**只按目标修订检查潮位**——按快照中的航段编号顺序与原占用分钟
     （进入分钟 `d` 占用 `d..d+t-1`），依目标修订逐分钟的名义潮位与**误差带**逐段
     校验（水深用 `名义−误差`、净空用 `名义+误差` 的保守极值）；**不检查**与其他
     已确认快照的占用重叠（既有快照彼此重叠原样保留，不构成潮位风险）。首分钟违规即
     判定原行程不安全，返回首次违规的航段（`leg_index`/`seg_id`）、分钟及原因
     （`depth` 水深不足 / `clearance` 桥高净空不足），并给出该分钟实际/要求的水深与
     净空——实际值按**对应极值**计算
     （`available_depth = 静水深度 + 名义潮位 − 误差`、
     `available_clearance = 桥底净空 − 名义潮位 − 误差`），另返回
     `tide_level`（名义潮位）与 `tide_error`（误差）；
  2. **改期**：不安全时启用，仍按原规则（误差带）求可行进入分钟。**保持**原航段
     编号顺序、船舶参数（吃水/水面以上高度/余量）、原起始分钟与规划窗口（窗口内
     占用才合法），**仅允许在节点重新等待**——即各段首尾相接、只调整每段出发分钟。
     改期建议还须**避开其他已确认快照的航段占用，但排除自身快照**（自身原占用不挡
     自己；建议本身不占位、不新增确认）。择优依次为：
     ① 相对快照**出发分钟变动的航段个数**（`changed_leg_count`）最少；
     ② **抵达分钟最早**；③ 前述均相同，按各段出发分钟序列做字典序（小者优先）；
  3. 原行程安全时直接返回 `safe=true`（零航段行程恒安全，无需改期）；潮位可行但
     所有可行进入区间均被其他快照占满、窗口内无改期解时返回原行程风险加
     `no_solution=true`，此时仍可在 `/api/trips` 读到原封不动的快照。缺名义潮位、
     未知行程、未知修订沿用 `MISSING_TIDE_DATA` / `TRIP_NOT_FOUND` /
     `TIDE_REVISION_NOT_FOUND` 既有错误语义。

### 错误响应格式

```json
{ "error": { "code": "NODE_NOT_FOUND", "message": "未知节点: ZZ", "details": {"node_id": "ZZ"} } }
```

| 场景 | HTTP | code |
| --- | --- | --- |
| 起点或终点未知 | 404 | `NODE_NOT_FOUND` |
| 指定潮位修订不存在 / 复核时目标修订未知 / 误差修订的基础修订未知 | 404 | `TIDE_REVISION_NOT_FOUND` |
| 复核时行程号未知 | 404 | `TRIP_NOT_FOUND` |
| 窗口内有分钟缺名义潮位 / 误差修订提交分钟在基础修订无名义潮位 | 422 | `MISSING_TIDE_DATA`（`details` 给出缺失分钟列表与数量） |
| 窗口内无可行行程（含所有潮位可行区间均被已确认占用占满） | 404 | `NO_FEASIBLE_ITINERARY` |
| 参数校验失败（窗口>1440、航时非正、误差为负、分钟为负等） | 422 | `VALIDATION_ERROR` |
| 确认时修订已过期（单船与批次一致） | 409 | `REVISION_STALE` |
| 确认时行程复核不一致（潮位重算结果不同，或占用变化使提交行程不再最优/已无可行解） | 409 | `PLAN_MISMATCH` |
| 批次预排中任一船失败（未知节点/缺潮位/无可行行程） | 同既有码 | 既有码，`details.ship_index` 给出首个失败序号（0 起） |
| 批次确认中任一船无解或与复算不一致 | 409 | `PLAN_MISMATCH`（`details.ship_index`/`reason`，全批不保存） |
| 同批次幂等键提交了不同内容（或不同修订号） | 409 | `BATCH_KEY_CONFLICT`（不保存） |
| 批次船数不在 2～5、幂等键缺失/过长、字段非法 | 422 | `VALIDATION_ERROR` |

---

## 3. HTTP API

> **本轮用法（航段独占占用）**：API 路径与请求/响应结构与上一版完全一致，无需新增
> 端点——
> - `POST /api/trips/plan`：规划结果自动避开当时全部已确认快照的占用（**只读，不占位**）；
> - `POST /api/trips/confirm`：在同一事务内以**最新潮位修订 + 当时占用**重新计算，
>   修订过期沿用 `409 REVISION_STALE`，占用变化导致提交行程不再最优（或窗口内已无
>   可行解）沿用 `409 PLAN_MISMATCH`，冲突均不保存；
> - `POST /api/trips/{id}/review`：原快照仍只按目标修订的潮位误差带复核；需要改期时，
>   建议避开**其他**已确认快照占用（排除自身），保持原航段顺序与原择优规则，
>   **建议不占位、快照不改写**；窗口内无解沿用 `no_solution=true`。
> - **本轮新增批次端点**：`POST /api/trips/batch/plan` 按优先顺序预排 2～5 艘船
>   （后船避开已确认占用 + 本批前船拟占用，只读不占位，首个失败序号见
>   `details.ship_index`）；`POST /api/trips/batch/confirm` 在单事务内核订最新修订、
>   逐艘复算逐段比对，`REVISION_STALE` / `PLAN_MISMATCH` 全批不保存，成功凭
>   `idempotency_key` 一次写入全部快照，同键同内容重放原结果、同键异内容
>   `BATCH_KEY_CONFLICT`。
> - 启动方式不变（`docker compose up` / `docker run` / `uvicorn app.main:app`）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/nodes` | 录入节点 |
| GET | `/api/nodes` | 节点列表 |
| POST | `/api/segments` | 录入单向航段 |
| GET | `/api/segments` | 航段列表（按编号排序） |
| GET | `/api/tide/revisions` | 潮位修订列表 |
| GET | `/api/tide/revisions/{rev}?from_minute=&to_minute=` | 取某修订逐分钟名义潮位 `levels` 与预测误差 `errors` |
| POST | `/api/tide/corrections` | 名义潮位更正（继承误差），生成新修订 |
| POST | `/api/tide/error-revisions` | 预测误差修订（名义潮位不变），生成新修订 |
| POST | `/api/trips/plan` | 规划最早抵达行程 |
| POST | `/api/trips/confirm` | 携带修订号确认行程（事务复核） |
| POST | `/api/trips/batch/plan` | **本轮新增**：同修订 2～5 艘船按优先顺序批次预排（只读不占位，后船避前船拟占用） |
| POST | `/api/trips/batch/confirm` | **本轮新增**：携带修订号 + 幂等键批次确认（事务逐艘复算，全成全败、幂等重放） |
| POST | `/api/trips/{trip_id}/review` | 更正后按目标修订复核原行程，不安全时求改期 |
| GET | `/api/trips` | 已确认行程列表 |
| GET | `/api/trips/{trip_id}` | 已确认行程快照详情 |
| GET | `/health` | 健康检查 |

### 预测误差修订请求 / 响应示例

名义潮位不变，只登记逐分钟预测误差（米，非负）；未列出分钟继承基础修订误差，
旧修订视为误差 0：

```json
POST /api/tide/error-revisions
{
  "errors": {"100": 2.0, "101": 0.5},
  "base_revision": 1,
  "note": "人工修订：100/101 分钟预测误差"
}
```

- `errors`：`{非负整数分钟: 非负误差(米)}`，不能为空；分钟或误差为负返回
  `422 VALIDATION_ERROR`；
- `base_revision` 可省略，省略时基于**最新**修订；基础修订不存在返回
  `404 TIDE_REVISION_NOT_FOUND`；
- 提交的任一分钟在基础修订中没有名义潮位，返回 `422 MISSING_TIDE_DATA`，不产生
  新修订；
- 成功返回 `201` 与新修订号（与名义更正共用同一自增序列）：

```json
{ "revision": 3, "note": "人工修订：100/101 分钟预测误差", "created_at": "2026-10-01 08:00:00" }
```

随后 `GET /api/tide/revisions/3?from_minute=99&to_minute=102` 可同时读到
`levels`（名义潮位，与基础修订一致）与 `errors`（100=2.0、101=0.5、其余继承为 0）。

规划/确认/复核携带该修订号时，水深按 `名义−误差`、净空按 `名义+误差` 的保守极值
核验；确认冲突（`REVISION_STALE`）、择优（最早抵达 + 航段序列字典序）、响应字段
均与既有规则一致。

### 规划请求 / 响应示例

请求：

```json
POST /api/trips/plan
{
  "origin": "A",
  "destination": "D",
  "start_minute": 0,
  "window_minutes": 1440,
  "draft": 5.0,
  "height": 2.0,
  "margin": 0.5,
  "revision": 1
}
```

响应（每段给出 `seg_id` 与起止分钟；`end_minute` 为抵达下一节点的分钟）：

```json
{
  "revision": 1,
  "origin": "A",
  "destination": "D",
  "start_minute": 0,
  "window_minutes": 1440,
  "draft": 5.0,
  "height": 2.0,
  "margin": 0.5,
  "arrival_minute": 90,
  "total_wait_minutes": 0,
  "itinerary": [
    {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 0,  "end_minute": 30, "travel_time": 30},
    {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 30, "end_minute": 70, "travel_time": 40},
    {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 70, "end_minute": 90, "travel_time": 20}
  ]
}
```

### 确认请求 / 响应示例

将规划响应**原样回填**（含 `revision` 与每段 `seg_id/起止分钟`）即可：

```json
POST /api/trips/confirm
{
  "revision": 1, "origin": "A", "destination": "D",
  "start_minute": 0, "window_minutes": 1440,
  "draft": 5.0, "height": 2.0, "margin": 0.5,
  "itinerary": [
    {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 0,  "end_minute": 30, "travel_time": 30},
    {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 30, "end_minute": 70, "travel_time": 40},
    {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 70, "end_minute": 90, "travel_time": 20}
  ]
}
```

成功返回 `201`，响应为含 `trip_id / revision / arrival_minute / itinerary / confirmed_at`
的不可变快照；过期返回 `409 REVISION_STALE`。

### 批次预排 / 确认请求与响应示例（本轮新增）

同一潮位修订下，把按**优先顺序**排列的 2～5 艘船一次预排。每艘船的字段与单船
`/api/trips/plan` 完全一致；`revision` 提升到批次级（可省略，省略时取最新修订，
响应回带实际所据修订号）：

```json
POST /api/trips/batch/plan
{
  "revision": 1,
  "ships": [
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,"draft":5.0,"height":2.0,"margin":0.5},
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,"draft":5.0,"height":2.0,"margin":0.5}
  ]
}
```

响应为 `revision` + `ships`；每艘船带 `index`（0 起，即优先顺序）与单船规划同形的
行程结果。船 1 的结果会避开船 0 的**拟占用**（演示库中船 0 于第 90 分钟抵达，
船 1 整体后移，第 130 分钟抵达）：

```json
{
  "revision": 1,
  "ships": [
    {"index":0,"arrival_minute":90,"itinerary":[ /* seg1: 0..30, seg2: 30..70, seg3: 70..90 */ ]},
    {"index":1,"arrival_minute":130,"itinerary":[ /* seg1: 40..70, seg2: 70..110, seg3: 110..130 */ ]}
  ]
}
```

- 船数不在 2～5 或任一船字段非法（窗口 >1440、负吃水等）-> `422 VALIDATION_ERROR`；
- 任一船未知节点 / 窗口缺潮位 / 无可行行程 -> 沿用 `NODE_NOT_FOUND` /
  `MISSING_TIDE_DATA` / `NO_FEASIBLE_ITINERARY`，`details.ship_index` 给出首个
  失败序号（0 起）；
- 预排**不占位**，重复调用结果一致（前提是占用/修订未变）。

确认时回填各船预排行程，并额外提供**必填** `revision` 与批次**幂等键**
`idempotency_key`：

```json
POST /api/trips/batch/confirm
{
  "revision": 1,
  "idempotency_key": "tide-window-2026-10-02-am-batch",
  "ships": [
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
     "draft":5.0,"height":2.0,"margin":0.5,
     "itinerary":[ /* 预排 ships[0].itinerary 原样回填 */ ]},
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
     "draft":5.0,"height":2.0,"margin":0.5,
     "itinerary":[ /* 预排 ships[1].itinerary 原样回填 */ ]}
  ]
}
```

首次成功返回 `201`，一次写入整批不可变快照：

```json
{
  "idempotency_key": "tide-window-2026-10-02-am-batch",
  "revision": 1,
  "replayed": false,
  "trips": [
    {"index":0,"trip_id":7,"revision":1,"arrival_minute":90, "itinerary":[ /* … */ ]},
    {"index":1,"trip_id":8,"revision":1,"arrival_minute":130,"itinerary":[ /* … */ ]}
  ]
}
```

- **同键同内容重试**：返回 `200`、`replayed=true` 与**首次相同的各行程号**，
  服务不再复算、不新增快照——即使这期间潮位已生成新修订或占用已变化；
- **同键异内容**（改任一船参数/行程，或换修订号）：`409 BATCH_KEY_CONFLICT`，
  不保存；请换新幂等键；
- **修订过期**：`409 REVISION_STALE`（`provided_revision/current_revision`）；
- **复算无解或逐段不一致**：`409 PLAN_MISMATCH`，`details.ship_index` 指出首个
  失败船、`reason` 为 `no_feasible_itinerary` 或 `itinerary_mismatch`，**全批回滚**；
- 批次写入的每条快照都出现在 `GET /api/trips` / `GET /api/trips/{id}` 中，占用
  立即对后续单船/批次规划生效；旧单船 `/api/trips/plan`、`/api/trips/confirm`
  接口路径、字段与冲突语义均保持不变。

### 更正后复核 / 改期请求与响应示例

潮位发生更正后，凭**已确认行程号**与**目标潮位修订号**复核（`target_revision`
可省略，省略时取最新修订；请求体也可整体省略）：

```json
POST /api/trips/1/review
{ "target_revision": 2 }
```

**原行程安全**：`safe=true`，`first_violation/reschedule` 均为 `null`。

**原行程不安全且窗口内可改期**（下例为演示库中 `A→D`、吃水 6.5/船高 2.0/余量 0.5、
窗口 720 的已确认行程，第 100 分钟被更正为低潮 0.2m）：

```json
{
  "trip_id": 1,
  "snapshot_revision": 1,
  "target_revision": 2,
  "origin": "A", "destination": "D",
  "start_minute": 0, "window_minutes": 720,
  "draft": 6.5, "height": 2.0, "margin": 0.5,
  "original_arrival_minute": 144,
  "safe": false,
  "first_violation": {
    "leg_index": 1,
    "seg_id": 2,
    "origin": "B", "destination": "C",
    "minute": 100,
    "reasons": ["depth"],
    "tide_level": 0.2,
    "tide_error": 0.0,
    "depth": 4.0,
    "clearance": 8.0,
    "required_depth": 7.0,
    "required_clearance": 2.5,
    "available_depth": 4.2,
    "available_clearance": 7.8
  },
  "reschedule": {
    "revision": 2,
    "arrival_minute": 161,
    "total_wait_minutes": 71,
    "changed_leg_count": 2,
    "changed_legs": [
      {"leg_index": 1, "seg_id": 2, "original_departure_minute": 84,  "new_departure_minute": 101},
      {"leg_index": 2, "seg_id": 3, "original_departure_minute": 124, "new_departure_minute": 141}
    ],
    "itinerary": [
      {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 54,  "end_minute": 84,  "travel_time": 30},
      {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 101, "end_minute": 141, "travel_time": 40},
      {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 141, "end_minute": 161, "travel_time": 20}
    ]
  },
  "no_solution": false,
  "original_itinerary": [ /* 快照原行程，逐段原样返回 */ ]
}
```

要点：

- 第一段出发分钟仍是快照中的 `54`（**变动航段数最少**优先）；第二、三段因需避开
  低潮而整体后移，`changed_leg_count=2`，改期抵达 161。
- `first_violation` 按快照航段顺序、占用分钟递增给出**首次**违规；`reasons` 可能为
  `["depth"]`、`["clearance"]` 或二者同时出现。`tide_level`/`tide_error` 为该分钟
  目标修订的名义潮位与预测误差；`available_depth = depth + tide_level − tide_error`、
  `available_clearance = clearance − tide_level − tide_error`（无桥为 `null`），
  即按对应保守极值计算的实际可用值。
- **窗口内无改期解**：`reschedule=null` 且 `no_solution=true`，`first_violation`
  仍给出原行程风险；快照保持不变（`GET /api/trips/{trip_id}` 读到的仍是原内容）。
- 该接口为**只读**：不新增、不修改任何确认行程，也不产生新修订。

---

## 4. 调用示例（curl）

```bash
BASE=http://localhost:8000

# 健康检查
curl -s $BASE/health

# 查看演示航段
curl -s $BASE/api/segments

# 1) 规划
curl -s -X POST $BASE/api/trips/plan \
  -H 'Content-Type: application/json' \
  -d '{"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
       "draft":5.0,"height":2.0,"margin":0.5,"revision":1}' | tee /tmp/plan.json

# 2) 确认（规划响应可直接作为确认请求体）
curl -s -X POST $BASE/api/trips/confirm \
  -H 'Content-Type: application/json' -d @/tmp/plan.json

# 3) 潮位更正：把第 100、101 分钟改为低潮 0.2m（继承最新修订，生成新修订）
curl -s -X POST $BASE/api/tide/corrections \
  -H 'Content-Type: application/json' \
  -d '{"corrections":{"100":0.2,"101":0.2},"note":"人工低潮更正"}'

# 3b) 预测误差修订：第 100、101 分钟误差 0.3m（名义潮位不变，未列出分钟继承误差）
curl -s -X POST $BASE/api/tide/error-revisions \
  -H 'Content-Type: application/json' \
  -d '{"errors":{"100":0.3,"101":0.3},"base_revision":2,"note":"预报误差带修订"}'

# 查看某修订的名义潮位与误差
curl -s "$BASE/api/tide/revisions/3?from_minute=99&to_minute=102"

# 4) 用旧修订号再次确认 -> 409 REVISION_STALE
curl -i -X POST $BASE/api/trips/confirm \
  -H 'Content-Type: application/json' -d @/tmp/plan.json

# 5) 更正后复核已确认行程（目标修订取最新；first_violation 给首次违规）
curl -s -X POST $BASE/api/trips/$TID/review \
  -H 'Content-Type: application/json' -d '{"target_revision":2}'
# 请求体可省略（默认最新修订）：
curl -s -X POST $BASE/api/trips/$TID/review

# 6) 航段独占：第二艘同航路船规划时自动避开第一艘的占用（plan 只读不占位，
#    confirm 成功后该占用才生效）；用旧规划结果去确认会 409 PLAN_MISMATCH
curl -s -X POST $BASE/api/trips/plan -H 'Content-Type: application/json' \
  -d '{"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
       "draft":5.0,"height":2.0,"margin":0.5,"revision":1}' > /tmp/plan2.json
# /tmp/plan2.json 的出发分钟必然整体晚于已确认船（避开 seg1 的 0..29、seg2 的 30..69…）
curl -s -X POST $BASE/api/trips/confirm -H 'Content-Type: application/json' \
  -d @/tmp/plan2.json

# 7) 潮位修订后复核改期：改期建议避开其他已确认船的占用（排除被复核行程自身），
#    建议不占位、快照不改写
curl -s -X POST $BASE/api/trips/$TID/review -H 'Content-Type: application/json' -d '{}'

# 8) 本轮新增：同修订批次预排（2～5 艘，按优先顺序；后船自动避开前船拟占用，不占位）
curl -s -X POST $BASE/api/trips/batch/plan -H 'Content-Type: application/json' -d '{
  "revision": 1,
  "ships": [
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,"draft":5.0,"height":2.0,"margin":0.5},
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,"draft":5.0,"height":2.0,"margin":0.5}
  ]
}' > /tmp/batch_plan.json

# 9) 批次确认：把预排各船行程连同 revision 与批次幂等键一起提交（全成全败）
python3 - <<'PY'
import json
plan = json.load(open("/tmp/batch_plan.json"))
body = {
    "revision": plan["revision"],
    "idempotency_key": "demo-batch-0001",
    "ships": [
        {"origin": s["origin"], "destination": s["destination"],
         "start_minute": s["start_minute"], "window_minutes": s["window_minutes"],
         "draft": s["draft"], "height": s["height"], "margin": s["margin"],
         "itinerary": s["itinerary"]}
        for s in plan["ships"]
    ],
}
json.dump(body, open("/tmp/batch_confirm.json", "w"), ensure_ascii=False)
PY
curl -s -X POST $BASE/api/trips/batch/confirm \
  -H 'Content-Type: application/json' -d @/tmp/batch_confirm.json
# 同键同内容重试 -> 200 且 replayed=true、行程号不变；
# 同键异内容 -> 409 BATCH_KEY_CONFLICT；修订过期 -> 409 REVISION_STALE
curl -s -i -X POST $BASE/api/trips/batch/confirm \
  -H 'Content-Type: application/json' -d @/tmp/batch_confirm.json | head -1
```

Python 示例（规划 → 确认 → 更正 → 复核/改期）：

```python
import requests

B = "http://localhost:8000"
plan = requests.post(f"{B}/api/trips/plan", json={
    "origin": "A", "destination": "D",
    "start_minute": 0, "window_minutes": 720,
    "draft": 6.5, "height": 2.0, "margin": 0.5,
}).json()
trip = requests.post(f"{B}/api/trips/confirm", json=plan).json()
tid = trip["trip_id"]
print("已确认行程", tid, "第", trip["arrival_minute"], "分钟抵达")

# 潮位更正：第 100 分钟异常低潮
requests.post(f"{B}/api/tide/corrections", json={"corrections": {100: 0.2}})

# 预测误差修订：第 100 分钟名义潮位不变、误差 0.3m（旧修订与未列出分钟误差为 0）
r = requests.post(f"{B}/api/tide/error-revisions", json={"errors": {100: 0.3}})
print("误差修订号", r.json()["revision"])

# 复核原航路并取改期建议（只读，快照不变）
rv = requests.post(f"{B}/api/trips/{tid}/review", json={}).json()
if rv["safe"]:
    print("原行程在最新潮位下仍安全")
elif rv["no_solution"]:
    v = rv["first_violation"]
    print("窗口内无改期解；首次违规：航段", v["seg_id"], "分钟", v["minute"], v["reasons"])
else:
    rs = rv["reschedule"]
    print("改期：变动航段", rs["changed_leg_count"], "个，改至第",
          rs["arrival_minute"], "分钟抵达")
    print("新出发分钟：", [l["start_minute"] for l in rs["itinerary"]])
```

Python 示例（同修订批次预排 → 确认 → 幂等重试）：

```python
import requests

B = "http://localhost:8000"
ships = [
    {"origin": "A", "destination": "D", "start_minute": 0, "window_minutes": 1440,
     "draft": 5.0, "height": 2.0, "margin": 0.5},
    {"origin": "B", "destination": "D", "start_minute": 60, "window_minutes": 1380,
     "draft": 4.0, "height": 1.0, "margin": 0.3},
]

# 1) 批次预排（只读不占位；ships[1] 会避开 ships[0] 的拟占用）
plan = requests.post(f"{B}/api/trips/batch/plan",
                     json={"revision": 1, "ships": ships}).json()

# 2) 原样回填各船行程 + 修订号 + 批次幂等键，一次确认整批
confirm_body = {
    "revision": plan["revision"],
    "idempotency_key": "batch-2026-10-02-001",
    "ships": [
        {**ships[i], "itinerary": plan["ships"][i]["itinerary"]}
        for i in range(len(ships))
    ],
}
r = requests.post(f"{B}/api/trips/batch/confirm", json=confirm_body)
r.raise_for_status()
for t in r.json()["trips"]:
    print("船", t["index"], "-> 行程号", t["trip_id"],
          "抵达分钟", t["arrival_minute"])

# 3) 网络重试：同键同内容安全重放（即使期间已产生新修订）
retry = requests.post(f"{B}/api/trips/batch/confirm", json=confirm_body)
assert retry.status_code == 200 and retry.json()["replayed"] is True
```

录入自定义数据：

```bash
curl -s -X POST $BASE/api/nodes    -H 'Content-Type: application/json' -d '{"node_id":"E","name":"新码头"}'
curl -s -X POST $BASE/api/segments -H 'Content-Type: application/json' \
  -d '{"origin":"D","destination":"E","travel_time":15,"depth":4.2,"clearance":7.0}'
```

---

## 5. 算法说明

窗口不超过 1440 分钟，采用**时间展开动态规划**（实现见 `app/services/planning.py`）：

1. 校验节点与修订；取窗口内全部名义潮位与预测误差，任一分钟缺名义潮位立即报
   `MISSING_TIDE_DATA`；同时读取窗口内全部已确认快照的航段占用
   （`load_occupancy`，既有快照彼此重叠也原样计入）；
2. 对每条航段用"不可行分钟前缀和"预处理：任意进入分钟对应的占用区间是否全程满足
   误差带约束（水深 `深度+名义−误差`、净空 `净空−名义−误差`）**且无任何一分钟被
   其他已确认快照占用**，查询为 O(1)；
3. 状态 `(节点, 分钟)` 只保留一个值：到达该状态的航段编号序列中**字典序最小**者。
   转移有两种：
   - 等待：同一节点序列不变推进到下一分钟；
   - 出发：在该节点的整分钟进入可行航段，序列追加该航段编号；
4. 按分钟递增处理，终点第一次出现的分钟即最早抵达，其序列即同到达时刻字典序最优；
5. 回溯父指针，输出每段的进入/抵达分钟。

剪枝依据：若序列 `s1 < s2`（字典序），则对任意后缀 `x` 恒有 `s1+x < s2+x`，
故同一 `(节点, 分钟)` 只需保留最小序列，不会丢失最优解。复杂度 O(W·E)（W≤1440）。

### 同修订批次预排 / 确认（实现见 `app/services/batch.py`）

1. **批次预排**：先解析批次修订号（显式指定须存在，否则取最新；未知沿用
   `TIDE_REVISION_NOT_FOUND`）；一次读取覆盖全批各船窗口并集区间
   `[min(start), max(start+window-1)]` 的全部已确认占用。随后按 `ships` 优先顺序
   逐艘调用上面的单船 DP（同一修订、同一择优规则），每艘规划成功后把其行程
   `d..d+t-1` 并入内存中的 blocked 集合作为**下一艘**的占用；预排全程只读。
   任一船抛 `NODE_NOT_FOUND` / `MISSING_TIDE_DATA` / `NO_FEASIBLE_ITINERARY` 时，
   原样附加 `ship_index`（首个失败序号，0 起）后抛出。
2. **批次确认**（单事务 `BEGIN IMMEDIATE`）：先按幂等键查 `batch_confirmations`：
   命中且内容哈希（请求体去掉幂等键后的规范化 SHA-256）与修订号一致 -> 直接返回
   存储的首次结果（标记 `replayed=true`，**优先于修订新鲜度检查**）；命中但不一致
   -> `BATCH_KEY_CONFLICT`。新键时依次做：修订新鲜度（`REVISION_STALE`）->
   读取当时已确认占用 -> 逐艘复算（`NO_FEASIBLE_ITINERARY` 转
   `PLAN_MISMATCH(no_feasible_itinerary)`，与提交行程六元组逐段或抵达分钟不一致转
   `PLAN_MISMATCH(itinerary_mismatch)`，均带 `ship_index`）-> 复算通过即在同一事务
   写入该船快照并把其**复算行程**并入后船占用。全部通过后写入幂等记录并 `COMMIT`；
   任一异常 `ROLLBACK`，已插入快照随之撤销，实现全成全败。唯一键竞争（并发同键）
   在回滚后按已存在记录重判重放/冲突。

### 更正后复核 / 改期（实现见 `app/services/reschedule.py`）

1. 只读取出已确认快照（行程号、快照修订、船舶参数、原起始分钟/窗口、完整行程 JSON），
   校验目标修订（缺省取最新；未知修订 `TIDE_REVISION_NOT_FOUND`），取全窗口名义
   潮位与误差，任一分钟缺名义潮位报 `MISSING_TIDE_DATA`；未知行程报 `TRIP_NOT_FOUND`。
   另以 `load_occupancy(..., exclude_trip_id=本行程)` 读取**其他**已确认快照的占用。
2. **原航路复核**：按快照航段顺序遍历每段原占用分钟（`d..d+t-1`），只用与规划相同的
   误差带 `_minute_ok` 判定（水深 `深度+名义−误差`、净空 `净空−名义−误差`），
   **不检查与其他快照的占用重叠**；返回首个违规分钟及其 `depth`/`clearance` 原因；
   `available_depth`/`available_clearance` 按上述对应极值计算。
3. **改期 DP**：航段序列固定（快照的 k 个 `seg_id`），状态为「完成前 i 段、于某分钟
   抵达该段终点」，每状态只保留一个最优代价
   `(相对快照的出发分钟变动段数, 各段出发分钟序列)`（字典序）。可行表复用规划的
   不可行分钟前缀和，每分钟的不可行原因同时包含误差带违规与**他船占用**（本快照自身
   已排除）；第 i 段用「抵达分钟 ≤ 当前出发分钟」的逐分钟最优前缀扫描（等待由前缀
   隐式表达），命中可行进入分钟即追加该段出发分钟，单次判定 O(1)。
4. 终态在全部抵达分钟上取 `(变动段数, 抵达分钟, 出发分钟序列)` 字典序最小者；无终态
   即 `no_solution`。复杂度 O(k·W)，且全程不写库。

剪枝依据：同一 `(i, 抵达分钟)` 上若代价 `(c1, d1) < (c2, d2)`，则追加同一段的同一
出发分钟后，变动段数同增 0/1、出发序列同追加同一值，字典序关系不变，故每状态只保留
一个最优代价不会丢失最优解。

---

## 6. 项目结构

```
app/
  config.py             # 环境变量配置
  database.py           # SQLite 连接、建表、演示数据初始化
  errors.py             # 统一业务错误（含 trip_not_found 构造器）
  schemas.py            # Pydantic 模型与入参校验（窗口<=1440、航时>0 等）
  services/planning.py  # 潮汐约束与时间展开 DP 规划算法
  services/batch.py     # 同修订批次预排/确认（逐艘串行规划 + 事务复算 + 幂等键）
  services/reschedule.py# 更正后原行程复核与改期 DP（只读）
  main.py               # FastAPI 路由（节点/航段/潮位更正与误差修订/单船与批次规划确认/复核改期）
tests/                  # pytest 测试（85 个，含择优、缺潮位、事务冲突、误差带、复核改期、航段占用、批次预排/确认/幂等等）
Dockerfile
docker-compose.yml
requirements.txt
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```
