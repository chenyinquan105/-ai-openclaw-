# time_master — 时间管家（虚拟时钟沙盒）

## Skill Identity

| Field | Value |
|-------|-------|
| **Skill ID** | `time_master` |
| **Module** | `skills.time_master.time_master` |
| **Entry Class** | `TimeMaster` (`get_master()` 获取全局单例) |
| **Dependencies** | 无第三方依赖（仅 `threading` 标准库） |
| **Domain** | 时间模拟 / 排程触发 / 虚拟时钟沙盒 |
| **Cross-Day** | 不支持跨天，限制在 00:00–23:59 |

## 语义描述触发（意图描述）

当用户或上游系统需要 **模拟时间推进**、**快进或跳转虚拟时钟**、**以倍速自动走时**、或 **触发排程中的任务事件** 时，调用本 Skill。

典型触发场景：

- "把时间快进 10/20/30 分钟" → `offset(session_id, delta_minutes)`（相对偏移）
- "把时间跳到 14:30" → `jump(session_id, target_time)`（绝对跳转）
- "启动走时，倍速 2x" → `start_auto_tick(session_id, speed=120)`
- "停止走时" → `stop_auto_tick(session_id)`
- "注册今天的工作节点" → `set_schedule(session_id, nodes, initial_time)`
- "查询/创建时钟会话" → `get_or_create_session(session_id, initial_time)`
- "消费触发的事件队列" → `pop_triggered_events(session_id)`

**核心设计原则：**

- 单一时钟源：服务端持有最终状态，客户端只发指令，不传当前时间
- 纯数学计算：不涉及任何系统真实时间
- 1 倍速 = 1 秒走 1 虚拟分钟；speed 枚举：60(1x) / 120(2x) / 180(3x)
- 排程联动：时钟推进后返回 `triggered_nodes`，由上层处理节点完成
- 多会话隔离：每个 `session_id` 拥有独立的 ClockState

## 输入协议（Input Protocol）

### 函数签名一览

| 函数 | 参数 | 说明 |
|------|------|------|
| `offset` | `session_id: str, delta_minutes: int` | 相对快进，推荐值 +10/+20/+30。**注意：** 源码无负数校验，传负值会倒退时间。调用方应自行限制 `delta_minutes >= 0`。**注意：** 源码无负数校验，传负值会倒退时间。调用方应自行限制 `delta_minutes >= 0`。
| `jump` | `session_id: str, target_time: str` | 绝对跳转，格式 `"HH:MM"`，范围 00:00–23:59 |
| `start_auto_tick` | `session_id: str, speed: float = 60.0` | 启动自动走时，speed ∈ [1, 1440] |
| `stop_auto_tick` | `session_id: str` | 停止自动走时 |
| `set_speed` | `session_id: str, speed: float` | 仅设倍速不启动走时，speed ∈ [1, 1440] |
| `set_schedule` | `session_id: str, nodes: list, initial_time: str = "08:00"` | 注册排程节点，自动排序。**注意：** 每个 node 必须含 `time` 字段（`"HH:MM"`），否则会抛出 KeyError。不含 `node_id` 或 `name` 的 node 不会报错但会丢失触发事件中的标识。
| `get_or_create_session` | `session_id: str, initial_time: str = "08:00"` | 获取/创建会话 |
| `remove_session` | `session_id: str` | 移除会话并停止走时 |
| `pop_triggered_events` | `session_id: str` | 消费事件队列，返回 list |

### 排程节点格式

```json
[
  {"time": "08:00", "node_id": "task_001", "name": "上班打卡"},
  {"time": "12:00", "node_id": "task_002", "name": "午餐"},
  {"time": "18:00", "node_id": "task_003", "name": "下班"}
]
```

- `time`: 必填，`"HH:MM"` 格式，24 小时制
- `node_id`: 必填，唯一标识
- `name`: 可选，人类可读名称

### 错误输入处理

| 条件 | 行为 |
|------|------|
| `target_time` 越界（不在 00:00–23:59） | 返回 ERROR，保留原时间 |
| `speed` 不在 [1, 1440] 范围 | 返回 ERROR，不修改状态 |
| `start_auto_tick` 已在运行 | 返回 ERROR，提示"已在运行中" |
| `session_id` 不存在（非创建型函数） | 自动创建或返回 None 取决于函数 |

## 输出协议（Output Protocol）

所有操作返回 **统一输出格式**：

```json
{
  "status": "SUCCESS | ERROR",
  "previous_virtual_time": "HH:MM",
  "new_virtual_time": "HH:MM",
  "elapsed_minutes": 0,
  "ticked_minutes_list": ["HH:MM", "HH:MM", ...],
  "triggered_nodes": [
    {"time": "12:00", "node_id": "task_002", "name": "午餐"}
  ],
  "error_message": ""
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `"SUCCESS"` 或 `"ERROR"` |
| `previous_virtual_time` | string | 操作前虚拟时间 `"HH:MM"` |
| `new_virtual_time` | string | 操作后虚拟时间 `"HH:MM"` |
| `elapsed_minutes` | int | 实际推进的分钟数（非差值，≥0） |
| `ticked_minutes_list` | string[] | 推进区间内每一分钟的时间点列表（闭区间） |
| `triggered_nodes` | object[] | 推进过程中命中的排程节点列表 |
| `error_message` | string | 错误描述，成功时为空字符串 `""` |

### 特殊输出行为

| 函数 | 附加字段 | 说明 |
|------|---------|------|
| `set_speed` | `"speed": float` | 返回当前设置的速度值 |
| `pop_triggered_events` | 返回值是 `list`，非标准输出格式 | 直接返回事件列表 |
| `stop_auto_tick` / 无推进的操作 | `elapsed_minutes: 0` | 走时为 0 |
| `jump` 越界错误 | `new_virtual_time === previous_virtual_time` | 时间不变化 |

## 少样本示例（Few-Shot Examples）

### 示例 1：相对快进 + 排程触发

**场景：** 当前 08:00，注册了两个节点（08:10 晨会、08:20 日报），然后快进 15 分钟。

```python
from skills.time_master.time_master import get_master

master = get_master()
session = "user_demo_1"

# 注册排程
master.set_schedule(session, [
    {"time": "08:10", "node_id": "standup_001", "name": "晨会"},
    {"time": "08:20", "node_id": "daily_report_001", "name": "日报提交"},
], initial_time="08:00")

# 快进 15 分钟
result = master.offset(session, 15)
```

**输出：**

```json
{
  "status": "SUCCESS",
  "previous_virtual_time": "08:00",
  "new_virtual_time": "08:15",
  "elapsed_minutes": 15,
  "ticked_minutes_list": [
    "08:00", "08:01", "08:02", "08:03", "08:04",
    "08:05", "08:06", "08:07", "08:08", "08:09",
    "08:10", "08:11", "08:12", "08:13", "08:14", "08:15"
  ],
  "triggered_nodes": [
    {"time": "08:10", "node_id": "standup_001", "name": "晨会"}
  ],
  "error_message": ""
}
```

**说明：** 时间从 08:00 → 08:15，经过 08:10 时命中并移除了"晨会"节点。"日报提交"节点未到时间，保留在排程中。

---

### 示例 2：绝对跳转 + 越界错误

**场景：** 当前 14:30，尝试跳转到 25:00（越界）。

```python
master = get_master()
master.get_or_create_session("user_demo_2", "14:30")
result = master.jump("user_demo_2", "25:00")
```

**输出：**

```json
{
  "status": "ERROR",
  "previous_virtual_time": "14:30",
  "new_virtual_time": "14:30",
  "elapsed_minutes": 0,
  "ticked_minutes_list": ["14:30"],
  "triggered_nodes": [],
  "error_message": "目标时间越界: 25:00，仅允许 00:00-23:59"
}
```

**说明：** 越界输入不会修改虚拟时钟，`new_virtual_time` 与 `previous_virtual_time` 一致，`status` 为 `"ERROR"`。`ticked_minutes_list` 只包含当前时间点。

---

### 示例 3：自动走时启停

**场景：** 启动 2x 倍速（120 虚拟分钟/秒），运行一段时间后停止。

```python
master = get_master()
master.get_or_create_session("user_demo_3", "09:00")

# 启动自动走时（2x 倍速）
result1 = master.start_auto_tick("user_demo_3", speed=120)
# ── 系统每秒自动推进 120 虚拟分钟 ──

# 停止自动走时
result2 = master.stop_auto_tick("user_demo_3")
```

**输出（启动）：**

```json
{
  "status": "SUCCESS",
  "previous_virtual_time": "09:00",
  "new_virtual_time": "09:00",
  "elapsed_minutes": 0,
  "ticked_minutes_list": ["09:00"],
  "triggered_nodes": [],
  "error_message": ""
}
```

**输出（停止）：**

```json
{
  "status": "SUCCESS",
  "previous_virtual_time": "09:00",
  "new_virtual_time": "09:00",
  "elapsed_minutes": 0,
  "ticked_minutes_list": ["09:00"],
  "triggered_nodes": [],
  "error_message": ""
}
```

**说明：** `start_auto_tick` 启动时不做推进（elapsed=0），启动后服务端 `threading.Timer` 每秒推进 `speed` 虚拟分钟并扫描排程。`stop_auto_tick` 取消定时器并保存当前时间。自动走时期间触发的节点会存入 `triggered_queue`，通过 `pop_triggered_events(session_id)` 异步消费。
