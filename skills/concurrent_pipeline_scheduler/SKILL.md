# concurrent_pipeline_scheduler

## Skill Identity

| 属性 | 值 |
|---|---|
| **技能名称 (Skill Name)** | concurrent_pipeline_scheduler |
| **源文件 (Source)** | `concurrent_pipeline_scheduler.py` |
| **入口函数 (Entry Point)** | `solve_concurrent_timeline(task_list, spatial_matrix, current_time_str, user_confirmed_tasks=None, user_rejected_tasks=None) -> dict` |
| **核心能力** | 多任务并发排程引擎。根据空间位置、交通耗时、固定锚点时间，对"人在场执行"和"放下即走"两类任务进行最优路线编排与冲突消解。 |
| **适用场景** | 用户选定多家店铺后，排程各任务的执行顺序、计算交通时间、处理时间冲突、生成完整时间线。 |

---

## 语义描述触发（意图描述）

用户出现以下含义时，视为触发该技能：

- 排程 / 排时间 / 安排顺序
- 规划路线 / 怎么走 / 先去哪再去哪
- 时间冲突 / 赶得上吗 / 会不会迟到
- 定行程 / 排行程 / 帮我安排
- 帮我看先做什么后做什么 / 怎么走最顺

典型用户表达示例：

> "我选好了这几家店，帮我排一下先去哪家后去哪家。"
> "先去理发再去干洗会绕路吗？"
> "理发店10点开门，但我9点出门来得及吗？"
> "我想先去干洗店放衣服，再去吃饭，最后去健身房，赶得上吗？"

当触发条件满足时，调用该技能的核心函数 `solve_concurrent_timeline` 完成排程。

---

## 输入协议（Input Protocol）

### 函数签名

```python
def solve_concurrent_timeline(
    task_list: list,
    spatial_matrix: dict,
    current_time_str: str,
    user_confirmed_tasks: list = None,
    user_rejected_tasks: list = None
) -> dict:
```

### 字段说明

#### `task_list` — 任务列表 (required)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `task_id` | string | ✅ | 任务唯一标识，例如 `"task_001"` |
| `name` | string | ✅ | 任务名称，例如 `"理发"`, `"取干洗"`, `"健身房"` |
| `location_id` | string | ✅ | 该任务对应的位置 ID，例如 `"loc_barber"` |
| `coord` | string | ✅ | 经纬度坐标，格式 `"lat,lng"`，例如 `"39.9087,116.3975"` |
| `human_needed` | boolean | ✅ | `true` = 人在场执行（理发/吃饭/健身）；`false` = 可放下即走（宠物/干洗） |
| `duration_minutes` | int | ✅ | 任务所需时长（分钟） |
| `category` | string | ✅ | 任务类别标签，例如 `"grooming"`, `"laundry"`, `"fitness"` |
| `fixed_start_time` | string | ❌ | 固定开始时间 `"HH:MM"`，可选。若有则作为锚点必须准时执行 |

```json
[
  {
    "task_id": "task_001",
    "name": "理发",
    "location_id": "loc_barber",
    "coord": "39.9087,116.3975",
    "human_needed": true,
    "duration_minutes": 40,
    "category": "grooming"
  },
  {
    "task_id": "task_002",
    "name": "取干洗",
    "location_id": "loc_laundry",
    "coord": "39.9140,116.4050",
    "human_needed": false,
    "duration_minutes": 15,
    "category": "laundry"
  },
  {
    "task_id": "task_003",
    "name": "健身",
    "location_id": "loc_gym",
    "coord": "39.9200,116.4100",
    "human_needed": true,
    "duration_minutes": 60,
    "category": "fitness",
    "fixed_start_time": "10:30"
  }
]
```

#### `spatial_matrix` — 空间矩阵 (required)

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `start_location_id` | string | ✅ | 出发点的位置 ID，例如 `"loc_current"` |
| `locations` | dict | ✅ | `{loc_id: {coord: "lat,lng"}}` 全部位置坐标字典 |
| `routes` | dict | ❌ | `{"locA->locB": {transport_mode, distance_meters}}` 预计算的路线数据。不存在时将用 haversine 公式推算步行距离 |

```json
{
  "start_location_id": "loc_current",
  "locations": {
    "loc_current": {"coord": "39.9000,116.3800"},
    "loc_barber": {"coord": "39.9087,116.3975"},
    "loc_laundry": {"coord": "39.9140,116.4050"},
    "loc_gym": {"coord": "39.9200,116.4100"}
  },
  "routes": {
    "loc_current->loc_barber": {"transport_mode": "WALK", "distance_meters": 1200},
    "loc_barber->loc_laundry": {"transport_mode": "TAXI", "distance_meters": 800},
    "loc_laundry->loc_gym": {"transport_mode": "WALK", "distance_meters": 600}
  }
}
```

#### `current_time_str` — 当前时间 (required)

字符串格式 `"HH:MM"`。例如 `"09:00"`。

#### `user_confirmed_tasks` — 用户确认接受延误的任务列表 (optional)

`list[string]`，元素为已确认的 `task_id`。

```json
["task_002"]
```

#### `user_rejected_tasks` — 用户拒绝的任务列表 (optional)

`list[string]`，元素为已拒绝的 `task_id`。

```json
["task_002"]
```

---

## 输出协议（Output Protocol）

### 返回格式

```json
{
  "status": "SUCCESS | CONFLICT | CONFIRM_REQUIRED",
  "timeline": [
    {
      "time": "HH:MM",
      "action": "DEPART | MOVE | DROP_TASK | START_TASK | WAIT | PICK_TASK",
      "target_location_id": "string",
      "next_location_id": "string",
      "task_id": "string",
      "memo": "string"
    }
  ],
  "suggested_departure_time": "HH:MM",
  "total_duration_minutes": 0,
  "conflict_task": {},
  "delay_minutes": 0,
  "message": ""
}
```

### 状态枚举

| status | 含义 | 后续处理 |
|---|---|---|
| `SUCCESS` | 排程成功，无冲突 | 直接按 `timeline` 展示给用户 |
| `CONFIRM_REQUIRED` | 存在 0~15 分钟的软冲突，需用户确认是否接受延误 | 将 `conflict_task` 和 `delay_minutes` 展示给用户，询问是否继续 |
| `CONFLICT` | 入参错误或无法排程 | 展示 `message` 中的错误提示 |

### Action 类型

| action | 含义 |
|---|---|
| `DEPART` | 从起点出发 |
| `MOVE` | 在两个地点之间移动。**注意：** 阶段 D（PICK 回程）代码当前未单独 push MOVE 事件，travel 时间直接累加在 `cur_min` 上，之后直接 push PICK_TASK。这是一个已知的源码 gap，后续版本会修复。
| `DROP_TASK` | 到达地点放下任务（human_needed=false 的场景） |
| `START_TASK` | 开始人在场执行的任务（human_needed=true 的场景） |
| `WAIT` | 等待（提前到达锚点时） |
| `PICK_TASK` | 回收放下即走任务的结果 |

### 核心逻辑概述

1. **空间重心策略**：找出带 `fixed_start_time` 的任务作为锚点，计算各非固定任务相对于"起点→锚点"路线的绕路程度。绕路比 >2.0 且绝对绕路 >3000m 的任务标记为严重绕路，延后到锚点之后执行。

2. **冲突预案模拟**：对不走绕路的任务模拟是否会导致锚点延误：
   - **延误 > 15 分钟** 或 **已被用户拒绝** → 自动延后到锚点之后
   - **延误 0~15 分钟且未被确认** → 返回 `CONFIRM_REQUIRED`
   - **已确认** 或 **无延误** → 准许执行

3. **执行顺序**：顺路 Drop → 顺路 Exec → 固定锚点任务 → 延后任务（按类型）→ PICK 收尾

4. **交通耗时**：支持 WALK / TAXI / DRIVE / METRO / BUS 五种模式，含启动/等候/接驳时间。

---

## 少样本示例（Few-Shot Examples）

### Example 1: 正常排程（SUCCESS）

**输入：**

```json
{
  "task_list": [
    {
      "task_id": "t1",
      "name": "取干洗",
      "location_id": "loc_laundry",
      "coord": "39.9140,116.4050",
      "human_needed": false,
      "duration_minutes": 15,
      "category": "laundry"
    },
    {
      "task_id": "t2",
      "name": "理发",
      "location_id": "loc_barber",
      "coord": "39.9087,116.3975",
      "human_needed": true,
      "duration_minutes": 40,
      "category": "grooming",
      "fixed_start_time": "10:00"
    }
  ],
  "spatial_matrix": {
    "start_location_id": "loc_home",
    "locations": {
      "loc_home": {"coord": "39.9000,116.3800"},
      "loc_laundry": {"coord": "39.9140,116.4050"},
      "loc_barber": {"coord": "39.9087,116.3975"}
    },
    "routes": {
      "loc_home->loc_laundry": {"transport_mode": "WALK", "distance_meters": 1500},
      "loc_laundry->loc_barber": {"transport_mode": "WALK", "distance_meters": 800},
      "loc_barber->loc_laundry": {"transport_mode": "WALK", "distance_meters": 800}
    }
  },
  "current_time_str": "09:00"
}
```

**输出：**

```json
{
  "status": "SUCCESS",
  "timeline": [
    {"time": "09:00", "action": "DEPART", "target_location_id": "loc_home", "next_location_id": null, "task_id": null, "memo": "准备出发"},
    {"time": "09:00", "action": "MOVE", "target_location_id": "loc_home", "next_location_id": "loc_laundry", "task_id": "t1", "memo": "前往 取干洗"},
    {"time": "09:19", "action": "DROP_TASK", "target_location_id": "loc_laundry", "next_location_id": null, "task_id": "t1", "memo": "放下物品，开始后台处理"},
    {"time": "09:24", "action": "MOVE", "target_location_id": "loc_laundry", "next_location_id": "loc_barber", "task_id": "t1", "memo": "前往 取干洗"},
    {"time": "09:34", "action": "START_TASK", "target_location_id": "loc_barber", "next_location_id": null, "task_id": "t2", "memo": "开始固定任务: 理发"},
    {"time": "10:24", "action": "PICK_TASK", "target_location_id": "loc_laundry", "next_location_id": null, "task_id": "t1", "memo": "完成回收: 取干洗"}
  ],
  "suggested_departure_time": "09:00",
  "total_duration_minutes": 84
}
```

**正确性验证（对齐实际源码行为）：**
- 家→干洗店 WALK 1500m：ceil(1500/80)=19 min
- 09:00 MOVE出发 +19 → 09:19 DROP_TASK
- +5 DROP_PICK → 09:24 MOVE +10 → 09:34 START_TASK
- 理发40min → 10:14 DONE
- **阶段D：源码无 MOVE push，travel 10min 累加进 cur_min，直接 push PICK_TASK(10:24)**
- 总耗时 84 min

---

### Example 2: 软冲突需确认（CONFIRM_REQUIRED）

**输入：**

```json
{
  "task_list": [
    {
      "task_id": "t1",
      "name": "取干洗",
      "location_id": "loc_laundry",
      "coord": "39.9140,116.4050",
      "human_needed": false,
      "duration_minutes": 15,
      "category": "laundry"
    },
    {
      "task_id": "t2",
      "name": "理发",
      "location_id": "loc_barber",
      "coord": "39.9087,116.3975",
      "human_needed": true,
      "duration_minutes": 40,
      "category": "grooming",
      "fixed_start_time": "09:30"
    }
  ],
  "spatial_matrix": {
    "start_location_id": "loc_home",
    "locations": {
      "loc_home": {"coord": "39.9000,116.3800"},
      "loc_laundry": {"coord": "39.9140,116.4050"},
      "loc_barber": {"coord": "39.9087,116.3975"}
    },
    "routes": {
      "loc_home->loc_laundry": {"transport_mode": "WALK", "distance_meters": 2500},
      "loc_laundry->loc_barber": {"transport_mode": "WALK", "distance_meters": 1200}
    }
  },
  "current_time_str": "09:00"
}
```

**输出：**

```json
{
  "status": "CONFIRM_REQUIRED",
  "conflict_task": {
    "task_id": "t1",
    "name": "取干洗",
    "location_id": "loc_laundry",
    "human_needed": false,
    "duration_minutes": 15
  },
  "delay_minutes": 11,
  "fixed_task_name": "理发",
  "message": "执行[取干洗]将使[理发]延误11分钟，是否继续？"
}
```

---

### Example 3: 已确认接受延误（SUCCESS + 已确认）

**输入**（延续 Example 2 任务，增加返回路线）：

```json
{
  "task_list": [
    {
      "task_id": "t1",
      "name": "取干洗",
      "location_id": "loc_laundry",
      "coord": "39.9140,116.4050",
      "human_needed": false,
      "duration_minutes": 15,
      "category": "laundry"
    },
    {
      "task_id": "t2",
      "name": "理发",
      "location_id": "loc_barber",
      "coord": "39.9087,116.3975",
      "human_needed": true,
      "duration_minutes": 40,
      "category": "grooming",
      "fixed_start_time": "09:30"
    }
  ],
  "spatial_matrix": {
    "start_location_id": "loc_home",
    "locations": {
      "loc_home": {"coord": "39.9000,116.3800"},
      "loc_laundry": {"coord": "39.9140,116.4050"},
      "loc_barber": {"coord": "39.9087,116.3975"}
    },
    "routes": {
      "loc_home->loc_laundry": {"transport_mode": "WALK", "distance_meters": 2500},
      "loc_laundry->loc_barber": {"transport_mode": "WALK", "distance_meters": 1200},
      "loc_barber->loc_laundry": {"transport_mode": "WALK", "distance_meters": 1200}
    }
  },
  "current_time_str": "09:00",
  "user_confirmed_tasks": ["t1"]
}
```

**输出：**

```json
{
  "status": "SUCCESS",
  "timeline": [
    {"time": "09:00", "action": "DEPART", "target_location_id": "loc_home", "next_location_id": null, "task_id": null, "memo": "准备出发"},
    {"time": "09:00", "action": "MOVE", "target_location_id": "loc_home", "next_location_id": "loc_laundry", "task_id": "t1", "memo": "前往 取干洗"},
    {"time": "09:32", "action": "DROP_TASK", "target_location_id": "loc_laundry", "next_location_id": null, "task_id": "t1", "memo": "放下物品，开始后台处理"},
    {"time": "09:37", "action": "MOVE", "target_location_id": "loc_laundry", "next_location_id": "loc_barber", "task_id": "t1", "memo": "前往 取干洗"},
    {"time": "09:52", "action": "START_TASK", "target_location_id": "loc_barber", "next_location_id": null, "task_id": "t2", "memo": "开始固定任务: 理发"},
    {"time": "10:47", "action": "PICK_TASK", "target_location_id": "loc_laundry", "next_location_id": null, "task_id": "t1", "memo": "完成回收: 取干洗"}
  ],
  "suggested_departure_time": "09:00",
  "total_duration_minutes": 107
}
```

**正确性验证（对齐实际源码行为）：**
- 家→干洗店 WALK 2500m：ceil(2500/80)=32 min
- 09:00 MOVE出发 +32 → 09:32 DROP_TASK
- +5 DROP_PICK → 09:37 MOVE +15 → 09:52 START_TASK
- 理发40min → 10:32 DONE
- **阶段D：源码无 MOVE push，travel 15min 累加进 cur_min，直接 push PICK_TASK(10:47)**
- 总耗时 107 min
