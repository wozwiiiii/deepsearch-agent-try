# P0-3 WebSocket 事件回放设计方案

> 状态：**已实现（SQLite 版，2026-08-30 第四批）**。本方案中的 seq 序号、last_seq 差量补发、
> 前端指数退避、限长裁剪均已落地，实现记录见 `PRODUCTION_NOTES.md` 第四批与
> `tests/test_event_replay.py`。
> **与设计稿的唯一偏差**：存储后端用 SQLite（`app/api/event_store.py`）而非 Redis Stream——
> 当前单进程部署引入 Redis 只为存事件不划算。`append`/`read_after` 与 `XADD`/`XREAD`
> 语义一一对应，P0-2 任务出进程接入 Redis 时只换存储实现类，协议与前端零改动。
> 目标：断线/重连不丢事件、能发现丢件、服务重启后历史可回放。

## 一、当前为什么丢事件

```
monitor._emit() ──▶ WS.send_json()    发完即丢，无持久化
                                          ↑
                              断线期间的最终答案、子智能体结果永久丢失
```

| 问题 | 后果 |
|------|------|
| 事件不持久化 | 断线期间的事件（含**最终答案**）永久丢失 |
| 事件无序号 | 前端无法发现"少收了几条" |
| 重启无回放 | 服务重启后重连的前端拿不到历史事件 |
| 前端固定 2s 重连 | 无退避，服务抖动时雪崩重连 |

`useDeepAgentSession.ts:154` 固定 `setTimeout(reconnect, 2000)`，无指数退避无抖动。

## 二、目标：Redis Stream + 序号 + 差量补发

Redis Stream 天生带自增 ID（序号）+ 支持 `XRANGE` 按范围读取 = 既广播又回放，一套机制解决 P0-2 广播 + P0-3 回放。

```
worker 执行中：
  monitor._emit() ──▶ XADD stream:{task_key} * event_type msg data
                     （Stream ID = 序号，自动递增）

API 进程（持有 WS）：
  订阅 stream:{task_key} ──▶ 收到新事件 ──▶ WS.send_json()
  重连握手带 last_seq ──▶ XREAD 从 last_seq+1 读差量 ──▶ 先补发历史再继续实时
```

## 三、事件 Stream schema

```
# 每个 task_key 一个 Stream（与复合键对齐）
XADD stream:{user_id}-{thread_id} * \
    event_type task_result \
    message "任务执行完成" \
    data '{"result":"..."}' \
    ts 2026-08-17T10:00:00
```

- `*` 让 Redis 自增生成 ID（形如 `1694256000123-0`）——这就是全局有序的事件序号；
- `data` 存原 monitor payload 的 JSON；
- Stream 天然保留历史，配合 `MAXLEN ~ 1000` 或 `XTRIM` 限长防膨胀。

## 四、重连协议（差量补发）

前端 WS 重连时握手带 `last_seq`（最后收到的事件 Stream ID）：

```
WS /ws/{thread_id}?api_key=...&last_seq=1694256000123-0

服务端：
  1. 鉴权（同现有）
  2. XREAD stream:{task_key} 1694256000123-0 +   # 读 last_seq 之后全部
     → 把差量事件按序补发给该连接
  3. 然后进入实时订阅模式（BLOCK 读新事件）
```

- 前端首次连接无 `last_seq` → 可选：补发最近 N 条 / 或从任务开始补 / 或不补（看产品取舍，默认补最近 100 条）；
- `last_seq` 不存在（Stream 已被裁剪）→ 降级：补现有全部 + 标记"可能有不完整"。

## 五、序号让前端能发现丢件

每条 WS 事件携带 `seq`（Stream ID）。前端记录 `last_seq`，若收到 `seq` 不连续（跳号），主动请求 `XREAD last_seq+1` 补发。这让"静默丢件"变成"可检测可恢复"。

## 六、前端重连退避

`useDeepAgentSession.ts` 改：

```ts
// 当前（固定 2s）
setTimeout(reconnect, 2000)

// 改为指数退避 + 抖动
const base = 2000, max = 60000, attempt = retryCount++
const delay = Math.min(base * 2 ** attempt, max) + Math.random() * 1000
setTimeout(reconnect, delay)
```

- 2s → 4s → 8s → … 上限 60s，加随机抖动避免雪崩；
- 重连成功后 `retryCount = 0`。

## 七、与 monitor 的对接（最小改动）

| 现有 | 改造后 |
|------|--------|
| `monitor._send_to_websocket` 直推本进程 WS | `redis.xadd(stream:{task_key}, ...)`（worker 侧）|
| API 进程 WS 端点直接收事件 | 启动时 `XREAD BLOCK` 订阅用户相关 stream，推 WS |
| 无序号 | Stream ID 即序号，payload 带 `seq` |

**关键**：`monitor` 的公开方法（`report_tool`/`report_task_result`/...）签名不变，只改 `_emit` 内部从"推 WS"变成"写 Stream"。业务工具零改动。

## 八、与 P0-2 合并的协同

P0-2 任务队列用 Redis 做 ARQ 队列；本方案用 Redis Stream 做事件。**同一个 Redis 实例**：
- `queue:arq` 队列（任务）
- `stream:{task_key}`（事件）

两者共用连接池、共用运维。建议 P0-2 阶段 2 直接做 P0-3，省一次 Redis 接入。

## 九、迁移步骤

1. **事件落 Stream（1 天）**：`monitor._emit` 改 `redis.xadd`；保留 console print 兜底；API 进程订阅推 WS。
2. **重连带 last_seq 补发（0.5 天）**：WS 握手收 `last_seq`，`XREAD` 差量先补。
3. **前端退避（0.5 天）**：`useDeepAgentSession.ts` 改指数退避。
4. **裁剪策略（0.5 天）**：`XADD ... MAXLEN ~ 1000` 防膨胀；任务结束保留 30 分钟后删除 Stream。

**合计 2-3 天**（与 P0-2 阶段 2 合并则边际成本更低）。

## 十、风险与取舍

| 风险 | 缓解 |
|------|------|
| Redis 挂了事件全丢 | Redis 持久化（AOF）+ 事件丢失时前端可降级重试整个任务查询 |
| Stream 膨胀 | `MAXLEN` 限长 + 任务结束 TTL 删除 |
| 补发顺序与实时事件交错 | 补发完成后才进入实时订阅，串行化避免乱序 |
| last_seq 跨任务混淆 | Stream 按 task_key 隔离，序号只在单 Stream 内有意义 |

## 十一、面试讲法

> "事件从'发完即丢'改成 Redis Stream：worker 把事件 XADD 进 `stream:{task_key}`，Stream 自增 ID 就是事件序号。API 进程订阅 Stream 推 WS；前端重连带 `last_seq`，服务端 `XREAD` 从该序号补发差量，断线不丢、丢件可检测。前端固定 2s 重连改指数退避加抖动防雪崩。这跟任务队列共用 Redis，所以 P0-2/P0-3 合并做最划算。"

## 十二、工作量估计

- 独立做：2-3 天
- 与 P0-2 合并做：边际约 1 天（Redis 已接入）
