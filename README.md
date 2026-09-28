# 内网 CIDR 批量分配 API

基于 **Python + FastAPI + SQLite + 标准库 `ipaddress`** 的内网地址池批量分配服务。

地址池由一个**父网段**和若干**保留子网**组成。预演接口一次接收 **1～8 个前缀长度请求**，在剩余空间内搜索**全部可行的对齐子网组合**，按以下字典序选取唯一最优方案：

1. **先最小化**：分配完成后剩余空闲空间的规范 CIDR 数量（即尽量减少地址碎片）；
2. **再最小化**：按请求顺序排列的网络地址序列，取字典序最小者（即尽量让靠前的请求拿到更小的地址）。

提交接口只在**池版本未变化且方案仍可用**时，在单条数据库事务内**原子落库**；否则返回 `409 Conflict`，**不会发生部分分配**。

回收接口对**已分配地址做部分回收**：传入池版本与一个规范 CIDR，该 CIDR 必须**完全由当前已分配地址覆盖**（可跨越相邻的多条分配记录）；在单条写事务内扣除目标地址、把仍被占用的部分重写为最少的规范 CIDR 并自增版本。若含未分配或保留地址、越出父网段或版本过期，则返回 `409 Conflict` 且**地址池保持不变**。

保留区替换接口把**保留地址整体替换**为目标集合：传入池版本与 `desired_cidrs`（允许空列表、重叠与相邻网段），在单条写事务内规范化为**覆盖范围相同且数量最少**的规范 CIDR、重写全部保留记录并自增版本。目标范围与已分配地址相交或版本过期时返回 `409 Conflict` 且**地址池保持不变**；非规范、越出父网段或地址族不一致的输入返回 `422 Unprocessable Entity`。

整批迁移接口一次接收 **1～8 组 `from_cidr`/`to_cidr`**，把多段已分配地址**按整批同时迁移的结果**搬到新位置：两端必须是规范 CIDR、地址族与前缀长度相同，各来源之间、各目标之间不得重叠；每个来源必须被当前已分配地址完整覆盖，目标可使用本批来源释放的地址（支持两组目标互换、链式迁移、原地迁移），但不得触及保留区或未迁移的已分配地址。迁移后把剩余已分配地址与目标地址合并重写为最少的规范 CIDR，版本检查、覆盖与冲突复核、重写及版本自增在单个 SQLite 写事务内完成；输入非法返回 `422 Unprocessable Entity`，状态冲突返回 `409 Conflict` 且池与版本不变。

每次提交、回收、迁移或保留区替换**成功**时，都会在同一个写事务内为新版本保存一份**不可变历史快照**（父网段、保留 CIDR、已分配 CIDR）；失败操作回滚，不产生版本。数据库首次升级时为当前池版本补存一份基线快照。通过 `GET /api/pool/versions/{version}` 可查询任意历史版本：返回该版本固化的状态与**据此快照重新计算**的空闲 CIDR，旧快照不随后续写入变化；未知版本返回 `404 Not Found`。

---

## 一条命令启动

```bash
docker compose up --build -d
```

该命令会一次性完成：

- 构建镜像并安装全部 Python 依赖；
- 初始化 SQLite 数据库（建表、写入父网段与保留子网、保存版本 1 基线快照）；
- 启动 API 服务。

> 数据库与依赖初始化均随 `docker compose up --build` 自动完成，无需任何手动迁移步骤。数据持久化在命名卷 `cidr-data`（容器内 `/data`）。

服务地址与端口：

- **API 基址**：`http://localhost:8000`
- **交互式文档（Swagger UI）**：<http://localhost:8000/docs>
- **OpenAPI Schema**：<http://localhost:8000/openapi.json>
- **健康检查**：`GET http://localhost:8000/healthz`

停止 / 清理：

```bash
docker compose down            # 停止并删除容器（保留数据卷）
docker compose down -v         # 同时删除数据卷（彻底重置）
```

---

## 必要配置

配置通过 `docker-compose.yml` 中的环境变量提供（也可在 `environment:` 中直接修改）：

| 环境变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `PARENT_CIDR` | 是 | `10.0.0.0/8` | 地址池父网段，IPv4/IPv6 均可，如 `10.0.0.0/16`、`2001:db8::/48`。 |
| `RESERVED_CIDRS` | 否 | 空 | 保留子网列表，逗号分隔，必须全部包含在父网段内；如 `10.0.0.0/20,10.0.255.0/24`。 |
| `DB_PATH` | 否 | `/data/cidr_pool.db` | SQLite 数据库文件路径；建议保持在挂载卷 `/data` 下。 |

> 地址池配置仅在**数据库首次初始化**（数据卷为空）时生效。修改父网段/保留子网后若希望重新初始化，请先执行 `docker compose down -v` 再 `up`。

支持 IPv4 与 IPv6（二者不能混用在同一地址池）。

---

## API 一览

基址：`http://localhost:8000`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查 |
| GET | `/api/pool` | 查询池状态：父网段、版本、保留/已分配子网、当前空闲 CIDR |
| GET | `/api/pool/versions/{version}` | **历史版本**：返回指定版本的不可变快照状态与据此计算的空闲 CIDR，未知版本 404 |
| POST | `/api/allocations/preview` | **预演**：不修改状态，返回池版本与最优方案 |
| POST | `/api/allocations/commit` | **提交**：携带版本与方案，原子落库并生成新版本快照或返回 409 |
| POST | `/api/allocations/reclaim` | **部分回收**：回收完全被已分配地址覆盖的规范 CIDR |
| POST | `/api/allocations/relocate` | **整批迁移**：1～8 组 from/to，同时迁移多段已分配地址并合并重写 |
| POST | `/api/pool/reservations/replace` | **替换保留区**：以目标集合整体重写保留子网（规范化为最少 CIDR） |

### 1. 查询池状态

```bash
curl http://localhost:8000/api/pool
```

```json
{
  "parent_cidr": "10.0.0.0/8",
  "version": 1,
  "reserved_cidrs": ["10.0.0.0/16", "10.255.255.0/24"],
  "allocated_cidrs": [],
  "free_cidrs": ["10.1.0.0/16", "10.2.0.0/15", "..."],
  "free_cidr_count": 4
}
```

### 2. 预演分配

请求体 `prefix_lengths` 为 1～8 个前缀长度，**顺序即请求顺序**。

```bash
curl -X POST http://localhost:8000/api/allocations/preview \
  -H 'Content-Type: application/json' \
  -d '{"prefix_lengths": [24, 24, 23]}'
```

成功（方案可行）：

```json
{
  "pool_version": 1,
  "feasible": true,
  "allocations": ["10.1.0.0/24", "10.1.1.0/24", "10.1.2.0/23"],
  "free_cidr_count_after": 3,
  "free_cidrs_after": ["..."],
  "reason": null
}
```

- `pool_version`：预演时刻的池版本，提交时必须原样带回；
- `allocations`：与 `prefix_lengths` **同序**对齐的子网 CIDR，为全局最优方案；
- `free_cidr_count_after` / `free_cidrs_after`：按此方案分配后剩余的空闲 CIDR 数量与列表。

无可行方案时 HTTP 200 返回：

```json
{ "pool_version": 1, "feasible": false, "allocations": [],
  "free_cidr_count_after": 0, "free_cidrs_after": [],
  "reason": "no aligned subnet fits in free space" }
```

参数非法（空列表、超过 8 个、前缀越界等）返回 `422 Unprocessable Entity`。

### 3. 提交分配（原子）

把预演返回的版本号与方案原样提交：

```bash
curl -X POST http://localhost:8000/api/allocations/commit \
  -H 'Content-Type: application/json' \
  -d '{"pool_version": 1,
       "allocations": ["10.1.0.0/24", "10.1.1.0/24", "10.1.2.0/23"]}'
```

成功（版本自增、方案整体落库）：

```json
{
  "status": "committed",
  "pool_version": 2,
  "allocations": ["10.1.0.0/24", "10.1.1.0/24", "10.1.2.0/23"],
  "free_cidrs": ["..."]
}
```

失败时返回 `409 Conflict`，响应体 `detail` 说明原因，**数据库不发生任何写入**：

- 池版本已被其它提交改变（乐观并发控制）；
- 方案中的子网已被占用或与现存子网重叠；
- 方案越出父网段或内部重叠。

典型工作流：**先 preview 拿到 `pool_version` + `allocations`，再原样 commit**；若收到 409，则重新 preview 后重试。

### 4. 回收已分配地址（部分回收，原子）

把当前池版本与一个**规范 CIDR** 提交到回收接口。该 CIDR 必须**完全由当前已分配地址覆盖**——可以恰好是一条分配记录，也可以跨越相邻的多条记录（例如两个相邻 `/25` 合成的 `/24`），也可以只覆盖某条记录的一部分。

```bash
curl -X POST http://localhost:8000/api/allocations/reclaim \
  -H 'Content-Type: application/json' \
  -d '{"pool_version": 3, "cidr": "10.1.0.128/25"}'
```

成功（在单个 SQLite 写事务内扣除目标地址、剩余占用重写为最少规范 CIDR、版本自增）：

```json
{
  "status": "reclaimed",
  "pool_version": 4,
  "reclaimed_cidr": "10.1.0.128/25",
  "allocated_cidrs": ["10.1.1.0/24"],
  "free_cidrs": ["..."]
}
```

- `reclaimed_cidr`：实际回收的 CIDR（即请求的规范 CIDR）；
- `allocated_cidrs`：回收后**剩余仍被占用**的地址，已全局合并相邻区间并重写为**数量最少**的规范 CIDR（可能少于原分配记录数）；
- `free_cidrs`：回收后的当前空闲 CIDR，回收空间立即可用；
- `pool_version`：自增后的新版本，后续预演/提交/回收均以此为准。

成功回收后，**池状态查询（`GET /api/pool`）与后续分配预演立即反映回收空间**：同一 CIDR 可立刻被 `/api/allocations/preview` 选中并再次提交。

失败时返回 `409 Conflict`，响应体 `detail` 说明原因，**数据库不发生任何写入（版本与地址池均不变）**：

- `pool_version` 已过期（被其它提交/回收改变，乐观并发控制）；
- CIDR 越出父网段；
- CIDR 与保留子网重叠（含保留地址）；
- CIDR 未被已分配地址完全覆盖（含未分配地址），包括已分配集为空时的回收。

CIDR 写法非规范（如主机位非零的 `10.1.0.1/24` 或无法解析）返回 `422 Unprocessable Entity`。

> 回收不要求目标与既有分配记录的边界对齐，但目标本身必须是规范 CIDR；扣除后系统会重新规范化全部已分配地址，因此回收后 `allocated_cidrs` 里的记录集合可能与回收前完全不同（数目通常更少），其覆盖的地址集合则严格等于“原已分配集合减去目标”。

### 5. 整批迁移已分配地址（同时迁移，原子）

把当前池版本与 **1～8 组** `from_cidr`/`to_cidr` 提交到迁移接口，系统按**整批同时迁移**的结果一次性完成搬运：每段来源被释放、每个目标被占用。

- 每组两端都必须是**规范 CIDR**，且**地址族与前缀长度相同**；
- 各来源之间不得重叠、各目标之间不得重叠（相邻允许）；
- 每个来源必须被**当前已分配地址完整覆盖**——可跨越相邻的多条分配记录，也可只覆盖某条记录的一部分；
- 每个目标必须在父网段内，**可以使用本批来源释放的地址**（例如两组目标互换、链式搬家、`from_cidr == to_cidr` 的原地迁移），但**不得触及保留区或任何未被本批迁移的已分配地址**。

```bash
curl -X POST http://localhost:8000/api/allocations/relocate \
  -H 'Content-Type: application/json' \
  -d '{"pool_version": 6,
       "moves": [
         {"from_cidr": "10.10.254.0/25",  "to_cidr": "10.10.253.128/25"},
         {"from_cidr": "10.10.254.224/27","to_cidr": "10.10.251.0/27"}
       ]}'
```

成功（版本检查、覆盖与冲突复核、重写、版本自增在单个 SQLite 写事务内完成）：

```json
{
  "status": "relocated",
  "pool_version": 7,
  "moves": [
    {"from_cidr": "10.10.254.0/25",  "to_cidr": "10.10.253.128/25"},
    {"from_cidr": "10.10.254.224/27","to_cidr": "10.10.251.0/27"}
  ],
  "allocated_cidrs": [
    "10.10.251.0/27",
    "10.10.253.128/25",
    "10.10.254.128/26",
    "10.10.254.192/27"
  ],
  "free_cidrs": ["..."]
}
```

- `moves`：原样回显本次迁移的 1～8 组来源/目标；
- `allocated_cidrs`：迁移后全部已分配地址，已把**剩余已分配地址与目标地址**全局合并相邻区间并重写为**数量最少**的规范 CIDR（其覆盖的地址集合严格等于“原已分配集合 − 全部来源 + 全部目标”）；
- `free_cidrs`：迁移后的当前空闲 CIDR，来源释放出的空间立即体现在其中；
- `pool_version`：自增后的新版本，后续预演/提交/回收/迁移/替换均以此为准。

> “同时迁移”意味着目标可以落在另一组来源的地址上：A 搬到 B 的原位置、B 搬到别处，这类**互换 / 链式**搬家若拆成多次串行调用会在第一步 409，但放在同一次请求里就是合法的。目标也允许落在与迁移无关的普通空闲地址上；`from_cidr == to_cidr` 视为原地迁移（占用集不变，仅版本自增）。

成功迁移后，**池状态查询（`GET /api/pool`）与后续分配预演立即反映迁移结果**：来源地址立即可被 `/api/allocations/preview` 选中并提交。

失败时返回 `409 Conflict`，响应体 `detail` 说明原因，**数据库不发生任何写入（版本与地址池均不变）**：

- `pool_version` 已过期（被其它提交/回收/迁移/替换改变，乐观并发控制）；
- 来源未被当前已分配地址完整覆盖（含未分配地址或保留地址）；
- 目标触及保留子网；
- 目标触及未被本批迁移的已分配地址（整批语义：只有全部来源覆盖的地址会被释放）。

输入非法时返回 `422 Unprocessable Entity`：组数为空或超过 8 个、CIDR 无法解析或写法非规范（如主机位非零的 `10.10.254.1/25`）、同组两端地址族或前缀长度不一致、来源之间或目标之间相互重叠、来源或目标越出父网段（含父网段的超网）或地址族与父网段不一致。

### 6. 替换保留地址（整体替换，原子）

把当前池版本与保留地址的**完整目标集合** `desired_cidrs` 提交到替换接口。目标集合允许为**空列表**（清空全部保留地址），也允许网段之间**重叠、包含或相邻**；落库时系统会合并相交/相邻区间，规范化为**覆盖范围相同且数量最少**的规范 CIDR 后整体重写保留记录。

```bash
curl -X POST http://localhost:8000/api/pool/reservations/replace \
  -H 'Content-Type: application/json' \
  -d '{"pool_version": 4,
       "desired_cidrs": ["10.0.0.0/24", "10.0.1.0/24", "10.0.2.128/25"]}'
```

成功（在单个 SQLite 写事务内规范化、重写保留记录、版本自增）：

```json
{
  "status": "replaced",
  "pool_version": 5,
  "reserved_cidrs": ["10.0.0.0/23", "10.0.2.128/25"],
  "free_cidrs": ["..."]
}
```

- `desired_cidrs` 是保留地址的**完整目标集合**（不是增量）：上例中相邻的 `10.0.0.0/24` + `10.0.1.0/24` 被规范化为 `10.0.0.0/23`；
- `reserved_cidrs`：替换后的保留子网，已规范化为数量最少的规范 CIDR；
- `pool_version`：自增后的新版本，后续预演/提交/回收/替换均以此为准。

成功替换后，**池状态查询（`GET /api/pool`）与后续分配预演立即采用新保留区**：新保留的空间不再可被分配，被移出保留区的空间立即可被 `/api/allocations/preview` 选中并提交。

失败时返回 `409 Conflict`，响应体 `detail` 说明原因，**数据库不发生任何写入（版本与地址池均不变）**：

- `pool_version` 已过期（被其它提交/回收/替换改变，乐观并发控制）；
- 目标范围与**已分配地址**相交（含完全包含某条分配记录；与既有保留子网重叠不视为冲突——它们将被整体替换）。

输入非法时返回 `422 Unprocessable Entity`：CIDR 写法非规范（如主机位非零的 `10.0.0.1/24`）、无法解析、越出父网段（含父网段的超网）或地址族与父网段不一致。

### 7. 查询历史版本（只读快照）

每个池版本在生成时都会固化一份**不可变快照**：当时的父网段、保留 CIDR 与已分配 CIDR。按版本号查询即返回该版本的完整状态，并**依据快照内容**重新计算空闲 CIDR。

```bash
curl http://localhost:8000/api/pool/versions/3
```

成功（HTTP 200）响应体与 `GET /api/pool` 结构一致，但内容来自历史快照而非当前池：

```json
{
  "parent_cidr": "10.0.0.0/8",
  "version": 3,
  "reserved_cidrs": ["10.0.0.0/16", "10.255.255.0/24"],
  "allocated_cidrs": ["10.1.0.0/24", "10.1.1.0/24"],
  "free_cidrs": ["..."],
  "free_cidr_count": 3
}
```

快照的生成时机与不变量：

- **基线**：数据库首次升级（老数据卷启动新版本服务、历史版本表首次创建）时，在同一次初始化事务内为**当前**池版本补存一份基线快照；全新建库的基线即初始版本 1。更早的历史版本没有快照；
- **提交 / 部分回收 / 整批迁移 / 保留区替换成功**：在各自写事务内、与状态改写同一事务写入新版本快照；操作失败（回滚，含 409/422）**不生成版本**，因此版本号可能出现空洞；
- **不可变**：快照只写不改，后续任何写入都不影响旧快照；`free_cidrs` 虽在查询时计算，但输入完全来自固化快照，结果同样不随时间变化；
- 查询在单条读事务内整行读取快照；版本号必须是整数，否则返回 `422 Unprocessable Entity`；**未知版本**（含尚未产生的版本、失败操作空出的版本号、基线之前的老版本）返回 `404 Not Found`：

```json
{ "detail": "unknown pool version: 99" }
```

> 历史版本仅记录可观测的池状态（父网段 / 保留 / 已分配 / 空闲 CIDR），不区分该版本由哪一种写操作产生。

---

## 正确性与并发保证

- 所有候选均为网络地址对齐的规范 CIDR（基于标准库 `ipaddress` 严格校验）；
- 搜索算法在二进制前缀树上做子集 DP（每请求集合状态至多 2^8=256 个），
  通过“节点几何 + 请求前缀多重集”的结构化记忆化与等前缀对称剪枝，
  穷举全部可行划分后比较目标函数，因此结果与完全暴力枚举一致
  （仓库测试含数千组 IPv4/IPv6 随机对照用例）；
- 预演与池状态查询在**同一条 SQLite 读事务（WAL 快照）**内读取池版本
  与子网：两次查询之间即使有并发提交落库，返回的版本与占用集也必然
  属于同一快照，不会拼出“旧版本 + 新占用集”的错位状态；
- 提交使用 SQLite `BEGIN IMMEDIATE` 单事务：版本检查 → 方案可用性复检
  （与保留/已分配子网逐一比对）→ 批量插入 → 版本自增。任一环节失败立即
  回滚，保证“全成功或全不写入”，并发提交同一版本时**恰有一个**成功；
- 提交响应中的版本、已分配子网与空闲网段在**本次写事务内部**读取，
  严格绑定本次提交；不会在提交后另开连接而混入其它并发提交的结果。
  （`tests/test_concurrency.py` 用可控的并发交错对以上两条读取不变量
  做了回归覆盖。）
- 回收接口同样使用 `BEGIN IMMEDIATE` 单事务：版本检查 → 父网段/保留地址
  /覆盖性复检 → 删除并以最少规范 CIDR 重写已分配集 → 版本自增。任一环节
  失败立即回滚，地址池不发生任何改动；回收与并发提交/回收之间由写锁互斥，
  同一版本并发写时**恰有一个**成功。回收响应中的已分配/空闲 CIDR 同样在
  本写事务内部读取，提交后的 `GET /api/pool` 与预演立即基于新版本与回收后
  的占用集计算。
- 保留区替换接口同样使用 `BEGIN IMMEDIATE` 单事务：父网段/地址族校验 →
  版本核对 → 与已分配地址的冲突复核 → 删除并以最少规范 CIDR 重写保留
  记录 → 版本自增。任一环节失败立即回滚，池状态与版本均不变；替换与并发
  提交/回收/替换之间由写锁互斥，同一版本并发写时**恰有一个**成功。替换
  响应中的保留/空闲 CIDR 同样在本写事务内部读取，提交后的 `GET /api/pool`
  与预演立即基于新版本与新保留区计算。
- 整批迁移接口同样使用 `BEGIN IMMEDIATE` 单事务：父网段/地址族校验 →
  版本检查 → 来源覆盖性复核（每个来源必须被当前已分配地址完整覆盖）→
  目标冲突复核（不触及保留区、不触及未迁移的已分配地址；本批来源释放
  的地址可用）→ 把“剩余已分配地址 ∪ 全部目标”合并重写为最少规范
  CIDR → 版本自增。全部复核针对事务内读取的同一状态快照，任一环节失败
  立即回滚，池与版本均不变；迁移与并发提交/回收/替换/迁移之间由写锁
  互斥，同一版本并发写时**恰有一个**成功。迁移响应中的已分配/空闲 CIDR
  同样在本写事务内部读取，提交后的 `GET /api/pool` 与预演立即基于新版本
  与迁移后的占用集计算。
- 历史版本快照随上述四类写操作在**同一写事务**内写入（版本检查、复核、
  状态重写、版本自增与快照插入要么全部提交要么全部回滚），失败操作不会
  留下版本；快照行只插入、从不更新或删除，因此旧版本状态不随后续写入
  变化。历史版本查询整行读取快照并据此重新计算空闲 CIDR，与当前 `subnets`
  表完全解耦。数据库首次升级时基线补存在初始化事务内完成，与建表、初始
  池写入原子一致；重复初始化（重启）幂等，不会重复写入基线。

---

## 本地开发（不使用 Docker）

```bash
pip install -r requirements.txt
DB_PATH=./cidr_pool.db PARENT_CIDR=10.0.0.0/16 \
  uvicorn app.main:app --reload --port 8000
```

运行测试（无需网络与第三方依赖即可跑算法对照测试）：

```bash
python3 -m tests.test_allocator_bruteforce   # 4000 组 IPv4 暴力对照
python3 -m tests.test_api                    # 接口端到端 + 冲突/原子性
python3 -m tests.test_reclaim                # 部分回收端到端：跨记录/重写/冲突
python3 -m tests.test_relocate               # 整批迁移：互换/链式/原地/重写/冲突/并发
python3 -m tests.test_reservations_replace   # 保留区整体替换：规范化/冲突/并发
python3 -m tests.test_versions               # 历史版本：基线补存/成功写出版/失败不出版/不可变/老库升级
python3 -m tests.test_concurrency            # 并发交错回归：快照读取与提交响应绑定
```

## 项目结构

```
app/
  allocator.py   # 区间运算 + 前缀树子集 DP 搜索算法（仅标准库）
  config.py      # 环境变量配置
  db.py          # SQLite 建表、状态读取、BEGIN IMMEDIATE 原子提交、历史版本快照
  main.py        # FastAPI 路由与请求/响应模型
tests/
  test_allocator_bruteforce.py
  test_dense_reserved.py
  test_api.py
  test_reclaim.py
  test_relocate.py
  test_reservations_replace.py
  test_versions.py
  test_concurrency.py
Dockerfile
docker-compose.yml
requirements.txt
```
