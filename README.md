# segment-recorder：环形覆盖的录像分片接收与回看服务

产线值班员回看录像时，录像盘仍在覆盖旧片。本服务用 FastAPI 接收单路编码器按连续片号上传的录像分片，本地固定槽位环形覆盖保存；终端凭游标按序续读原始字节回看，被覆盖时明确报断档并指引重新起播。

## 功能

- **分片上传**：单路编码器按连续片号上传；单片最大 **16 MiB**；随片用 `independent` 标明能否独立解码。片号不连续返回 409。
- **环形覆盖**：槽位数可配（`SLOT_COUNT`），每槽一片，写满覆盖最旧片；磁盘上始终只有活动槽位个分片文件。
- **在线调容**：值班员可用 `PUT /v1/slots/limit?limit=N` 在 1 到启动配置槽位数之间调小/调大上限，无需重启。调小保留生效时最新的至多 N 片，其余片淘汰；调大只恢复容量，不恢复已淘汰的片。
- **起播**：从"不晚于所求片号、仍留存的最近独立片"起播。
- **续读**：终端凭游标按序读**原始字节**，每次最多 **64 KiB**（可用 `max_bytes` 调小）；一片读完自动滚入下一片。
- **断档（410）**：找不到起点（起点已被覆盖/淘汰）或续读位置已被覆盖/淘汰时，返回 JSON 并给出 `earliest_playable`（最早仍可起播的片号）。
- **等待（204）**：库里没有任何可独立解码的片（无可用起点），或已读到直播边缘（下一片尚未写入）。
- **旧游标安全**：游标绑定 `(槽位, 代次, 片号, 偏移)`，覆盖使代次 +1，旧游标永远读不到同槽新片的字节，只会收到 410。
- **调容不复制录像**：缩容只在元数据层决定取舍，保留片的槽位、代次、文件一律不动，原游标从原偏移续读；淘汰片的文件异步删除（先原子改名到 `trash_*` 再删，不会误删复用槽位的新片）。
- **回收可观测、可重试**：`/v1/state` 报告待删旧片占用字节数 `pending_delete_bytes` 与 `reclaim_complete`；删除失败自动退避重试，也可 `POST /v1/slots/reclaim` 立即重试；删完才报回收完成。回收未完成时拒绝再次调小（调大不限）。
- **慢终端无妨碍**：读路径只在单个请求内短暂持锁（≤64 KiB），请求之间不持有任何资源；写路径不等待读者；槽位固定，慢终端不多占磁盘。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up -d --build     # 监听 8000 端口，SLOT_COUNT=4（便于演示覆盖）
curl localhost:8000/healthz
```

### 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SLOT_COUNT=4 DATA_DIR=./data .venv/bin/uvicorn app.main:app --port 8000
```

### 端到端演示（含"读到半片被覆盖后重新起播"）

```bash
./demo/demo.sh                   # 自动 docker compose up/down，全程 curl 演示
./demo/demo.sh --no-compose      # 服务已在运行时，直接演（可用 BASE=... 指定地址）
```

演示流程：空库起播(204) → 上传片 1..4 写满 4 槽 → 求片 2 的起点、实际从片 1 起播 →
读完片 1、再读片 2 的一半 → 上传片 5、6 覆盖片 1、2 所在槽 → 旧游标续读收到
**410 断档**及 `earliest_playable=4` → 用该片号**重新起播** → 读到直播边缘(204) →
`cmp` 字节级校验读回内容与上传完全一致。

### 在线调容演示（缩容后继续录制）

```bash
./demo/resize_demo.sh              # 同样自动 docker compose up/down
./demo/resize_demo.sh --no-compose # 服务已在运行时直接演（BASE=... 指定地址）
```

演示流程：上传片 1..4 → 两个终端各自起播并各读 64 KiB 留下原游标 →
`PUT /v1/slots/limit?limit=2` 把上限从 4 调到 2，保留片 3、4、淘汰片 1、2
（状态报告 204800 待删字节，删完才报回收完成）→ **保留片 4 用原游标从 offset=65536
续读并字节校验** → **淘汰片 1 的旧游标收到 410 断档** → 继续上传片 5、6，按新上限 2
覆盖片 3、4 → 起播读到直播边缘并 `cmp` 校验 → 调回上限 4（只恢复容量，片 1 仍 410，不复活）。

## API

### `POST /v1/segments/{seq}?independent=true|false`

上传一片。请求体为原始字节（`application/octet-stream`），≤16 MiB，非空。
片号必须连续（等于上一片 +1；库空时可任意起步）。

| 状态码 | 含义 |
|---|---|
| 201 | 已接收，JSON 返回 `{seq, slot, generation, size, independent, oldest_seq, newest_seq, slot_limit}` |
| 400 | 空分片 |
| 409 | 片号不连续，JSON 给出 `expected_seq` / `got_seq` |
| 413 | 超过 16 MiB |

### `GET /v1/playback/start?seq=N`

起播。从"不晚于 N、仍留存的最近独立片"开始。

| 状态码 | 含义 |
|---|---|
| 200 | `{"status":"ok","start_seq":S,"cursor":"..."}`，S 为实际起播片号（≤N） |
| 204 | 等待：库里没有可独立解码的片（`X-Reason: no-independent-start`） |
| 410 | 断档：起点已被覆盖，JSON 给出 `earliest_playable`（最早仍可起播片号，可能为 null） |

### `GET /v1/playback/read?cursor=...&max_bytes=...`

凭游标续读。`max_bytes` 可选，默认且上限 64 KiB。

| 状态码 | 含义 |
|---|---|
| 200 | 响应体即**原始字节**（≤64 KiB）；响应头见下 |
| 204 | 等待：下一片尚未写入（`X-Reason: next-segment-not-written`），同一游标稍后继续有效 |
| 400 | 游标无法解析或越界 |
| 410 | 断档：续读位置已被覆盖，JSON 给出 `earliest_playable`；旧游标不会读到同槽新片 |
| 422 | `max_bytes` 超过 64 KiB |

200 响应头：`X-Next-Cursor`（下一次续读凭据）、`X-Seq`、`X-Offset`、`X-Segment-Size`、
`X-Independent`、`X-End-Of-Segment`。一片读完后下一次请求自动滚入下一片。

### `GET /v1/state` / `GET /healthz`

`/v1/state` 返回：`slot_count`（启动配置）、`slot_limit`（当前上限）、`active_slots`、
各槽 `(slot, active, empty, seq, independent, generation, size)`、最旧/最新片号、最早可起播片号，
以及回收现状：`pending_delete_bytes`（待删旧片占用字节）、`reclaim_complete`、
`reclaim{pending_segments, complete, in_progress, failed_segments, last_error}`、`retired[]`。
`/healthz` 为健康检查。

### `PUT /v1/slots/limit?limit=N`

在线设置活动槽位上限，`N` 取 **1..启动配置的 `SLOT_COUNT`**。

- **调小**：保留生效时最新的至多 N 片（`retained_seqs`），其余片清出索引并排队删除（`evicted_seqs`）；
  返回 `evicted_delete_bytes` 与当前 `pending_delete_bytes`。保留片的槽位/代次/文件不动，
  原游标可原偏移续读；淘汰片的游标与起播请求一律 410 断档。
- **调大**：把非活动槽位重新纳入（`activated_slots`），只恢复容量；**已淘汰的片不会恢复**。
- 删除在后台异步进行，完成后 `reclaim_complete` 才为 true。

| 状态码 | 含义 |
|---|---|
| 200 | 已生效，JSON 返回本次调容结果与最新 `state` |
| 409 | 上一轮淘汰的旧片尚未删完，拒绝再次调小；JSON 给出 `pending_delete_bytes` / `pending_segments` 与重试入口 |
| 422 | `limit` 不在 1..`SLOT_COUNT`，JSON 给出 `min_limit` / `configured_slot_count` |

### `POST /v1/slots/reclaim`

立即重试删除待删旧片（删除失败时可手动触发；后台也会按指数退避自动重试）。
返回最新 `reclaim` 状态与 `state`。

## 设计要点

- **存储**：每槽一个文件 `slot_XXXX.seg`；写入先落临时文件再 `os.replace` 原子替换；
  元数据（片号、独立标记、代次、大小）全在内存，由一把 `asyncio.Lock` 保护。
- **在线调容**：运行期维护活动槽位集合与上限。缩容时在锁内选出最新的至多 N 片保留，
  保留片完全不动（不复制、不搬动、代次不变），被淘汰的槽位清空元数据并把旧文件登记
  到待删表。扩缩容都只改元数据，不触碰保留片文件。
- **回收安全**：删除先在锁内把旧文件原子改名为 `trash_sXXXX_gYYYYYYYY.del`，再在锁外
  `unlink`。即使该物理槽位随后被调大上限复用并写入新片，清理任务删的也只是 trash，
  不会删到新片；若槽位在清理前已被复用，旧槽位路径已被新片原子替换，登记项直接作废
  （新文件本就不该删）。
- **一致性**："校验游标 → 读字节"与"覆盖写文件 → 改元数据"在同一把锁内互斥，
  不会读到半新半旧；读到的 64 KiB 一旦返回即与当时槽位内容一致。
- **游标**：base64url(JSON `{s:槽位, g:代次, q:片号, o:偏移})`，不透明但无签名（见限制）。
- **断档 vs 等待**：还能救（存在更晚的可起播点）→ 410 并给出最早可起播片号；
  库里根本没东西可播 / 已追到直播边缘 → 204 让客户端稍后再来。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SLOT_COUNT` | 8（compose 中为 4） | 启动配置的物理槽位数，也是调容上限的最大值 |
| `DATA_DIR` | `./data`（镜像中 `/data`） | 分片文件目录 |
| `MAX_SEGMENT_BYTES` | 16777216 | 单片上限，需求规定 16 MiB |
| `MAX_READ_BYTES` | 65536 | 单次续读上限，需求规定 64 KiB |

运行期槽位上限默认等于 `SLOT_COUNT`，通过 `PUT /v1/slots/limit` 调整；它不持久化，
重启后回到 `SLOT_COUNT`（元数据本身也不持久化，见限制）。

## 自测

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q     # 45 项用例
```

覆盖：上传校验（连续片号/空片/16 MiB 上限）、环形覆盖顺序与代次递增、起播取
"不晚于所求的最近独立片"、起点被覆盖报 410、空库/无独立片报 204、64 KiB 分块、
跨片自动滚动、直播边缘 204 且游标不失效、半片被覆盖后旧游标 410 且读不到同槽新片、
整圈覆盖后游标失效、端到端字节一致、磁盘槽位不膨胀；以及在线调容：
上调/下调/越界(422)、缩容保留最新至多 N 片、保留片原偏移续读、淘汰片 410、
缩容后按新上限继续录制、调容不复制/不搬动保留片、调大不恢复已淘汰片、
待删字节上报、删除失败(409)与重试(200)、回收未完成时拒绝再次调小、回收期间上传/回看照常、
旧清理不误删复用槽位的新片。

## 限制与未完成

- **重启即清空**：元数据在内存，重启后无法恢复索引，启动时清空数据目录从头开始
  （未做崩溃恢复/索引持久化）。
- **单编码器**：片号连续性按单路设计；多路并发上传会被 409 拒绝。
- **游标无签名**：仅防意外不防伪造，伪造游标只会得到 400/410，不会越权读数据。
- **无鉴权/HTTPS**：假定部署在受信内网。调容接口同样无鉴权，任何能访问 API 的人都能改容量。
- **调容上限不持久化**：运行期上限与元数据一样只在内存，重启回到 `SLOT_COUNT`。
- **回收任务在进程内**：删除失败时后台重试，进程退出后待删文件留到下次启动统一清空
  （`trash_*` 也会被启动清空）；未做跨重启的回收续做。
- **写入串行化**：单把锁串行所有上传与读临界区（单写者下每次写仅阻塞读者一次
  落盘时间），未做分槽锁等进一步优化。
- Docker 镜像/compose 文件已经过静态审查，但本环境无 Docker，未实际构建运行；
  等价路径（同一环境变量、同一入口命令）已在宿主机直接验证，`demo/resize_demo.sh --no-compose`
  与 `demo/demo.sh --no-compose` 均在宿主机 uvicorn 上完整跑通。
