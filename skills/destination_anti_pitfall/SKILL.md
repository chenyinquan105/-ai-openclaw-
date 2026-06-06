# destination_anti_pitfall — 目的地防踩坑 Skill

---

## Skill Identity

| 字段 | 值 |
|---|---|
| **Skill ID** | `destination_anti_pitfall` |
| **名称** | 目的地防踩坑 |
| **版本** | 1.0.0 |
| **入口函数** | `execute_anti_pitfall_skill(input_payload, client=None, model="deepseek-chat")` |
| **辅助函数** | `get_pending_triggers(skill_output) -> list` / `dispatch_reflection(trigger) -> dict` |
| **触发时机** | 用户行程规划完成、店铺选择确认后，评估各节点的潜在风险/体感提示，以及是否需要前置导航或叫车 |

---

## 语义描述触发（意图描述）

当用户意图包含以下语义时，LLM 应当调用此 Skill：

- **出行前防呆检查**：出门前提醒用户带齐钥匙、手机、充电宝、身份证等物品
- **目的地体感/风险提示**：针对不同品类（电影院、火锅、理发、宠物店、健身房等）给出针对性防坑建议，如影院冷气足需带外套、火锅易沾味需备口香糖、理发店警惕推销等
- **行程动作按钮**：根据行程节点和交通方式，动态渲染导航按钮或叫车按钮（含餐饮节点时额外提供餐厅排号按钮）
- **触发条件**：用户已规划好行程（`pipeline_nodes` 非空），选择店铺后由上游 Pipeline 调用

---

## 输入协议（Input Protocol）

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "OpenClaw_Skill_Input_Protocol",
  "type": "object",
  "required": ["trip_id", "pipeline_nodes", "environmental_context"],
  "properties": {
    "trip_id": {
      "type": "string",
      "description": "本次行程实例的唯一分布式追踪 ID"
    },
    "current_node_index": {
      "type": "integer",
      "description": "当前执行到了第几个空间节点（可选，默认从头扫描）"
    },
    "pipeline_nodes": {
      "type": "array",
      "description": "用户本次行程中所有已被调度的空间实体节点序列",
      "items": {
        "type": "object",
        "required": ["node_id", "node_name", "category", "coordinate"],
        "properties": {
          "node_id": {
            "type": "string",
            "description": "POI 实体的唯一业务 ID"
          },
          "node_name": {
            "type": "string",
            "description": "POI 实体的物理名称，如「海底捞·望京店」"
          },
          "category": {
            "type": "string",
            "description": "泛化标准品类标签，与上游 generic_poi_searcher 品类体系对齐。合法值：hair, pet, cafe, gym, restaurant, cinema, laundry, japanese, hotpot"
          },
          "coordinate": {
            "type": "string",
            "description": "纬度,经度 字符串，如 39.9042,116.4074"
          },
          "attributes": {
            "type": "object",
            "description": "动态扩展属性域，例如是否需要人在场、预计停留时间等",
            "additionalProperties": true
          }
        }
      }
    },
    "environmental_context": {
      "type": "object",
      "required": ["timestamp", "weather_summary"],
      "properties": {
        "timestamp": {
          "type": "integer",
          "description": "当前触发时间戳（Unix 秒级）"
        },
        "weather_summary": {
          "type": "string",
          "description": "当前物理天气特征简述，如「晴转多云，26°C」"
        },
        "client_platform": {
          "type": "string",
          "description": "终端渠道，如 WECHAT, TELEGRAM, SLACK"
        }
      }
    },
    "transport": {
      "type": "string",
      "description": "用户选择的出行方式，可选值为「步行」或「打车」",
      "enum": ["步行", "打车"]
    }
  }
}
```

---

## 输出协议（Output Protocol）

```jsonc
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "OpenClaw_Skill_Output_Protocol",
  "type": "object",
  "required": ["status", "global_reminders", "localized_insights", "intent_triggers"],
  "properties": {
    "status": {
      "type": "string",
      "enum": ["SUCCESS", "ERROR"],
      "description": "执行状态标识"
    },
    "global_reminders": {
      "type": "array",
      "description": "全局物理强防呆提醒域，无论目的地为何处均强制高亮置顶渲染",
      "items": {
        "type": "object",
        "required": ["reminder_type", "display_text"],
        "properties": {
          "reminder_type": {
            "type": "string",
            "description": "提醒类型，枚举值：items_check, security, weather"
          },
          "display_text": {
            "type": "string",
            "description": "对用户展示的文案，支持 Markdown 格式"
          }
        }
      }
    },
    "localized_insights": {
      "type": "array",
      "description": "局部空间节点体感指引域，按节点动态绑定，每个节点对应一条",
      "items": {
        "type": "object",
        "required": ["associated_node_id", "title", "content"],
        "properties": {
          "associated_node_id": {
            "type": "string",
            "description": "对应输入接口中的 node_id，用于前端绑定卡片"
          },
          "title": {
            "type": "string",
            "description": "提示小标题，如「影厅环境防冻与装备指南」"
          },
          "content": {
            "type": "string",
            "description": "针对该特定节点深度优化的防坑/舒适度文本"
          }
        }
      }
    },
    "intent_triggers": {
      "type": "array",
      "description": "动态交互动作触发域。框架根据此域在前端渲染交互组件，并在触发时反射调用工具链",
      "items": {
        "type": "object",
        "required": ["trigger_id", "ui_manifest", "action_reflection"],
        "properties": {
          "trigger_id": {
            "type": "string",
            "description": "触发器唯一标识，格式 trg_flow_{timestamp}_{category} 或 trg_nav_{timestamp} 或 trg_taxi_{timestamp}"
          },
          "ui_manifest": {
            "type": "object",
            "required": ["component_type", "prompt_text", "confirm_label"],
            "properties": {
              "component_type": {
                "type": "string",
                "description": "UI 组件样式，当前支持 standard_button"
              },
              "prompt_text": {
                "type": "string",
                "description": "引导互动文案，如「是否需要为您导航前往 [店铺名]？」"
              },
              "confirm_label": {
                "type": "string",
                "description": "动作确认按钮上显示的文本，固定值「执行」"
              }
            }
          },
          "action_reflection": {
            "type": "object",
            "required": ["target_tools", "parameter_mapping"],
            "properties": {
              "target_tools": {
                "type": "array",
                "items": { "type": "string" },
                "description": "需要被触发的虚拟动作标识符列表。由虚拟后台事件总线统一注册，预注册动作及适用场景：\n  - virtual_call_taxi：叫车动作\n  - virtual_queue：餐厅排号动作\n  - virtual_weather_notice：天气提醒\n  - virtual_delay_warning：延误警告\n  - virtual_navigation：导航动作\n不在注册表中的标识符将被静默忽略"
              },
              "parameter_mapping": {
                "type": "object",
                "description": "运行时参数透传路由表，框架在此动态注入输入上下文中的真实变量值，属性见下表",
                "properties": {
                  "taxi_target_name":    { "type": "string" },
                  "taxi_target_coord":   { "type": "string" },
                  "queue_shop_category": { "type": "string" },
                  "nav_target_name":     { "type": "string" },
                  "nav_target_coord":    { "type": "string" },
                  "nav_transport":       { "type": "string" },
                  "request_timestamp":   { "type": "integer" }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

**品类 → 体感提示矩阵对照表**（规则引擎内置，无需 LLM 润色）：

| category | title | abstract_template |
|---|---|---|
| `cinema` | 影厅环境防冻与装备指南 | 影厅内冷气通常极足（约20°C），静坐极易受凉，强烈建议顺手带件薄外套。另外，部分特效厅不提供免费3D眼镜，建议包里自带。 |
| `hotpot` | 重装织物防味警示 | 吃火锅极易留下一身浓重风味。落座请立刻让服务员提供防护罩衣，强烈建议包里随身准备【口香糖】与【除味清新剂】及时解围。 |
| `restaurant` | 空间油烟与织物除味提示 | 部分餐厅通风较差易沾染明显油烟味，出门前强烈建议随身准备【口香糖】与【织物清新剂】以备不时之需。 |
| `japanese` | 烟熏环境与口气清新管理 | 部分烧鸟/居酒屋等密闭烟熏味较重，出门前强烈建议在随身包中塞入【口香糖】与【除味清新剂】。 |
| `hair` | 消费透明度物理防御 | 开剪前务必与发型师明确『洗剪吹』最终一口价，警惕中途推销任何高价药水、头皮护理或会员卡卡项。 |
| `pet` | 后台托管时效确认 | 宠物洗澡通常需要 1.5 至 2 小时，交付后无需现场死守。放下宠物后请务必与店员精确对齐预计接回的时间。 |
| `gym` | 运动装备及卫生防御 | 请检查行囊，确认带齐了干净的运动鞋、换洗衣物以及水杯。部分健身房不提供免费毛巾，建议自备。 |
| `laundry` | 资产交付前置清空核对 | 交付衣物前，请务必当场仔细掏空并核对所有衣袋，避免钥匙、硬币或重要小纸条遗留丢失。 |
| `cafe` | 移动办公舒适度与环保提示 | 如需长时间移动办公，请注意部分座位可能缺少电源插座。多数店铺自带杯可享受减免，建议随身携带保温杯。 |
| _其他_ | 出行前置提示 | 即将前往新空间，请注意保管好随身物品，保持机警。 |

**动作触发规则说明**：
- 行程中包含餐饮品类（hotpot / restaurant / japanese）→ 注册 FOOD_DELIVERY_FLOW 动作流
  - transport=打车 → 触发 `virtual_call_taxi` + `virtual_queue`
  - transport≠打车 → 触发 `virtual_navigation`
- 行程中无餐饮品类 → 弹单按钮
  - transport=打车 → 触发 `virtual_call_taxi`
  - transport≠打车 → 触发 `virtual_navigation`

---

## 少样本示例（Few-Shot Examples）

### 示例 1：打车 → 电影院 + 火锅店

**输入：**

```json
{
  "trip_id": "trip_abc123",
  "current_node_index": 0,
  "pipeline_nodes": [
    {
      "node_id": "poi_001",
      "node_name": "英皇影城",
      "category": "cinema",
      "coordinate": "39.9042,116.4074",
      "attributes": {}
    },
    {
      "node_id": "poi_002",
      "node_name": "海底捞·望京店",
      "category": "hotpot",
      "coordinate": "39.9884,116.4815",
      "attributes": {}
    }
  ],
  "environmental_context": {
    "timestamp": 1749121200,
    "weather_summary": "晴转多云，26°C",
    "client_platform": "WECHAT"
  },
  "transport": "打车"
}
```

**输出：**

```json
{
  "status": "SUCCESS",
  "global_reminders": [
    {
      "reminder_type": "items_check",
      "display_text": "出门前请确认是否带齐：钥匙、手机、充电宝、身份证。"
    }
  ],
  "localized_insights": [
    {
      "associated_node_id": "poi_001",
      "title": "影厅环境防冻与装备指南",
      "content": "影厅内冷气通常极足（约20°C），静坐极易受凉，强烈建议顺手带件薄外套。另外，部分特效厅不提供免费3D眼镜，建议包里自带。"
    },
    {
      "associated_node_id": "poi_002",
      "title": "重装织物防味警示",
      "content": "吃火锅极易留下一身浓重风味。落座请立刻让服务员提供防护罩衣，强烈建议包里随身准备【口香糖】与【除味清新剂】及时解围。"
    }
  ],
  "intent_triggers": [
    {
      "trigger_id": "trig_flow_1749121200_hotpot",
      "ui_manifest": {
        "component_type": "standard_button",
        "prompt_text": "行程包含餐饮节点 [海底捞·望京店]，是否需要为您一键餐厅排号和叫车？",
        "confirm_label": "执行"
      },
      "action_reflection": {
        "target_tools": ["virtual_call_taxi", "virtual_queue"],
        "parameter_mapping": {
          "taxi_target_name": "海底捞·望京店",
          "taxi_target_coord": "39.9884,116.4815",
          "queue_shop_category": "hotpot",
          "request_timestamp": 1749121200
        }
      }
    }
  ]
}
```

---

### 示例 2：步行 → 理发店 + 便利店（fallback 品类）

**输入：**

```json
{
  "trip_id": "trip_def456",
  "pipeline_nodes": [
    {
      "node_id": "poi_101",
      "node_name": "明星造型理发店",
      "category": "hair",
      "coordinate": "39.9200,116.4100",
      "attributes": {}
    }
  ],
  "environmental_context": {
    "timestamp": 1749135600,
    "weather_summary": "多云，22°C",
    "client_platform": "SLACK"
  },
  "transport": "步行"
}
```

**输出：**

```json
{
  "status": "SUCCESS",
  "global_reminders": [
    {
      "reminder_type": "items_check",
      "display_text": "出门前请确认是否带齐：钥匙、手机、充电宝、身份证。"
    }
  ],
  "localized_insights": [
    {
      "associated_node_id": "poi_101",
      "title": "消费透明度物理防御",
      "content": "开剪前务必与发型师明确『洗剪吹』最终一口价，警惕中途推销任何高价药水、头皮护理或会员卡卡项。"
    }
  ],
  "intent_triggers": [
    {
      "trigger_id": "trig_nav_1749135600",
      "ui_manifest": {
        "component_type": "standard_button",
        "prompt_text": "是否需要为您导航前往 [明星造型理发店]？",
        "confirm_label": "执行"
      },
      "action_reflection": {
        "target_tools": ["virtual_navigation"],
        "parameter_mapping": {
          "nav_target_name": "明星造型理发店",
          "nav_target_coord": "39.9200,116.4100",
          "nav_transport": "步行",
          "request_timestamp": 1749135600
        }
      }
    }
  ]
}
```
