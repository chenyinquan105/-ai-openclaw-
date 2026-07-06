# Implementation Plan

## Task Format Template

Use whichever pattern fits the work breakdown:

### Major task only
- [ ] {{NUMBER}}. {{TASK_DESCRIPTION}}{{PARALLEL_MARK}}
  - {{DETAIL_ITEM_1}} *(Include details only when needed. If the task stands alone, omit bullet items.)*
  - _Requirements: {{REQUIREMENT_IDS}}_

### Major + Sub-task structure
- [ ] {{MAJOR_NUMBER}}. {{MAJOR_TASK_SUMMARY}}
- [ ] {{MAJOR_NUMBER}}.{{SUB_NUMBER}} {{SUB_TASK_DESCRIPTION}}{{SUB_PARALLEL_MARK}}
  - {{DETAIL_ITEM_1}}
  - {{DETAIL_ITEM_2}}
  - {{OBSERVABLE_COMPLETION_ITEM}} *(At least one detail item should state the observable completion condition for this task.)*
  - _Requirements: {{REQUIREMENT_IDS}}_ *(IDs only; do not add descriptions or parentheses.)*
  - _Boundary: {{COMPONENT_NAMES}}_ *(Only for (P) tasks. Omit when scope is obvious.)*
  - _Depends: {{TASK_IDS}}_ *(Only for non-obvious cross-boundary dependencies. Most tasks omit this.)*

> **Parallel marker**: Append ` (P)` only to tasks that can be executed in parallel. Omit the marker when running in `--sequential` mode.
>
> **Optional test coverage**: When a sub-task is deferrable test work tied to acceptance criteria, mark the checkbox as `- [ ]*` and explain the referenced requirements in the detail bullets.

## Tasks

### 1. Foundation: LLM API 层和配置基础

- [ ] 1.1 定义默认坐标集中常量
  - 在模块顶层定义单一坐标常量，替代分散的硬编码 `"39.93,116.45"` 字符串
  - 将 LLM 相关调用路径（POI 搜索默认值、对话上下文默认位置、handler 参数默认值）中的硬编码替换为常量引用
  - 完成标志：`grep "39.93,116.45" server.py` 在 LLM 调用路径中不再出现裸字符串，全部替换为常量引用
  - _Requirements: 6.2_

- [ ] 1.2 为 LLM 调用入口增加结构化输出支持
  - 在 LLM 客户端（`main.py` 中的 DeepSeek API 调用封装）的所有调用方法（单次调用和流式调用）中增加 `response_format` 可选参数
  - 当调用方传入 `{"type": "json_object"}` 时，将该参数透传至 DeepSeek API；不传时行为完全不变
  - 确保流式调用（`chat_stream` / `chat_stream_continue`）同样支持该参数的透传
  - 完成标志：任意 LLM 调用点传入 `response_format={"type": "json_object"}` 后，DeepSeek API 请求体中包含该字段
  - _Requirements: 5.1_

### 2. 位置感知搜索工具链

- [ ] 2.1 新增地理编码工具的 CHAT_TOOLS 定义和 handler
  - 在 CHAT_TOOLS 列表中新增 `geocode` 工具定义：接受 `address`（必填）和 `city`（可选，默认"北京"）参数
  - 在工具分发逻辑中新增 `geocode` handler 分支：调用现有地理编码能力，将地名解析为经纬度坐标
  - 当地址无法解析时，返回明确错误信息（含用户可理解的提示），而非静默回退
  - 完成标志：LLM 在对话中可调用 `geocode` 工具将"故宫"等地名解析为 `{lng, lat}` 坐标
  - _Requirements: 1.1, 1.2, 1.4, 1.5_

- [ ] 2.2 新增周边搜索工具的 CHAT_TOOLS 定义和 handler
  - 在 CHAT_TOOLS 列表中新增 `search_nearby` 工具定义：接受 `lng`（必填）、`lat`（必填）、`keywords`（可选）、`radius`（可选，默认 3000）参数
  - 在工具分发逻辑中新增 `search_nearby` handler 分支：调用现有周边搜索能力，以指定坐标为中心搜索商户
  - 当必填参数缺失或无效时返回明确错误
  - 完成标志：LLM 可在获取坐标后调用 `search_nearby` 工具搜索该位置周边的商户
  - _Requirements: 1.1, 1.3, 1.5_

### 3. 系统提示词升级为分解思维

- [ ] 3.1 (P) 重写对话系统提示词
  - 将当前基于关键词规则（"句末有「吗」「？」→信息查询"等）的意图分类提示词，替换为引导 LLM 分解思维的提示词
  - 新提示词应教 LLM：先分析用户问题包含哪些维度（时间？地点？活动类型？是否涉及已有行程？），再确定需要哪些工具及调用顺序
  - 保留现有身份定义（管家角色"小美"）、语气规则和核心约束（必须调用工具而非凭训练数据推荐、修改行程前需确认等）
  - 明确告知 LLM 可用的 `geocode→search_nearby` 工具链模式，以及何时应使用该模式
  - 完成标志：LLM 收到"东来顺饭店旁边有啥好玩的"时，能自主执行 geocode→search_nearby 两步调用
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_
  - _Boundary: 系统提示词_

### 4. 多日行程状态查询增强

- [ ] 4.1 (P) 增强行程状态查询以返回多日行程数据
  - 在获取行程状态的 handler 中检测当前行程模式（单日/多日）
  - 多日模式下：除现有单日目的地列表外，额外返回全部天数的摘要（每天活动数量、核心活动名称）和当前活跃日的详细时间线
  - 单日模式下：保持现有返回结构不变（向后兼容）
  - 当日索引超出已规划范围时，返回明确提示"该日期暂无安排"
  - 完成标志：多日行程中用户问"6号上午有什么安排"，LLM 能获取并回复第 6 天的具体时间线
  - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - _Boundary: get_trip_status handler_

### 5. 编辑操作的两步确认流程

- [ ] 5.1 (P) 单日行程编辑增加意图确认步骤
  - 在单日行程编辑的 LLM 调用中启用 JSON mode（通过 `response_format` 参数），要求 LLM 输出包含 `action`、`params`、`confirm_needed`（布尔值）、`interpretation`（自然语言解读）的结构化结果
  - 当 `confirm_needed` 为 true（修改型操作）时：缓存待确认意图到 `session_state["_pending_confirmation"]`，返回解读结果给用户确认，不立即执行
  - 当 `confirm_needed` 为 false（纯查询或简单操作）时：直接执行
  - 确保首次使用时 `_pending_confirmation` 键的缺失被优雅处理（使用 `.get()` 访问并提供 None 默认值），避免 KeyError
  - 增加确认回调路径：用户确认后读取缓存意图并执行；用户修正后基于反馈重新理解
  - 确认状态使用会话级存储，会话重启后自动清除
  - 完成标志：用户说"把火锅换成烤肉"，系统先回复"我理解为：将火锅店替换为烤肉店，确认吗？"，确认后才执行替换
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - _Boundary: api_edit_trip_
  - _Depends: 1.2_

- [ ] 5.2 (P) 多日行程编辑增加意图确认步骤
  - 在多日行程编辑的 LLM 调用中启用 JSON mode（通过 `response_format` 参数），采用与单日编辑相同的确认模式（`confirm_needed` + `interpretation` 输出字段 + `_pending_confirmation` 缓存）
  - 多日特有的操作类型（跨天移动、天数调整等）同样需要确认
  - 确保多日行程的确认结果可正确跨天执行
  - 完成标志：多日行程中用户说"把第三天的故宫移到第一天"，系统先确认理解再执行
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - _Boundary: api_multi_day_edit_
  - _Depends: 1.2_

### 6. 坐标回退可观测性

- [ ] 6.1 (P) 坐标默认值集中管理并增加回退告警
  - 在 POI 搜索函数中增加可选的 `center_coord` 参数，允许调用方显式传入坐标覆盖默认值
  - 在所有坐标回退到默认值的分支处增加警告日志输出，日志包含触发回退的函数名称和用户输入摘要（截断至 50 字符）
  - 当周边搜索被调用时，优先尝试从会话上下文中推断用户位置（如已选店铺坐标、上次搜索坐标），仅在无法推断时才使用默认值
  - 完成标志：坐标回退事件在 gunicorn 日志中有清晰可追踪的 `[coord]` 标签警告
  - _Requirements: 6.1, 6.3, 6.4_
  - _Boundary: _search_poi_

### 7. JSON 解析健壮性

- [ ] 7.1 LLM 调用点启用结构化输出并加固错误处理
  - 在行程审查、偏好检测、日程编辑应用等非对话编辑类的 LLM 调用点启用 JSON mode（通过 `response_format` 参数）——对话编辑类调用点（`api_edit_trip` / `api_multi_day_edit`）的 JSON mode 由任务 5.1/5.2 负责
  - 为每个启用 JSON mode 的调用点增加 `json.JSONDecodeError` 异常处理，解析失败时输出警告日志（含 `[json_parse]` 标签和原始响应前 100 字符摘要）
  - 统一解析失败时的降级行为：审查类 → 返回安全默认值（空审查结果）；偏好检测 → 返回无变更；日程编辑应用 → 返回 clarify 提示
  - 完成标志：行程审查、偏好检测、日程编辑应用等 LLM 调用点在 API 返回非标准 JSON 时不会崩溃，有明确日志和一致的降级行为
  - _Requirements: 5.2, 5.3, 5.4_
  - _Depends: 1.2_

### 8. 集成和验证

- [ ] 8.1 端到端集成验证：位置感知搜索工具链
  - 验证"故宫附近有咖啡馆吗"查询能触发 geocode("故宫") → search_nearby(故宫坐标, "咖啡") 完整调用链
  - 验证"东来顺饭店旁边有啥好玩的"查询返回东来顺饭店周边的真实商户，而非三里屯周边
  - 验证地理编码失败（地名不存在）时 LLM 向用户返回可理解的错误提示
  - 完成标志：3 个位置感知搜索用例全部通过，搜索结果中心点与用户提及的地名一致
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [ ] 8.2 端到端集成验证：多日行程查询与编辑确认
  - 验证多日行程中"6号上午有什么安排"查询返回正确的日时间线
  - 验证编辑确认流程：发送编辑指令 → 收到确认解读 → 确认 → 行程更新
  - 验证修正流程：发送编辑指令 → 收到确认解读 → 修正 → 系统重新理解
  - 完成标志：多日查询和编辑确认的完整用户路径无阻塞
  - _Requirements: 3.1, 3.2, 4.1, 4.2, 4.3, 4.4_

- [ ] 8.3 回归验证：现有功能不受影响
  - 验证单日行程搜索（"帮我安排下午理发+喝咖啡"）行为不变
  - 验证关键词快速路径（非 LLM 的 `_try_fast_category_match()` 品类匹配）行为不变——输入"火锅"仍直接命中品类缓存，不经过 LLM
  - 验证提醒、天气、偏好读写等现有工具仍可正常调用
  - 验证单日模式下 get_trip_status 返回结构与改动前兼容
  - 验证未启用 JSON mode 的 LLM 调用点行为不变
  - 完成标志：现有 13 个 CHAT_TOOLS handler 及关键词快速路径全部正常，无退化
  - _Requirements: 2.5, 5.1, 6.2_

- [ ]* 8.4 自动化测试覆盖（可选）
  - 为 geocode handler 编写单元测试：正常解析"故宫"→ 返回有效坐标；空地址 → 返回 ERROR
  - 为 search_nearby handler 编写单元测试：传入有效 lng/lat → 返回商户列表；缺少必填参数 → 返回 ERROR
  - 为 get_trip_status 多日模式编写测试：多日模式下返回含 `all_days_summary` 的数据
  - 为编辑确认流程编写测试：修改型操作返回 confirm 阶段 + interpretation；确认回调正确执行
  - 完成标志：4 个核心场景的自动化测试通过
  - _Requirements: 1.4, 1.5, 3.1, 4.1_
