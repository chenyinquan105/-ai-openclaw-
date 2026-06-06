# 🔧 美团AI 竞赛漏洞修复计划

> 制定时间：2026-06-06
> 目标：在不动项目核心业务逻辑的前提下，覆盖竞赛的 5 个阻断级漏洞 + 5 个功能缺失

---

## 架构决策：不迁移到 OpenClaw TypeScript 插件

**原因**：
- 项目核心 6 个 Python Skill 共 ~2500 行代码，全部重写为 TypeScript + `defineToolPlugin` 需要 2-3 周
- 当前所有 Skill 之间的互调契约（`search_poi_matrix` → `solve_concurrent_timeline` → `clock.set_schedule`）都是 Python 函数调用，迁移到 TypeScript 需要全部重新设计
- 竞赛评审重点是"利用 OpenClaw 框架"的能力展示，**关键不是代码是 TS 还是 Python，而是使用了 OpenClaw 的哪些能力**

**替代方案**：**Python Skill 注册为 OpenClaw Agent Tool，通过 HTTP Bridge 桥接**
- 保留所有现有 Python 代码不变
- 创建一个薄层 TypeScript 插件 (`meituan-bridge`)，将 6 个 Python Skill 暴露为 OpenClaw Agent Tool
- HTTP 桥接：TypeScript 工具 → `http://localhost:5000/api/...` → Python Flask 后端
- OpenClaw 负责：Agent 对话、IM 通道、7×24 cron、Skill 发现/注册

```
┌──────────────────────────────────────────────────────┐
│  OpenClaw Gateway (TypeScript)                        │
│  ┌────────────────────────────────────────────────┐  │
│  │  meituan-bridge 插件                            │  │
│  │  - 6 个 Agent Tool (各映射一个 Python API)      │  │
│  │  - cron 后台任务 (动态沙盒事件注入)             │  │
│  │  - webchat 交互通道                             │  │
│  └────────────────────────────────────────────────┘  │
│           │ HTTP localhost:5000                       │
│  ┌────────▼───────────────────────────────────────┐  │
│  │  Python Flask 后端 (server.py + 6 skills)      │  │
│  │  - 全部现有代码不动                             │  │
│  │  - 新增 API: /api/anomaly/inject (事件注入)     │  │
│  │  - 新增 API: /api/memory/detect (语义偏好检测)  │  │
│  └────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

---

## Phase 1：打通 OpenClaw 框架（P0 阻断级）

### Step 1.1：创建 OpenClaw TypeScript 桥接插件 `meituan-bridge`

**文件**：`skills/meituan-bridge/`（TypeScript 插件目录）

**目标**：
- 在 OpenClaw Gateway 中注册 6 个 Agent Tool
- 解决 "项目不是 OpenClaw 插件" 的阻断问题
- 让 OpenClaw 的 webchat/sessions 原生能力驱动对话

**工具映射**：

| Agent Tool 名称 | → | Python API | 说明 |
|---|---|---|---|
| `meituan_search_poi` | → | `POST /api/start` | POI 搜索 + 选店 + 排程 |
| `meituan_clock_status` | → | `GET /api/clock/status` | 虚拟时钟状态 |
| `meituan_clock_forward` | → | `POST /api/clock/forward` | 快进时钟 |
| `meituan_clock_jump` | → | `POST /api/clock/jump` | 跳转时钟 |
| `meituan_clock_events` | → | `GET /api/clock/events` | 消费触发事件 |
| `meituan_preference_read` | → | `GET /api/profile/get` | 读取偏好 |
| `meituan_preference_update` | → | `POST /api/profile/set` | 更新偏好 |
| `meituan_reminder_add` | → | `POST /api/reminder/add_task` | 添加提醒 |
| `meituan_reminder_remove` | → | `POST /api/reminder/remove_task` | 删除提醒 |
| `meituan_anomaly_inject` | → | `POST /api/anomaly/inject` | （新增）动态注入异常事件 |
| `meituan_pitfall_check` | → | `POST /api/pitfall/check` | 防踩坑检查 |

### Step 1.2：`openclaw.plugin.json` 清单

```json
{
  "id": "meituan-bridge",
  "name": "美团AI 时空沙盒技能包",
  "description": "美团本地生活全天候数字管家 — 6个核心技能通过 HTTP Bridge 注册为 OpenClaw Agent Tool",
  "version": "1.0.0",
  "activation": { "onStartup": true },
  "contracts": {
    "tools": [
      "meituan_search_poi",
      "meituan_clock_status", 
      "meituan_clock_forward",
      "meituan_clock_jump",
      "meituan_clock_events",
      "meituan_preference_read",
      "meituan_preference_update",
      "meituan_reminder_add",
      "meituan_reminder_remove",
      "meituan_anomaly_inject",
      "meituan_pitfall_check"
    ]
  },
  "configSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "pythonBackendUrl": {
        "type": "string",
        "default": "http://localhost:5000"
      }
    }
  }
}
```

---

## Phase 2：7×24 小时自主后台任务（P0 阻断级）

### Step 2.1：动态沙盒事件注入引擎

**新增 Python API**：`POST /api/anomaly/inject`

随机从以下事件池中抽取一个注入：

```python
_ANOMALY_POOL = [
    {"class": "STORE_CLOSURE", "description": "{}店因突发电力故障暂停营业", "duration": 240},
    {"class": "QUEUE_FULL", "description": "{}店当前排队已满，预计等待90分钟", "duration": 90},
    {"class": "WEATHER_EVENT", "description": "雷暴预警，建议减少步行出行", "duration": 120},
    {"class": "TRAFFIC_CONTROL", "description": "{}店周边交通管制，建议绕行", "duration": 60},
]
```

**注入逻辑**（在 `generic_poi_searcher.py` 中新增 `random_anomaly_inject()`）：

1. 从 `_MOCK_POI_DB` 随机选一个 shop
2. 从事件池随机选一个事件类
3. 填充 shop 名称
4. 写入 `environmental_context.active_anomalies`
5. 触发 `anomaly_sensor_skill` 运行

### Step 2.2：OpenClaw Cron 任务注册

在 `meituan-bridge` 插件中使用 `api.registerService()` 注册 3 个后台定时任务：

| Cron 任务 | 频率 | 功能 |
|---|---|---|
| `meituan-sandbox-tick` | 每 5 分钟 | 随机注入动态事件（餐厅满位、天气变化等） |
| `meituan-clock-poll` | 每 10 秒 | 轮询虚拟时钟事件，主动推送到 webchat |
| `meituan-queue-monitor` | 每 3 分钟 | 检查队列状态变化 |

---

## Phase 3：管线变异器 + 实时推送（P1 阻断级）

### Step 3.1：实现 `virtual_pipeline_mutate` 

**新增 Python API**：`POST /api/pipeline/mutate`

在 `server.py` 中实现管线变异逻辑：

- `SWAP_NODE`：从 POI 数据库中搜索同品类的替选店铺（调用 `search_poi_matrix`），用替选店替换受灾节点
- `BYPASS_NODE`：移除受灾节点，重新调用 `solve_concurrent_timeline` 生成新时间线
- `POSTPONE_NODE`：将受灾节点延后到 `impact_duration_minutes` 分钟后

### Step 3.2：WebSocket/SSE 实时推送

在 `server.py` 中添加 Flask-SocketIO（或轻量 SSE endpoint）：

- 虚拟时钟每次推进 → 推送到前端
- 异常事件触发 → 推送到前端弹 Dialog
- 提醒事件 → 推送到前端

---

## Phase 4：功能缺失补齐（P2/P3）

### Step 4.1：膳食忌口二次过滤

在 `generic_poi_searcher.py` 的 `search_poi_matrix()` 中新增 `dietary_restrictions` 参数：
- 过滤掉 `top_comments` 中包含忌口食材的店铺
- 过滤掉 `signature_dishes` 中包含忌口关键词的店铺

### Step 4.2：交互后语义检测触发偏好写入

**新增 Python API**：`POST /api/memory/detect`

在用户每轮对话结束后，调用 LLM 做一次语义检测：
- 输入：用户最新消息 + 当前偏好
- 输出：检测到的偏好变化（如有）
- 自动调用 `_write_profile()` 更新

### Step 4.3：`walking_tolerance_meters` 实际生效

修改 `destination_anti_pitfall.py` 的 `execute_anti_pitfall_skill()`：
- 读取 `input_payload.get("walking_tolerance_meters", 800)`
- 计算每个节点的步行距离
- 超过阈值时自动将 `transport` 设为 `"打车"` 并产出一个 `virtual_call_taxi` trigger

### Step 4.4：交通模式计算修复

修改 `_run_schedule()` / `server.py` 中的 `spatial_matrix` 构建逻辑：
- 根据 `transport_priority` 自动计算路线 distance 和 `transport_mode`
- 默认 WALK → 根据偏好切换为 TAXI/DRIVE/METRO

---

## 实现关系依赖图

```
Phase 1 (OpenClaw 插件) ── 前置依赖 ──→ Phase 2 (cron 后台)
     │                                       │
     │                                       ├──→ Phase 3.1 (管线变异)
     │                                       ├──→ Phase 3.2 (实时推送)
     │                                       │
     └──→ 不依赖其他 Phase                    └──→ Phase 4 (功能补齐)
```

---

## 预计工作量

| Phase | 步骤数 | 预计时间 |
|-------|--------|----------|
| Phase 1: OpenClaw 桥接插件 | 创建 TS 插件 + 11 个 Tool | 4-6h |
| Phase 2: 7×24 后台任务 | 事件注入引擎 + cron 注册 | 2-3h |
| Phase 3.1: 管线变异器 | mutation 逻辑 + API | 2-3h |
| Phase 3.2: 实时推送 | SSE/WebSocket | 1-2h |
| Phase 4: 功能补齐 | 4 个缺失功能 | 3-4h |
| **总计** | | **12-18h** |

---

## 关键风险

| 风险 | 缓解措施 |
|------|---------|
| OpenClaw 插件 SDK 版本兼容性 | 使用 `defineToolPlugin`（v2026.5.17+），检查当前 OpenClaw 版本 |
| Flask + OpenClaw 双进程通信 | 统一使用 `localhost:5000` HTTP，test-first |
| cron 定时器精度 | 使用 `api.registerService()` + `setInterval`，不依赖系统 cron |
| 大模型 tool call 不够精准 | 为每个 tool 提供中文 description + 少样本示例 |
