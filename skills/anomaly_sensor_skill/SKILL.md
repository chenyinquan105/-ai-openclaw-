# Anomaly Sensor Skill — 异常传感器技能

## Skill Identity

| 字段 | 值 |
|------|-----|
| **Skill ID** | `anomaly_sensor_skill` |
| **入口函数** | `execute_anomaly_sensor_skill(input_payload: dict) -> dict` |
| **打断级别** | `dialog` (高打断 — 比 `destination_anti_pitfall` 的 `standard_button` 更高) |
| **核心能力** | 接收后端 Webhook 推送的时空异动（门店临时关店、天气突变等系统级事件），执行拓扑污染分析，生成阻断级弹窗触发器，驱动 Plan B 容灾管线变异。 |
| **依赖 Tool** | `virtual_pipeline_mutate` — 底层通用管线变异器，用于反射调用容灾动作。 |
| **设计哲学** | 纯数据驱动，零业务硬编码。输入协议完全泛化，通过 `fallback_directives` 解耦策略，所有扰动评估基于运行时节点拓扑索引，无任何静态业务假设。 |

---

## 语义描述触发（意图描述）

当系统满足以下条件之一时，Agent 应路由到 `anomaly_sensor_skill`：

1. **后端 Webhook 主动推送** — 收到门店关店、天气突变、交通管制等运营/环境异常事件通知。
2. **计划排程与实时环境冲突** — 在执行 `pipeline_executor` 或 `destination_anti_pitfall` 等技能时，检测到当前执行节点受 `active_anomalies` 影响，需要取消/推迟当前操作并触发容灾。
3. **拓扑连锁预警** — 上游节点已发生异常，需要预判下游节点的级联影响并提前告警。

**典型用户意图（自然语言等价）：**
- "检测到门店 X 因天气临时关闭，请更新排程路径。"
- "系统收到关店通知，触发 Plan B 并通知用户。"
- "交通管制导致节点不可达，启动容灾变异。"
- "用户当前操作节点可能受外部异常影响，请立即打断并给出降级方案。"

---

## 输入协议（Input Protocol）

```json
{
  "pipeline_nodes": [
    {
      "node_id": "<string>",
      "node_name": "<string>",
      "node_type": "<string>",
      "sequence_order": <int>,
      "location": {
        "latitude": <float>,
        "longitude": <float>
      },
      "status": "<string>",
      "metadata": {}
    }
  ],
  "environmental_context": {
    "active_anomalies": [
      {
        "anomaly_id": "<string>",
        "anomaly_class": "<enum: STORE_CLOSURE | WEATHER_EVENT | TRAFFIC_CONTROL | RESOURCE_SHORTAGE | UNKNOWN_DISRUPTION>",
        "target_node_id": "<string>",
        "description": "<string>",
        "impact_duration_minutes": <int>,
        "fallback_directives": {
          "action_required": "<string>",
          "attribute_filter": {}
        }
      }
    ]
  }
}
```

### 字段说明

| 路径 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `pipeline_nodes` | `array` | 否 | 当前管线排程的完整节点列表。为空时不影响异常检测，但拓扑锁定会退化为 ID 引用。 |
| `pipeline_nodes[].node_id` | `string` | 是 | 节点的物理/逻辑唯一标识。 |
| `pipeline_nodes[].node_name` | `string` | 否 | 节点可读名称。缺失时降级为 `Node_{target_node_id}`。 |
| `environmental_context` | `object` | 否 | 环境上下文容器。缺失等效于无异动。 |
| `environmental_context.active_anomalies` | `array` | 否 | 活跃异常列表。为空时直接返回 `SUCCESS` 零输出。 |
| `active_anomalies[].anomaly_id` | `string` | 是 | 异常事件的全局唯一 ID。 |
| `active_anomalies[].anomaly_class` | `string` | 是 | 异常分类，用于告警标题标签。 |
| `active_anomalies[].target_node_id` | `string` | 是 | 受影响的目标节点 ID。为空时该异常被静默跳过。 |
| `active_anomalies[].description` | `string` | 否 | 异常描述，默认值: `"时空环境发生不平稳异动"`。 |
| `active_anomalies[].impact_duration_minutes` | `int` | 否 | 预计影响时长（分钟），默认 `0`。 |
| `active_anomalies[].fallback_directives` | `object` | 否 | 容灾指令域。用于解耦策略逻辑。 |
| `fallback_directives.action_required` | `string` | 否 | 动作指令，默认 `"POSTPONE_NODE"`。 |
| `fallback_directives.attribute_filter` | `object` | 否 | Plan B 平替清洗时的过滤条件，默认 `{}`。 |

### 运行时校验

- 顶层 `input_payload` 必须为 `dict`；否则抛出 `ValueError`，返回 `ERROR` 状态。
- `active_anomalies` 为空 → 直接返回 `SUCCESS` 零输出。
- `target_node_id` 为空或不在 `node_indexer` 中 → 跳过该异常，但不会返回错误。

---

## 输出协议（Output Protocol）

```json
{
  "status": "<enum: SUCCESS | ERROR>",
  "localized_insights": [
    {
      "associated_node_id": "<string>",
      "title": "<string>",
      "content": "<string>"
    }
  ],
  "intent_triggers": [
    {
      "trigger_id": "<string>",
      "ui_manifest": {
        "component_type": "dialog",
        "prompt_text": "<string>",
        "confirm_label": "<string>"
      },
      "action_reflection": {
        "target_tools": ["virtual_pipeline_mutate"],
        "parameter_mapping": {
          "execute_intercept_hook": <bool>,
          "corrupted_node_id": "<string>",
          "mutation_directive": "<string>",
          "dynamic_filter": {},
          "delta_delay_minutes": <int>
        }
      }
    }
  ]
}
```

### 字段说明

| 路径 | 类型 | 说明 |
|------|------|------|
| `status` | `string` | `SUCCESS` — 正常完成；`ERROR` — 运行时异常，此时 `localized_insights` 含一条系统崩溃日志，`intent_triggers` 为空数组。 |
| `localized_insights` | `array` | 拓扑污染告警列表。空数组表示无异动。每条包含受影响节点 ID、告警标题和描述内容。 |
| `localized_insights[].associated_node_id` | `string` | 受异常影响的节点 ID。`ERROR` 时为 `"SYSTEM_CRITICAL"`。 |
| `localized_insights[].title` | `string` | 格式: `"⚠️ 空间管线拓扑污染告警 [{anomaly_class}]"`。 |
| `localized_insights[].content` | `string` | 自然语言描述，包含节点名和预估延误时长。 |
| `intent_triggers` | `array` | 阻断级弹窗触发器列表。每个触发器将驱动 main.py Hook 弹出打断 Dialog。 |
| `intent_triggers[].trigger_id` | `string` | 格式: `"tg_hook_{anomaly_id}"`。 |
| `intent_triggers[].ui_manifest.component_type` | `string` | **固定为 `"dialog"`** — 高打断级别弹窗，不可被用户静默忽略。 |
| `intent_triggers[].ui_manifest.prompt_text` | `string` | 弹窗提示文字，建议执行容灾预案。 |
| `intent_triggers[].ui_manifest.confirm_label` | `string` | 确认按钮文案，固定 `"执行 Plan B"`。 |
| `intent_triggers[].action_reflection.target_tools` | `array` | **固定 `["virtual_pipeline_mutate"]`** — 反射调用管线变异器。 |
| `intent_triggers[].action_reflection.parameter_mapping.execute_intercept_hook` | `bool` | 硬标记：**固定 `true`**，唤醒 main.py 拦截钩子。 |
| `intent_triggers[].action_reflection.parameter_mapping.corrupted_node_id` | `string` | 受灾节点 ID，透传自入参 `target_node_id`。 |
| `intent_triggers[].action_reflection.parameter_mapping.mutation_directive` | `string` | 变异策略指令（如 `SWAP_NODE` / `BYPASS_NODE` / `POSTPONE_NODE`），透传自 `fallback_directives.action_required`。 |
| `intent_triggers[].action_reflection.parameter_mapping.dynamic_filter` | `object` | Plan B 平替店过滤条件，透传自 `fallback_directives.attribute_filter`。 |
| `intent_triggers[].action_reflection.parameter_mapping.delta_delay_minutes` | `int` | 降级增时量化，透传自 `impact_duration_minutes`。 |

### 调用链契约

```
anomaly_sensor_skill  →  intent_triggers  →  main.py Hook  →  virtual_pipeline_mutate
                                                                    │
                                                                    └─ 管线变异容灾 (SWAP/BYPASS/REORDER)
```

- `main.py` 的拦截钩子应侦听 `intent_triggers[].action_reflection.parameter_mapping.execute_intercept_hook` 为 `true` 的条目，然后反射调用 `virtual_pipeline_mutate` 工具，传入 `parameter_mapping` 的内容（除 `execute_intercept_hook` 外）。
- `localized_insights` 仅供 UI 层展示 / 日志记录，不驱动管线变异逻辑。

---

## 少样本示例（Few-Shot Examples）

### 示例 1：门店关店异常 — 完整打断流

**输入：**

```json
{
  "pipeline_nodes": [
    {
      "node_id": "store_1234",
      "node_name": "北京市朝阳区望京店",
      "node_type": "destination",
      "sequence_order": 2,
      "location": { "latitude": 39.9947, "longitude": 116.4780 },
      "status": "pending"
    },
    {
      "node_id": "store_5678",
      "node_name": "北京市朝阳区国贸店",
      "node_type": "destination",
      "sequence_order": 3,
      "location": { "latitude": 39.9138, "longitude": 116.4605 },
      "status": "pending"
    }
  ],
  "environmental_context": {
    "active_anomalies": [
      {
        "anomaly_id": "anom_20260605_001",
        "anomaly_class": "STORE_CLOSURE",
        "target_node_id": "store_1234",
        "description": "望京店因突发电力故障当日暂停营业",
        "impact_duration_minutes": 480,
        "fallback_directives": {
          "action_required": "SWAP_NODE",
          "attribute_filter": { "district": "望京", "capacity_gte": 100 }
        }
      }
    ]
  }
}
```

**输出：**

```json
{
  "status": "SUCCESS",
  "localized_insights": [
    {
      "associated_node_id": "store_1234",
      "title": "⚠️ 空间管线拓扑污染告警 [STORE_CLOSURE]",
      "content": "检测到目标节点 [北京市朝阳区望京店] 遭遇环境突变。该时空异动预计引发管线整体延误约 480 分钟。"
    }
  ],
  "intent_triggers": [
    {
      "trigger_id": "tg_hook_anom_20260605_001",
      "ui_manifest": {
        "component_type": "dialog",
        "prompt_text": "系统已捕获非平稳时空阻断，建议立刻激活安全容灾预案：执行 [SWAP_NODE] 规避物理损耗。",
        "confirm_label": "执行 Plan B"
      },
      "action_reflection": {
        "target_tools": ["virtual_pipeline_mutate"],
        "parameter_mapping": {
          "execute_intercept_hook": true,
          "corrupted_node_id": "store_1234",
          "mutation_directive": "SWAP_NODE",
          "dynamic_filter": { "district": "望京", "capacity_gte": 100 },
          "delta_delay_minutes": 480
        }
      }
    }
  ]
}
```

---

### 示例 2：多异动叠加 — 级联告警

**输入：**

```json
{
  "pipeline_nodes": [
    {
      "node_id": "route_a_to_b",
      "node_name": "A点→B点主干道",
      "node_type": "transit",
      "sequence_order": 1,
      "status": "in_progress"
    }
  ],
  "environmental_context": {
    "active_anomalies": [
      {
        "anomaly_id": "anom_weather_002",
        "anomaly_class": "WEATHER_EVENT",
        "target_node_id": "route_a_to_b",
        "description": "暴雨红色预警，A→B 主干道积水严重",
        "impact_duration_minutes": 120,
        "fallback_directives": {
          "action_required": "BYPASS_NODE",
          "attribute_filter": {}
        }
      },
      {
        "anomaly_id": "anom_traffic_003",
        "anomaly_class": "TRAFFIC_CONTROL",
        "target_node_id": "route_a_to_b",
        "description": "交通管制，该路段禁止通行",
        "impact_duration_minutes": 180,
        "fallback_directives": {
          "action_required": "REORDER_PIPELINE",
          "attribute_filter": { "priority": "low" }
        }
      }
    ]
  }
}
```

**输出：**

```json
{
  "status": "SUCCESS",
  "localized_insights": [
    {
      "associated_node_id": "route_a_to_b",
      "title": "⚠️ 空间管线拓扑污染告警 [WEATHER_EVENT]",
      "content": "检测到目标节点 [A点→B点主干道] 遭遇环境突变。该时空异动预计引发管线整体延误约 120 分钟。"
    },
    {
      "associated_node_id": "route_a_to_b",
      "title": "⚠️ 空间管线拓扑污染告警 [TRAFFIC_CONTROL]",
      "content": "检测到目标节点 [A点→B点主干道] 遭遇环境突变。该时空异动预计引发管线整体延误约 180 分钟。"
    }
  ],
  "intent_triggers": [
    {
      "trigger_id": "tg_hook_anom_weather_002",
      "ui_manifest": {
        "component_type": "dialog",
        "prompt_text": "系统已捕获非平稳时空阻断，建议立刻激活安全容灾预案：执行 [BYPASS_NODE] 规避物理损耗。",
        "confirm_label": "执行 Plan B"
      },
      "action_reflection": {
        "target_tools": ["virtual_pipeline_mutate"],
        "parameter_mapping": {
          "execute_intercept_hook": true,
          "corrupted_node_id": "route_a_to_b",
          "mutation_directive": "BYPASS_NODE",
          "dynamic_filter": {},
          "delta_delay_minutes": 120
        }
      }
    },
    {
      "trigger_id": "tg_hook_anom_traffic_003",
      "ui_manifest": {
        "component_type": "dialog",
        "prompt_text": "系统已捕获非平稳时空阻断，建议立刻激活安全容灾预案：执行 [REORDER_PIPELINE] 规避物理损耗。",
        "confirm_label": "执行 Plan B"
      },
      "action_reflection": {
        "target_tools": ["virtual_pipeline_mutate"],
        "parameter_mapping": {
          "execute_intercept_hook": true,
          "corrupted_node_id": "route_a_to_b",
          "mutation_directive": "REORDER_PIPELINE",
          "dynamic_filter": { "priority": "low" },
          "delta_delay_minutes": 180
        }
      }
    }
  ]
}
```

---

### 示例 3：无声放行 — 无异动

**输入：**

```json
{
  "pipeline_nodes": [
    { "node_id": "store_a", "node_name": "A店" }
  ],
  "environmental_context": {
    "active_anomalies": []
  }
}
```

**输出：**

```json
{
  "status": "SUCCESS",
  "localized_insights": [],
  "intent_triggers": []
}
```
