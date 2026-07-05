# 多日行程模块完善 — 改造方案

## Context

上一轮已完成：日历日期选择、天气预调研、算法排程（天气感知）、LLM审查管线、审查问答卡片。

当前 5 个待解决问题：
1. 多日行程模式下的底部聊天框是摆设 — 输入消息后走的是单日行程的 `sendTripMessage()` → `/api/edit_trip`，不理解多日上下文，且成功后调用 `_refreshTripView()` 会覆盖多日卡片
2. 每日计划不够细致 — 缺少早餐、起床时间、就寝时间，且起床/就寝时间没有根据景点开门时间智能调整
3. 横向每日计划下方的圆点指示器需要删除
4. 横向卡片应只显示关键活动摘要，点击某天展开全屏详情（不遮挡 header 和底部输入栏）
5. **新增**：自动检索所有目的地营业时间（开门+关门），无法证实则标注"未知"；用户计划时间处于闭店状态时警告并给替代方案

---

## 分任务实施

### 任务 0：打通营业时间数据链路（新增 — 最高优先级）

**背景：** Amap API 在 `extensions=all` 时返回 `deep_info.opentime`（如 `"10:00-22:00"` 或 `"周一至周五 09:00-18:00"`），但 `_normalize_poi()` 完全丢弃了该字段。整个代码库零使用。

**文件：** [amap_poi.py](skills/amap_poi/amap_poi.py) + [server.py](server.py) + [multi_day_scheduler.py](skills/multi_day_scheduler/multi_day_scheduler.py)

**0.1 `_normalize_poi()` 捕获 `opentime`（amap_poi.py ~360行）：**

在 `_normalize_poi()` 返回的 dict 中新增字段：
```python
# 读取 deep_info（营业时间等额外信息）
deep_info = poi.get("deep_info", {}) or {}
opentime = deep_info.get("opentime", "")  # 如 "10:00-22:00" 或 "周一至周五 09:00-18:00"
```
返回 dict 新增：`"opentime": opentime or "未知"`

**0.2 桥接函数 `search_poi_matrix()` 透传（amap_poi.py ~652行）：**

在 repack 逻辑中新增 `"opentime": shop.get("opentime", "未知")`

**0.3 后端搜索端点透传（server.py）：**

- `/api/search_attraction`（~5497行）：POI 格式化结果中新增 `opentime` 字段
- `/api/poi/detail`（~4111行）：已有 `get_poi_detail()` 调用，确认透传
- `_run_multi_day_schedule()`（~423行）：转换候选池格式时保留 `opentime`

**0.4 排程引擎接收 `opentime`（multi_day_scheduler.py）：**

- 所有 shop dict 现在携带 `opentime` 字段
- 新增 `_parse_opentime(opentime_str)` 工具函数：解析 `"10:00-22:00"` → `{open: 600, close: 1320}`（分钟数）
  - 复杂格式（如 `"周一至周五 09:00-18:00; 周六,周日 10:00-20:00"`）→ 根据出行日期匹配对应时段
  - 无法解析 → 返回 `None`（标注"未知"）
- 新增 `_check_open(hours, visit_time_minutes)` 函数：判断 `visit_time` 是否在营业时间内

**预估：** ~80 行

---

### 任务 1：多日聊天后端 — 新增 `/api/multi_day_edit` 端点

**文件：** [server.py](server.py)

**背景：** 当前单日聊天走 `/api/edit_trip`，操作 `session_state["selected_pairs"]`（单日 pair 列表）。多日模式需要自己的端点。

**1.1 新增 endpoint（约在 1555 行后）：**

```python
@app.route("/api/multi_day_edit", methods=["POST"])
def api_multi_day_edit():
```

- 请求体：`{text, day_index, schedule: {days: [...]}, context}`
  - `schedule` 是前端传来的完整 `multiDayScheduleResult`（避免后端存储多日状态）
  - `day_index` 是当前活跃的天索引
  - `context` 是聊天历史（用于指代消解）
- 构建 LLM prompt：发送 schedule 摘要（每天的关键节点，不是完整 timeline）+ 用户消息
- LLM 返回：`{phase, message, days: [...updated...], candidates}`
- 支持 phase：`done` | `clarify` | `need_shop_selection` | `swap_selection` | `no_change`
- LLM 调用失败时返回 `{phase: "no_change", message: "抱歉，暂时无法处理..."}`

**1.2 新增 `_fallback_parse_multi_day_edit()` 规则解析器：**

- 作为 L1 快速通道（LLM 前的规则匹配）
- 识别简单意图：删除某天某个活动、移动活动到另一天、添加POI

**预估：** ~130 行

---

### 任务 2：多日聊天前端 — 新增 `sendMultiDayMessage()`

**文件：** [index.html](index.html)

**2.1 修改 `sendTripMessage()`（2245行）添加多日分支：**

在函数开头插入：
```javascript
if (multiDayMode && multiDayScheduleResult) {
  return sendMultiDayMessage();
}
```

**2.2 新增 `sendMultiDayMessage()`（2334行后）：**

- 调用 `API_BASE + '/api/multi_day_edit'`
- 请求体携带 `day_index: multiActiveDayIndex`、`schedule: multiDayScheduleResult`、`context`
- 在 `#dynamic-content` 中追加聊天气泡（用户 + AI）
- 成功更新时：更新 `multiDayScheduleResult.days` → 调用 `renderMultiDayCards()`
- 处理所有 phase：clarify 显示追问气泡、need_shop_selection 显示店铺选择、swap_selection 显示替换面板
- 维护 `multiDayChatHistory`（独立于单日 `tripChatHistory`）

**2.3 在 `renderMultiDayCards()` 中添加聊天区域：**

在多日卡片容器下方添加 `<div id="multi-day-chat-area">`，用于存放聊天消息气泡。

**预估：** ~110 行

---

### 任务 3：排程引擎增强 — 早餐 + 起床 + 就寝 + 营业时间感知

**文件：** [multi_day_scheduler.py](skills/multi_day_scheduler/multi_day_scheduler.py)

**3.1 `_build_timeline()` 添加早餐窗口和插入逻辑：**

- 新增常量 `BREAKFAST_WINDOW = (7*60, 9*60)`（387行附近）
- 在活动循环之前（418行前）插入早餐逻辑：
  - 从 `meal_map` 中找早餐类店铺（`_is_breakfast()` 或 `category == "breakfast"`）
  - 若有早餐店 → 插入 `BREAKFAST` 节点（07:00-07:45）+ 15min 缓冲
  - 若无早餐店 → 插入占位 `BREAKFAST_NEEDED` 节点（标记 `"🥐 早餐（待搜索）"`），后续由后端自动搜索补全
- 早餐后 `current_minutes` 推进到至少 09:00

**3.2 智能起床时间（基于当天首个景点开门时间）：**

- 遍历当天所有 VISIT 节点的 `opentime`，取最早的开门时间
- 起床时间 = 最早开门时间 - 60 分钟（留出洗漱+早餐+交通）
  - 如故宫 08:30 开门 → 起床 07:30
  - 如商场 10:00 开门 → 起床 09:00
- 若所有景点 `opentime === "未知"` → 默认起床 07:30
- action: `"WAKE_UP"`，memo: `"⏰ 起床"`，携带计算依据（如 `"故宫8:30开门"`）

**3.3 智能就寝时间（基于当天最后活动结束时间）：**

- 就寝时间 = 最后活动结束时间 + 90 分钟（留出回酒店+洗漱）
- 约束：不早于 21:00，不晚于 23:30
- 若晚餐/最后景点 `opentime` 已知且结束较早 → 就寝可提前
- action: `"BEDTIME"`，memo: `"🌙 就寝"`

**3.4 闭店检测 + 替代方案（在活动循环中）：**

- 每个 VISIT 节点插入前，调用 `_check_open(parsed_hours, current_minutes)`：
  - 若 `current_minutes + duration` 超出关门时间 → 标记 ⚠️
  - 若 `current_minutes` 早于开门时间 → 标记 ⚠️ + 推迟到开门时间
  - 若完全在闭店时段内 → 标记 🚫 + 不插入该节点，收集到 `closed_conflicts` 列表
- `closed_conflicts` 返回给后端，由后端搜索附近同类型替代 POI
- 节点 memo 附加标记：
  - `"⚠️ 到达时已关门（22:00关门）"` — 时间冲突
  - `"⚠️ 尚未开门（10:00开门），已推迟"` — 过早到达
  - `"⏰ 注意：需在18:00前离开（18:00关门）"` — 提前提醒
- 营业时间未知（`opentime === "未知"` 或解析失败）→ 标注 `"🕐 营业时间未知"`

**3.5 `_build_timeline()` 返回值扩展：**

原来只返回 `timeline: list`，改为返回：
```python
{"timeline": [...], "closed_conflicts": [...], "unknown_hours_shops": [...]}
```

**3.6 传参链路更新：**

- `solve_multi_day()` 新增 `wake_time_str="07:30"`（作为兜底默认值）、`bedtime_str="22:00"`（兜底）
- 透传至 `_build_timeline()`
- `solve()` 桥接函数同样更新
- `_route_one_day()` 返回结构更新以携带 conflicts

**预估：** ~110 行

---

### 任务 4：自动搜索餐厅 + 处理闭店冲突

**文件：** [server.py](server.py)

**4.1 新增 `_auto_search_restaurants_for_day()` 函数（~458行附近）：**

- 输入：day_centroid(lat,lng)、cuisine_prefs、rating_cutoff、destination
- 使用 `_amap_client.search_nearby()` 搜索周边 3km 内餐厅
- 关键词：cuisine_prefs 拼接（如 "川菜|粤菜|日料"）
- 按评分降序排列，取 Top 3
- 返回格式化后的餐厅 POI 列表（含 `opentime`）

**4.2 在 `_run_multi_day_schedule()` 中集成（451行后）：**

- 排程完成后，遍历每天检查缺少的餐次：
  - 缺少早餐 → `_auto_search_restaurants_for_day()` 搜索 >4.0 评分的早餐店 → 插入到当天 timeline
  - 缺少午餐/晚餐（meal_map 用尽）→ 搜索餐厅 → 插入
- 支持幂等：已有餐厅的天不再搜索

**4.3 处理闭店冲突（新增）：**

- 排程引擎返回 `closed_conflicts` 后，对每个冲突：
  1. 搜索附近同类别+高评分替代 POI（`_amap_client.search_nearby()`）
  2. 若无替代 → 生成警告：`"🚫 {POI名称} 在 {visit_time} 已关门（{opentime}），附近无替代"`
  3. 若有替代 → 自动替换或通过审查卡片让用户选择
- 在审查阶段，将闭店冲突作为 `risk_flags` 的一部分发送给 LLM

**4.4 在 `/api/smart_schedule` 响应中标记：**

- 新增字段：
  - `auto_meals_added: [{day_index, meal_type, count}]`
  - `closed_conflicts: [{day_index, shop_name, visit_time, opentime, alternatives}]`

**预估：** ~140 行

---

### 任务 5：删除分页圆点

**文件：** [index.html](index.html)

**5.1 从 `renderMultiDayCards()` 中删除（8063-8067行）：**

删除 5 行圆点渲染代码。

**5.2 清理 `switchMultiDay()` 中的圆点更新逻辑（8075-8078行）：**

删除 `.multi-day-dot` 相关的 DOM 操作，保留卡片边框颜色更新和滚动逻辑。

**预估：** 删除 ~10 行

---

### 任务 6：紧凑横向视图 + 点击展开全天详情

**文件：** [index.html](index.html)

**6.1 修改 `renderMultiDayCards()` 卡片渲染（8048-8060行）：**

- 每个卡片只显示**关键节点**（过滤规则）：
  - `action === "WAKE_UP"` → ⏰ + 时间
  - `action === "BEDTIME"` → 🌙 + 时间
  - `action === "BREAKFAST"` / `"LUNCH"` / `"DINNER"` → 🍽️ + 名称（截断12字）
  - `action === "VISIT"` → 📍 + 名称（截断15字）
  - 跳过 `REST`、缓冲、交通节点
- 每个节点一行：`{时间} {图标} {名称}`
- 节点间间距从 `space-y-2` 缩小为 `space-y-1`
- 卡片添加 `onclick="expandDayDetail(N)"` 和 `cursor-pointer hover:shadow-md`

**6.2 新增 `expandDayDetail(dayIndex)` 函数（8071行后）：**

全屏详情视图，填满 `#dynamic-content`（header 和 bottom bar 之间的 flex-1 区域）：

- **顶部固定栏**（flex-shrink-0）：
  - ← 返回按钮（调用 `closeDayDetail()`）
  - 日期标签（如 "7/4 周六 · 第1天"）
  - 统计条：总时长、交通时间、站点数
- **中间可滚动时间线**（flex-1 overflow-y-auto）：
  - 完整 timeline 所有节点，每个节点一个卡片
  - 不同类型不同背景色：WAKE_UP 蓝、BEDTIME 靛、餐饮 橙、REST 灰、VISIT 白
  - 每张卡片：时间 | 图标 | 名称 | 时长
  - **VISIT 节点额外显示**：
    - 🕐 营业时间（如 `"10:00-22:00"` 或 `"未知"`）
    - ⚠️ 闭店警告（若 `closed_conflict` 匹配到该节点，显示红色警告条）
- **底部导航**（flex-shrink-0）：
  - ← 前一天 | 后一天 → 按钮（边界时禁用）
- 布局利用 flexbox：`#day-detail-view` = `h-full flex flex-col`，header/bottom 用 `flex-shrink-0`，timeline 用 `flex-1 overflow-y-auto`

**6.3 新增 `closeDayDetail()` 函数：**

- 调用 `renderMultiDayCards(multiDayScheduleResult)` 恢复到卡片视图

**预估：** ~110 行

---

### 任务 7：提醒系统集成 — 起床/就寝闹钟

**文件：** [server.py](server.py)

**7.1 新增 `_inject_multi_day_reminders()` 函数（~458行附近）：**

- 从排程结果中提取所有 `WAKE_UP` 和 `BEDTIME` 节点
- 注册为 CUSTOM 类型提醒节点到虚拟时钟
- 配置：
  - 起床：`alarm_type: "persistent_ring"`（持续响铃直到用户操作）
  - 就寝：`alarm_type: "persistent_ring"`
  - 支持按天重复（multi-day 场景）
- 保护现有 WATER/MED 节点不被覆盖

**7.2 在 `/api/smart_schedule` 中调用（5701行后）：**

- 排程完成后调用 `_inject_multi_day_reminders(result, start_date, trip_days)`
- 在响应中包含 `reminders_injected` 字段

**预估：** ~65 行

---

## 实施顺序（依赖关系）

```
任务0 (营业时间数据链路) ──→ 任务3 (排程使用营业时间+闭店检测)
                                   ↓
任务5 (删除圆点, 独立)      任务4 (自动搜索餐厅+处理闭店冲突)
                                   ↓
                              任务7 (提醒集成)
                                   ↓
                              任务6 (展开视图, 展示营业时间)
                                   ↓
任务1 (后端chat) ──────────→ 任务2 (前端chat)
```

推荐执行顺序：0 → 3 → 5 → 4 → 7 → 6 → 1 → 2

---

## 涉及文件

| 文件 | 改动类型 | 改动量 |
|------|---------|--------|
| [amap_poi.py](skills/amap_poi/amap_poi.py) | 增强 `_normalize_poi()` + 桥接函数 | ~20 行 |
| [multi_day_scheduler.py](skills/multi_day_scheduler/multi_day_scheduler.py) | 增强 `_build_timeline()` + 新增解析函数 | ~110 行 |
| [index.html](index.html) | 新增+修改+删除 | ~230 行 |
| [server.py](server.py) | 新增 endpoint + 函数 + 透传 opentime | ~360 行 |

总计：~720 行，4 个文件。

---

## 验证方式

1. 多日排程生成后 → 验证每天 timeline 包含 ⏰起床 + 🥐早餐 + 🍽️午餐 + 🍽️晚餐 + 🌙就寝
2. 景点有营业时间数据 → 验证起床时间按最早开门时间调整（如故宫8:30开 → 7:30起床；商场10:00开 → 9:00起床）
3. 景点营业时间未知 → 验证标注 `🕐 营业时间未知`
4. 计划时间处于闭店状态 → 验证 ⚠️ 警告 + 自动搜索替代POI + 在审查卡片中提示用户
5. 无餐厅的候选池 → 验证自动搜索补全了高评分餐厅（含营业时间）
6. 底部聊天框输入"把第二天的故宫换到第一天" → 验证 LLM 理解并更新多日卡片
7. 底部聊天框输入"第一天加一个咖啡厅" → 验证搜索推荐 + 用户选择
8. 点击某天卡片 → 验证展开全屏详情（含 🕐 营业时间），不遮挡 header 和输入栏
9. 详情页 ← 返回 → 验证恢复卡片视图
10. 横向卡片 → 验证圆点已删除
11. 虚拟时钟 → 验证起床/就寝提醒已注册

---

## 风险

- **LLM 多日上下文过大**：7天 × 每天15节点 = 105节点。缓解：发送摘要而非完整 timeline，只发关键节点
- **Amap API 限流**：自动搜索餐厅时串行调用，每次 ≥200ms 间隔
- **展开视图性能**：7天详情切换通过 JS 重绘，无性能问题（DOM 操作 <50ms）
