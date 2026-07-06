# Requirements Document

## Introduction

美团 AI 管家用户在与系统对话时，系统采用的是"关键词映射→工具"模式，而非"语义理解→结构化参数→多步工具调用"模式。当前 LLM 调用链存在 6 类架构缺陷，导致位置相关查询全部使用硬编码默认坐标、复杂查询无法分解为多步工具调用、编辑操作缺乏确认步骤、JSON 解析脆弱等问题。本需求旨在将系统从关键词匹配模式升级为语义理解驱动的多步工具调用模式。

## Boundary Context

- **In scope**: LLM 对话工具链（CHAT_TOOLS）、系统提示词、行程状态查询、编辑类操作的确认流程、LLM 输出解析健壮性、坐标回退的可观测性
- **Out of scope**: LLM 模型更换、前端 UI 重构、Skill 模块业务逻辑修改（如 amap_poi 搜索算法本身）、新增外部 API 依赖、OpenClaw Gateway 插件目录结构调整
- **Adjacent expectations**: `skills/amap_poi/amap_poi.py` 中的 `geocode()` 和 `search_nearby()` 方法已实现且可用；Flask 端点 `/api/poi/geocode` 和 `/api/poi/nearby` 已实现；meituan-bridge 插件已注册 `meituan_poi_geocode` 和 `meituan_poi_nearby` 工具

## Requirements

### Requirement 1: 位置感知的商户搜索

**Objective:** 作为用户，我希望提及具体地名（如"东来顺饭店旁边"）时，系统能自动解析地名坐标并搜索该位置周边的商户，而非使用固定的默认坐标。

#### Acceptance Criteria
1. When 用户在对话中提及具体地名并询问周边商户，the 美团 AI 管家 shall 先对该地名执行地理编码获取坐标，再以该坐标为中心搜索周边商户。
2. When 用户使用"XX附近"、"XX旁边"、"XX周边"等位置参照表达，the 美团 AI 管家 shall 将地名解析为经纬度坐标后再执行搜索。
3. When 地理编码返回有效坐标，the 美团 AI 管家 shall 使用该坐标作为周边搜索的中心点。
4. If 地理编码无法解析用户提及的地名，then the 美团 AI 管家 shall 向用户明确说明无法识别该位置，并询问更具体的位置信息。
5. The 美团 AI 管家 shall 在对话工具集中包含地理编码和周边搜索工具，使 LLM 能够自主组合调用。

### Requirement 2: 复杂查询的分解式理解

**Objective:** 作为用户，我希望使用自然语言表达复杂的多步骤需求（如"6号上午去干点啥呢"），系统能自动分解为子任务并逐步调用工具，而非依赖关键词规则做单一分类。

#### Acceptance Criteria
1. When 用户输入包含时间、地点、活动类型等多维度信息的复杂查询，the 美团 AI 管家 shall 将其分解为子任务并依次调用相应工具。
2. When 查询同时涉及位置解析和商户搜索，the 美团 AI 管家 shall 按"地理编码→周边搜索"的顺序逐步执行。
3. When 查询涉及已有多日行程的某一天，the 美团 AI 管家 shall 先查询行程状态，再基于已有安排给出建议。
4. The 美团 AI 管家 shall 在系统提示词中引导 LLM 采用"先理解问题结构→确定所需工具→按序调用"的思维模式。
5. If 用户查询存在歧义（如地点名不明确、时间范围模糊），then the 美团 AI 管家 shall 主动询问澄清而非猜测执行。

### Requirement 3: 多日行程状态查询

**Objective:** 作为用户，我希望询问多日行程中任意一天的具体安排（如"6号上午有什么安排"、"第三天还有空吗"），系统能准确返回该日的行程时间线。

#### Acceptance Criteria
1. When 用户询问特定日期的行程安排，the 美团 AI 管家 shall 返回该日的时间线摘要，包含已安排的活动、时间段和空闲时段。
2. When 用户询问多日行程整体概况，the 美团 AI 管家 shall 返回每日的核心活动摘要。
3. When 当前为多日行程模式（trip_mode 为 multi-day），the 美团 AI 管家 shall 在行程状态查询中同时提供当日和多日数据。
4. If 用户询问的日期超出已规划的行程范围，then the 美团 AI 管家 shall 明确告知该日期暂无安排。

### Requirement 4: 编辑操作的两步确认

**Objective:** 作为用户，我希望在对行程进行修改时，系统先确认理解了我的意图再执行操作，避免因理解偏差直接修改行程。

#### Acceptance Criteria
1. When 用户发出行程编辑指令，the 美团 AI 管家 shall 先返回对用户意图的解读结果，待用户确认后再执行修改。
2. When 系统返回意图解读，the 系统 shall 以用户可理解的自然语言描述将要执行的操作及其影响范围。
3. If 用户确认解读正确，then the 美团 AI 管家 shall 执行对应的行程修改操作。
4. If 用户否定或修正解读结果，then the 美团 AI 管家 shall 基于用户反馈重新理解意图，不执行原解读对应的操作。
5. The 美团 AI 管家 shall 区分信息查询和修改操作——当用户仅提问时不触发确认流程。

### Requirement 5: 健壮的 LLM 输出解析

**Objective:** 作为系统运维者，我希望 LLM 输出的 JSON 能被可靠解析，解析失败时有明确且一致的降级行为，避免因格式偏差导致功能静默失败。

#### Acceptance Criteria
1. The 美团 AI 管家 shall 在所有需要提取结构化数据的 LLM 调用中使用结构化输出能力（如 JSON mode），而非依赖正则表达式从自由文本中提取 JSON。
2. If LLM 返回的内容无法解析为有效的结构化数据，then the 美团 AI 管家 shall 执行明确的降级行为（如要求 LLM 重新生成、返回用户要求澄清、或使用安全的默认值），而非静默失败。
3. The 美团 AI 管家 shall 在解析失败时输出警告日志，包含失败原因和原始响应摘要。
4. When 降级行为涉及用户交互，the 美团 AI 管家 shall 向用户提供清晰的错误提示和后续操作建议。

### Requirement 6: 坐标回退的可见性与可控性

**Objective:** 作为系统运维者，我希望当坐标数据不可用系统回退到默认值时能收到明确告警，且默认坐标的使用集中在可控的少数位置，便于维护和排查问题。

#### Acceptance Criteria
1. When 坐标参数无效或缺失导致系统使用默认坐标，the 美团 AI 管家 shall 输出警告日志，包含触发回退的调用上下文。
2. The 美团 AI 管家 shall 将默认坐标定义为单一可配置常量，而非在多处硬编码重复出现。
3. When 周边搜索被调用时，the 美团 AI 管家 shall 允许调用方显式传入中心坐标参数以覆盖默认值。
4. If 搜索请求中未提供坐标，then the 美团 AI 管家 shall 优先尝试从会话上下文中推断用户位置（如已选店铺坐标、上次搜索坐标），仅在无法推断时才使用默认值。
