# FIB Compressor & ECMP Bucket Rearranger

边缘路由器换型 / 检修配套工具，在一台服务里提供两个独立能力。

## 1. FIB 压缩（`POST /compress`）

把现有 IPv4 转发表压缩成**条目最少**的等效表，装进新设备的容量预算。

- 输入：最多 128 条前缀互异的 IPv4 CIDR 路由（每条指定一个下一跳或 `drop` 丢弃）、新设备条目上限 `capacity`、一个要演练失效的下一跳 `failed_nexthop`。
- 输出：按最长前缀匹配（LPM）与原表**逐地址等价**的最小转发表——可换用新前缀，动作全部取自原表；`drop`（丢弃）与未匹配严格区分。
- 故障演练：把新旧两张表中指向 `failed_nexthop` 的条目全部撤掉后，两表仍须逐地址等价。压缩结果天然满足这一约束。
- 超限时只回报最低所需条数（`status=capacity_exceeded`，`table=null`）。

## 2. ECMP 哈希桶重排（`POST /ecmp/rearrange`）

上联检修前，为 IPv4 转发表中以 action 标识的 ECMP 组生成新的桶映射。

- 输入：组名、待摘除的下一跳、最多 16 个桶号互异的桶（每桶给出当前下一跳、同一采样窗口的字节数、活跃连接数）、最多 4 个保留下一跳（各指定**恰好**分到的桶数与该窗口的字节预算）。
- 约束：每个桶整体分给一个保留下一跳；每个保留下一跳恰好分到指定桶数；其分到的桶累计字节数不超预算；原本就在保留下一跳上的桶也允许移动。
- 目标：改走其他下一跳的活跃连接总数最少（并列时移动桶数最少）。
- 输出：可下发的桶号→下一跳映射 `assignment` 与迁移连接数 `migrated_connections`；无解时只回报 `feasible=false`（其余字段为 `null`）。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

`/docs`（Swagger UI）的 `POST /compress` 内置三个示例，`POST /ecmp/rearrange` 内置一个“摘除上联后重排桶”的示例，下拉即可切换：

| 接口 | 示例 | 说明 |
|---|---|---|
| `/compress` | `capacity_ok` | 容量足够：5 条压到 4 条，撤掉 NH2 后两表仍等价 |
| `/compress` | `capacity_exceeded` | 容量不够：只回报 `min_entries=4`，不下发转发表 |
| `/compress` | `failover_ladder` | 故障回落链：/8→NH1、/16→NH2、/24→NH3，演练失效 NH2 |
| `/ecmp/rearrange` | `remove_uplink` | 摘除 NH2：8 桶重排到 NH1/NH3/NH4，只迁移 NH2 的 2 个桶共 2800 条连接 |

命令行演示（服务启动后）：

```bash
./scripts/demo.sh                # 压缩的两种容量场景 + ECMP 重排 + 不可行场景
```

值班核对响应（独立复算桶数、预算、迁移连接数）：

```bash
python3 scripts/check_ecmp_response.py request.json response.json
```
## API

### `POST /compress`

```json
{
  "routes": [
    {"prefix": "10.0.0.0/8", "action": "NH1"},
    {"prefix": "10.1.0.0/16", "action": "NH2"},
    {"prefix": "10.2.0.0/16", "action": "NH1"},
    {"prefix": "10.3.0.0/16", "action": "drop"},
    {"prefix": "192.168.0.0/16", "action": "NH2"}
  ],
  "capacity": 4,
  "failed_nexthop": "NH2"
}
```

容量足够时返回最小等效表（`10.2.0.0/16` 与 `10.0.0.0/8` 同为 NH1，被合并吸收）：

```json
{
  "status": "ok", "fits": true, "min_entries": 4, "verified": true,
  "table": [
    {"prefix": "10.0.0.0/8", "action": "NH1"},
    {"prefix": "10.1.0.0/16", "action": "NH2"},
    {"prefix": "10.3.0.0/16", "action": "drop"},
    {"prefix": "192.168.0.0/16", "action": "NH2"}
  ]
}
```

容量不够（如 `capacity=3`）时只报最低所需条数：

```json
{"status": "capacity_exceeded", "fits": false, "min_entries": 4, "table": null}
```

约束与约定：

- `routes` 最多 128 条，前缀互异；非规范写法（如 `10.1.2.3/8`）会归一化为网络地址。
- `action` 为下一跳名称，或保留字 `drop`；不匹配任何条目的地址是“未匹配”，与 `drop` 不同。
- 响应前服务端会对结果做逐区间等价复核（正常 + 故障两种场景），`verified=true` 才返回。

### `POST /verify`

独立校验两张表是否在正常与故障两种场景下逐地址等价，返回首个失配点。

### `POST /ecmp/rearrange`

```json
{
  "group": "ecmp-core",
  "removed_nexthop": "NH2",
  "buckets": [
    {"bucket": 0, "current_nexthop": "NH1", "bytes_in_window": 4200000000, "active_connections": 1200},
    {"bucket": 1, "current_nexthop": "NH1", "bytes_in_window": 3800000000, "active_connections": 900},
    {"bucket": 2, "current_nexthop": "NH1", "bytes_in_window": 4600000000, "active_connections": 1500},
    {"bucket": 3, "current_nexthop": "NH2", "bytes_in_window": 5000000000, "active_connections": 2100},
    {"bucket": 4, "current_nexthop": "NH2", "bytes_in_window": 2400000000, "active_connections": 700},
    {"bucket": 5, "current_nexthop": "NH3", "bytes_in_window": 3100000000, "active_connections": 800},
    {"bucket": 6, "current_nexthop": "NH3", "bytes_in_window": 2900000000, "active_connections": 650},
    {"bucket": 7, "current_nexthop": "NH4", "bytes_in_window": 1800000000, "active_connections": 400}
  ],
  "retained": [
    {"nexthop": "NH1", "bucket_count": 3, "byte_budget": 15000000000},
    {"nexthop": "NH3", "bucket_count": 3, "byte_budget": 14000000000},
    {"nexthop": "NH4", "bucket_count": 2, "byte_budget": 5000000000}
  ]
}
```

可行时返回映射与迁移连接数（只动 NH2 的两个桶）：

```json
{
  "feasible": true,
  "group": "ecmp-core",
  "removed_nexthop": "NH2",
  "assignment": {"0": "NH1", "1": "NH1", "2": "NH1", "3": "NH3",
                 "4": "NH4", "5": "NH3", "6": "NH3", "7": "NH4"},
  "migrated_connections": 2800
}
```

无解时只报告不可行：

```json
{"feasible": false, "group": "ecmp-core", "removed_nexthop": "NH2",
 "assignment": null, "migrated_connections": null}
```

约束与约定：

- `buckets` 最多 16 个、桶号互异；`retained` 最多 4 个、下一跳互异，且不得包含 `removed_nexthop`。
- 所有保留下一跳的 `bucket_count` 之和必须等于桶总数，否则无解。
- 贡献迁移计数时按活跃连接数计，且只有**下一跳发生变化**的桶才计入；原在保留下一跳上的桶若被移到别处也计入。
- 求解为精确 DP（子集状态），同时最小化迁移连接数并以移动桶数做确定性并列裁决；结果返回前服务端复核桶数、预算与迁移计数。

### `GET /`

服务信息。

## 算法（为什么是最少条目）

每个目的地址带一个二元需求 `(正常结果, 撤掉故障下一跳后的结果)`，由原表推出；两个函数在所有前缀边界切出的基本区间上都是常数（≤ 2n+1 段）。

1. 把地址空间建成“常数需求”二分前缀 trie（叶 = 极大常数需求 dyadic 区间）。
2. 在 trie 上做精确动态规划：状态是“上方最长匹配条目动作 + 上方最长非故障条目动作”的继承对（可达状态只有 `2k+2` 种，k 为非故障动作数）；叶节点用闭式代价（含“正常层=F、回落层=b”的双层结构，无法从祖先继承回落时最多需 3 条）；内部节点在“不放置 / 放置故障下一跳 / 放置某非故障动作”三种选择中取最优。
3. 回溯选择得到最小条目表。DP 是精确的，返回的条目数是可证明的最小值。

复杂度：`O(节点数 × 动作数)`，128 条路由实测 < 0.5s。

## 算法（ECMP 桶重排）

桶按子集建模，精确动态规划：`dp[mask]` 表示用已处理的保留下一跳恰好覆盖桶集合 `mask` 的最小代价。处理每个下一跳时，枚举“从剩余桶中选恰好 `bucket_count` 个、字节和 ≤ 预算”的子集转移；最后一个下一跳被强制接收余下全部桶，省掉一整层枚举。

- 状态数最多 `2^16`；按桶数降序处理通常最省，代码在至多 `4!` 种处理顺序里选转移数最少的一种。
- 代价编码为 `迁移连接数 × (n+1) + 移动桶数`，单整数 DP 同时保证主目标与确定性并列裁决。
- 随机小规模用例与暴力枚举逐一对拍一致（见 `tests/test_ecmp.py`）。
- 16 桶 / 4 目标的纯 Python 最坏情况约 2–3s，8 桶量级为几十毫秒。

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest            # 62 项测试：正常转发、故障切换、最小性、ECMP 重排、API
uvicorn app.main:app --reload
```

## 目录结构

```
app/compressor.py   # FIB 压缩核心：需求区间 → 常数 trie → 双场景 DP → 回溯
app/ecmp.py         # ECMP 桶重排：子集 DP，桶数/预算约束 + 最少迁移连接
app/main.py         # FastAPI：/compress、/verify、/ecmp/rearrange、/docs 示例
tests/              # 单元、随机性质、暴力最小性、ECMP 对拍、API 测试
scripts/demo.sh     # 压缩两场景 + ECMP 重排 + 不可行场景的 curl 演示
scripts/check_ecmp_response.py  # 独立复算 ECMP 响应的约束
Dockerfile / docker-compose.yml
```
