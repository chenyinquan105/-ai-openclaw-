# task_reminder_skill —— 服药/喝水任务提醒引擎

## Skill Identity

**Skill 名称：** `task_reminder_skill`
**职责：** 在沙盒虚拟时钟环境（Time Master）驱动下，管理老年人每日健康任务的提醒、响铃、用户交互、催促升级、防重复服药闭环。
**一句话定位：** 基于状态机的服药/喝水提醒超时催促引擎，覆盖从初次响铃→用户确认→吞服确认→超时催促→紧急联络的完整生命周期。

**核心函数：**
| 函数 | 用途 |
|------|------|
| `process_reminder_pipeline()` | 核心时空过筛：处理 Time Master 刚触发的事件 + 定期盘点挂起事件是否超时 |
| `handle_user_action()` | 接收用户交互输入，驱动内部状态机流转 |

---

## 语义描述触发（意图描述）

当用户的描述或当前场景满足以下任一条，应当路由到此 Skill：

- 🕐 "吃药提醒" / "喝水提醒" / "该吃药了" / "药的时间到了"
- 🕐 "提醒响了" / "响铃了" / "振动了" / "手机一直在响"
- 🕐 "催促吃药" / "怎么还没吃" / "再提醒一下"
- 🕐 "确认吃药" / "我去拿药" / "延后" / "还没吃饭"
- 🕐 "我已经吃了" / "吞服药片" / "吃过了" / "吃完了"
- 🕐 "紧急联系人" / "联系家属" / "没响应" / "45分钟了"
- 🕐 任何涉及**时间驱动的健康任务提醒**场景

**不归属的场景（应当拒绝/回退）：**
- ❌ 普通闹钟/倒计时（非健康任务）
- ❌ 药物库存管理、处方审核、医嘱理解
- ❌ 聊天机器人闲聊

---

## 输入协议（Input Protocol）

### `process_reminder_pipeline()`
```python
def process_reminder_pipeline(
    session_id: str,                    # 会话 ID
    ticked_minutes: List[str],          # ❗ 兼容性保留参数，函数体内未使用。调用方可传 []
    triggered_events: List[dict],       # 由 time_master 触发的事件列表
    time_master,                        # TimeMaster 实例引用（用于读取当前虚拟时间、注入排程）
) -> List[dict]
```

**`triggered_events` 元素 Schema：**
```json
{
  "type":  "WATER | MED",
  "id":    "唯一事件 ID (string)",
  "time":  "HH:MM 格式触发时间 (string)",
  "name":  "事件名称如药品名 (string)"
}
```

### `handle_user_action()`
```python
def handle_user_action(
    session_id: str,      # 会话 ID
    user_input: str,      # 用户输入文本："1" / "2" / "我已吞服药片" / "吃了"
    current_time: str,    # 兼容降级用时间
    time_master,          # TimeMaster 实例引用（优先从 time_master 读当前虚拟时间）
) -> dict
```

**允许的 `user_input` 值：**
| 值 | 含义 | 触发场景 |
|----|------|----------|
| `"1"` | 确认去拿药 | RINGING 状态时用户响应 |
| `"2"` | 延后30分钟 | RINGING 状态时用户选择推迟 |
| `"我已吞服药片"` 或 `"吃了"` | 确认已吞服 | PENDING_SWALLOW 状态时用户回复 |

---

## 输出协议（Output Protocol）

### `process_reminder_pipeline()` 输出
返回 `List[dict]`，每个元素格式：

```json
{
  "type":    "WATER_UI_ALERT | MED_RINGING_ALERT | MED_DUPLICATE_BLOCK | MED_URGE_LIGHT | MED_URGE_HEAVY | MED_ESCALATION_CRITICAL",
  "time":    "HH:MM",
  "message": "中文提示文本（含 emoji 和格式化换行）"
}
```

**各 type 的语义与触发条件：**

| type | 含义 | 触发条件 |
|------|------|----------|
| `WATER_UI_ALERT` | 🥤 喝水提醒通知 | Time Master 触发 `WATER` 事件 |
| `MED_RINGING_ALERT` | 🔔 吃药初次响铃（含交互按钮） | Time Master 触发 `MED` 事件，且未有过重复服药记录 |
| `MED_DUPLICATE_BLOCK` | ⚠️ 防重复服药拦截 | Time Master 触发 `MED` 事件，但今日已吃过该药 |
| `MED_URGE_LIGHT` | 🔔 初次催促（15分钟未响应） | RINGING/PENDING_SWALLOW 状态超时 15 分钟无交互 |
| `MED_URGE_HEAVY` | ⚠️ 二次强震动催促（30分钟） | 超时 30 分钟无交互 |
| `MED_ESCALATION_CRITICAL` | 🚨 联系紧急联络人（45分钟） | 超时 45 分钟无交互，自动触发强打断级警报 |

### `handle_user_action()` 输出
```json
{
  "status":  "PROCEED | POSTPONED | SUCCESS_CLOSED | INTERCEPTED | WAITING_SWALLOW | INVALID_INPUT | NO_ACTIVE_PERIOD",
  "message": "中文提示文本"
}
```

**各 status 的语义：**

| status | 含义 | 触发时机 |
|--------|------|----------|
| `PROCEED` | ✅ 确认去拿药，进入 PENDING_SWALLOW 状态 | 用户输入 `"1"` |
| `POSTPONED` | 🌾 延后30分钟成功，状态回 IDLE，注入了新排程 | 用户输入 `"2"` |
| `SUCCESS_CLOSED` | 🎉 吞服确认闭环，安全锁定防重复 | 用户输入 `"我已吞服药片"` / `"吃了"` |
| `INTERCEPTED` | 🚨 防重复拦截（用户想重复确认但已吃完） | 输入 `"1"` / `"吃了"` 但状态已是 `COMPLETED` |
| `WAITING_SWALLOW` | ℹ️ 仍在等待确认，提示用户回复 `"我已吞服药片"` | PENDING_SWALLOW 状态下收到非确认输入 |
| `INVALID_INPUT` | ⚠️ 无效输入 | RINGING 状态下收到非 `"1"` / `"2"` |
| `NO_ACTIVE_PERIOD` | ℹ️ 当前无活跃服药流程 | 无任何 RINGING/PENDING_SWALLOW 状态的药 |

### 状态机流转图

```
                          ┌──────────────────────────────┐
                          │  Time Master 触发 MED 事件    │
                          └──────────────┬───────────────┘
                                         ▼
                                    ┌─────────┐
             ┌──────────────────── │  IDLE   │ ◄─────────────────────┐
             │                      └────┬────┘                       │
             │  触发事件，进入              │                           │
             │  响铃状态                   ▼                           │
             │                      ┌─────────┐     用户输入 "2"       │
             │                      │ RINGING │ ──────────────────────┘
             │                      └────┬────┘  (延后30分钟,回IDLE)
             │                           │
             │       用户输入 "1"         │
             │       (去拿药)             │
             │                           ▼
             │                  ┌─────────────────┐
             │                  │ PENDING_SWALLOW │
             │                  └────────┬────────┘
             │                           │
             │   用户输入 "我已吞服药片"   │  等待吞服确认
             │   或 "吃了"                │
             │                           ▼
             │                  ┌─────────────┐
             └──────────────── │  COMPLETED  │
                               └─────────────┘
                                (防重复锁定)

    ┌─────────────────────────────────────────────────────────────┐
    │               超时催促链（基于时间差）                         │
    │                                                             │
    │  15分钟无交互  →  MED_URGE_LIGHT   (初次催促)                 │
    │  30分钟无交互  →  MED_URGE_HEAVY   (二次强震动)               │
    │  45分钟无交互  →  MED_ESCALATION_CRITICAL (联系紧急联络人)    │
    └─────────────────────────────────────────────────────────────┘
```

---

## 少样本示例（Few-Shot Examples）

### 示例 1: 服药完整闭环（初次响铃 → 确认 → 吞服 → 完成）

**Input 1（触发事件：`process_reminder_pipeline`）:**
```python
process_reminder_pipeline(
    session_id="session_demo_001",
    ticked_minutes=["08:00"],
    triggered_events=[
        {
            "type": "MED",
            "id": "med_001",
            "time": "08:00",
            "name": "硝苯地平控释片"
        }
    ],
    time_master=<TimeMaster 实例 (虚拟时间 08:00)>
)
```

**Output 1:**
```json
[
  {
    "type": "MED_RINGING_ALERT",
    "time": "08:00",
    "med_id": "med_001",
    "message": "🔔【📱 手机正在密集振动与响铃...】\n👵 提示：王奶奶，该服用 [硝苯地平控释片] 了！\n请选择操作：\n  [输入 1] ：确认，我现在就去拿药\n  [输入 2] ：还没吃饭，帮我延后30分钟提醒"
  }
]
```

**Input 2（用户交互：`handle_user_action`）:**
```python
handle_user_action(
    session_id="session_demo_001",
    user_input="1",
    current_time="08:00",
    time_master=<TimeMaster 实例 (虚拟时间 08:00)>
)
```

**Output 2:**
```json
{
  "status": "PROCEED",
  "message": "✅【系统进入安全监视程序】奶奶去拿药了。请您在【真正把药片吞服下去】之后，点击或回复【我已吞服药片】，这样系统才能彻底放心哦！"
}
```

**Input 3（用户确认吞服：`handle_user_action`）:**
```python
handle_user_action(
    session_id="session_demo_001",
    user_input="我已吞服药片",
    current_time="08:03",
    time_master=<TimeMaster 实例 (虚拟时间 08:03)>
)
```

**Output 3:**
```json
{
  "status": "SUCCESS_CLOSED",
  "message": "🎉【服药闭环成功】记录成功！奶奶已于 08:03 顺利服用 [硝苯地平控释片]。今日该服药事件安全锁死，防重复覆盖机制已激活！"
}
```

---

### 示例 2: 超时催促链（45分钟未响应 → 联系紧急联络人）

**Input（虚拟时间推进到 08:45 时调用 `process_reminder_pipeline`）:**
```python
process_reminder_pipeline(
    session_id="session_demo_002",
    ticked_minutes=["08:00", "08:15", "08:30", "08:45"],
    triggered_events=[],
    time_master=<TimeMaster 实例 (虚拟时间 08:45)>
)
```

**Output:**
```json
[
  {
    "type": "MED_URGE_LIGHT",
    "time": "08:15",
    "message": "🔔【⚠️ 系统初次响铃补发催促】(08:15)\n👵 药点 [08:00] 已超时 15 分钟未响应，再次响铃提醒服用 [硝苯地平控释片]。"
  },
  {
    "type": "MED_URGE_HEAVY",
    "time": "08:30",
    "message": "⚠️【🛑 系统二次强震动催促】(08:30)\n👵 药点 [08:00] 已超时 30 分钟未处理！奶奶，请尽快服用 [硝苯地平控释片]，健康第一！"
  },
  {
    "type": "MED_ESCALATION_CRITICAL",
    "time": "08:45",
    "message": "💥【🔴 触发紧急联络预案】\n❌ 药点 [08:00] 的 [硝苯地平控释片] 已连续 45 分钟无任何人工交互响应！\n🚨 系统已自动连线紧急联络人（家属张小明：13800000000），抛出强打断级警报通知！"
  }
]
```
