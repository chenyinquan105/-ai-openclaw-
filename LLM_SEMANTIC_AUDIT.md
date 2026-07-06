# LLM 语义理解架构审计报告

> 审计日期: 2026-07-06  
> 审计范围: 全部 11 个 LLM 调用点的提示词、工具链、输入输出解析  
> 核心结论: 系统是"关键词映射→工具"模式，不是"语义理解→结构化参数→多步工具调用"模式

---

## 一、系统概况

- **LLM 模型**: DeepSeek Chat (`deepseek-chat`)
- **API 地址**: `https://api.deepseek.com`
- **Agent 类**: `MeituanAgent` (main.py:97)
- **LLM 包装方法**: `_call_llm()` (main.py:105) + `chat_stream()` (main.py:651)
- **总调用点数**: 11 个（server.py 中 9 个 + main.py 中 2 个流式）
- **Skills 目录**: 全部为纯算法，无 LLM 调用

---

## 二、全部 11 个 LLM 调用点清单

| # | 文件:行号 | 函数/端点 | 有工具? | 输入 | 输出解析 | 问题等级 |
|---|----------|----------|---------|------|---------|---------|
| 1 | server.py:1105 | `_search_poi()` 慢路径 | 1个(search_poi) | 用户原始文本 | regex JSON | A/D/E |
| 2 | server.py:5449 | `chat_stream()` L1 | 15个(CHAT_TOOLS) | 用户消息+历史 | 流式 tool_call | A/B/C/F |
| 3 | server.py:5510 | `chat_stream()` L2 | 15个 | 同上+tool结果 | 流式 tool_call | 同#2 |
| 4 | server.py:5543 | `chat_stream()` L3 | 15个 | 同上 | 流式 tool_call | 同#2 |
| 5 | server.py:832 | `_llm_review_schedule()` | 无 | 行程+天气 | regex JSON | C/D |
| 6 | server.py:6176 | `schedule_review_answer()` | 无 | 用户答案+快照 | regex JSON | D |
| 7 | server.py:1883 | `api_edit_trip()` | 无 | 用户编辑文本 | regex JSON | A/D/E |
| 8 | server.py:6428 | `api_multi_day_edit()` | 无 | 编辑文本+行程 | regex JSON | A/D/E |
| 9 | server.py:3338 | Plan B 异常决策 | 无 | 异常结构化数据 | 字符串匹配 | 轻微 |
| 10 | server.py:4264 | `memory_detect()` | 无 | 用户消息+偏好 | json.loads | A/D |
| 11 | server.py:4086 | `parse_note()` | 无 | 原始文本 | 直接取 content | 轻微 |

**问题等级说明**:
- **A** = 无结构化提取：原始文本直传 LLM，无预处理
- **B** = 缺少工具：LLM 需要的工具不在工具箱中
- **C** = 上下文不足：缺少天气/行程/偏好等关键上下文
- **D** = 脆弱解析：用 regex 提取 JSON 而非 structured output
- **E** = 单步强制：意图分类+参数提取强制在一次调用中完成
- **F** = 未教分解：提示词用关键词规则而非教 LLM 分解复杂查询

---

## 三、6 类架构缺陷详解

### 缺陷 1（P0·高危）：CHAT_TOOLS 缺少 geocode + nearby search 工具

**影响**: 所有"XX附近/旁边/周边有什么好玩/好吃的"查询都搜的是硬编码的三里屯(39.93,116.45)，而不是用户说的位置。

**现状**:
- `AmapPOIClient.geocode()` — 高德地名→坐标 API，代码存在于 `skills/amap_poi/amap_poi.py:316`
- `AmapPOIClient.search_nearby()` — 高德周边搜索 API，代码存在于 `skills/amap_poi/amap_poi.py:228`
- Flask 端点 `/api/poi/geocode` — server.py:4525，**已实现**
- Flask 端点 `/api/poi/nearby` — server.py:4489，**已实现**
- meituan-bridge 插件有对应工具 `meituan_poi_geocode` + `meituan_poi_nearby`
- **但 CHAT_TOOLS（15个工具）中没有这两个工具！Chat LLM 无法调用它们！**

**调用链对比**:

用户说"东来顺饭店旁边有啥好玩的"：

```
当前实际路径（错误）:
  chat_stream → search_poi(keywords="玩") 
  → _search_poi(原始文本) 
  → 坐标=39.93,116.45（三里屯硬编码）
  → 搜三里屯周边 ← 完全错误！

应有路径（正确）:
  chat_stream → geocode(address="东来顺饭店") → {lng:116.xxx, lat:39.xxx}
  → search_nearby(lng=116.xxx, lat=39.xxx, keywords="景点|娱乐")
  → 搜东来顺饭店周边 ← 正确！
```

### 缺陷 2（P0·高危）：系统提示词用关键词规则而非教分解思维

**影响**: LLM 收到复杂查询时不会拆解成子任务，只做单步关键词匹配。

**现状** (`chat_stream()` 系统提示词，server.py:5356-5405):

```
当前规则（关键词匹配式）:
  "句末有「吗」「？」 → 信息查询 → search_poi"
  "包含「加到行程」 → 行程操作 → edit_trip"
  "「帮我安排」 → 新建行程 → start_trip"
```

这是用自然语言写的 if-else 规则引擎，LLM 遵循这些规则时：
- 遇到混合查询（时间+地点+活动）时无法分解
- "6号上午去干点啥呢" 匹配"呢"→ 信息查询 ✓，"东来顺饭店旁边"→ 没有对应规则 ✗
- 永远不会自动执行 geocode → nearby search 两步流程

**应有模式**: 教 LLM 先分解问题，再逐步调用工具：
1. 用户想了解什么？（时间？地点？活动类型？）
2. 需要哪些工具？按什么顺序？
3. 是否需要先查已有行程？

### 缺陷 3（P1·中危）：编辑类 LLM 调用全是单步强制

**影响**: LLM 必须在一次调用中同时完成意图分类 + 参数提取，错误率高。

**涉及调用点**:
- `api_edit_trip()` (server.py:1883): 一次调用输出 `{action, params}`，解析失败 → fallback `clarify`
- `api_multi_day_edit()` (server.py:6428): 同上模式
- `_search_poi()` 慢路径 (server.py:1105): 一次调用同时提取 categories + keywords + coords

**应有模式**: 两步确认——先理解用户意图（返回解读结果给用户确认），再执行操作。

### 缺陷 4（P1·中危）：JSON 解析全部用脆弱正则

**影响**: LLM 输出格式稍有偏差就解析失败，fallback 行为不一致。

**现状**: 6+ 个调用点使用同一种模式：
```python
m = re.search(r'\{[\s\S]*\}', content)  # 匹配最大 JSON 块
result = json.loads(m.group(0))
```
问题：
- 匹配到嵌套或错误 JSON 块时 `json.loads` 直接抛异常
- 有些地方有 fallback（edit_trip → clarify），有些没有（memory_detect → 直接报错）
- 没用 DeepSeek 的 JSON mode 或 structured output

### 缺陷 5（P1·中危）：坐标硬编码为三里屯，静默回退无提示

**影响**: 用户说"故宫附近"，系统搜三里屯，用户不知道搜错了位置。

**现状** (`_search_poi()` server.py:1127-1140):
```python
if not coord or not coord.strip():
    coord = "39.93,116.45"  # 静默回退
```
这个硬编码值在代码中出现 6+ 次。

### 缺陷 6（P1·中危）：Chat LLM 无法查询多日行程内容

**影响**: LLM 无法回答"6号上午有什么安排"、"第三天还有空吗"。

**现状** (`get_trip_status` handler server.py:5048):
只返回 `session_state.get("selected_pairs")`（单日行程），不返回 `session_state.get("days")`（多日行程）。

---

## 四、修复优先级排序

### P0（必须修，直接影响用户体验）

| # | 修复项 | 文件 | 改动 |
|---|--------|------|------|
| 1 | CHAT_TOOLS 新增 `geocode` 工具 | server.py:4552, server.py:4788 | 工具定义 + handler (调用 `_amap_client.geocode()`) |
| 2 | CHAT_TOOLS 新增 `search_nearby` 工具 | server.py:4552, server.py:4788 | 工具定义 + handler (调用 `_amap_client.search_nearby()`) |
| 3 | 重写 `chat_stream()` 系统提示词 | server.py:5356 | 替换关键词规则为"分解思维+多步工作流"教学 |
| 4 | `get_trip_status` 返回多日行程 | server.py:5048 | 返回 `days[]` 的每日时间线摘要 |

### P1（应该修，减少错误率）

| # | 修复项 | 文件 | 改动 |
|---|--------|------|------|
| 5 | `search_poi` handler 增加地名自动 geocode | server.py:4794 | 检测用户文本中的地名 → 自动 geocode → 传坐标 |
| 6 | `_search_poi()` 签名增加 `center_coord` 参数 | server.py:980 | 允许调用方传入坐标覆盖硬编码默认值 |
| 7 | 坐标回退时打印警告日志 | server.py:1127 | `print("[search_poi] 坐标无效，回退到默认三里屯", flush=True)` |
| 8 | `edit_trip` / `multi_day_edit` 增加确认步骤 | server.py:1883, server.py:6428 | 修改型操作先返回解读确认 |

### P2（改善体验）

| # | 修复项 | 文件 | 改动 |
|---|--------|------|------|
| 9 | 前端适配新工具结果展示 | index.html | `geocode` / `search_nearby` 结果卡片 |
| 10 | JSON 解析增加健壮性 | server.py 多处 | 增加 `json.loads` 的 try/except + 明确 fallback |

---

## 五、验证用例

| 用例 | 用户输入 | 期望行为 |
|------|---------|---------|
| 1 | "东来顺饭店旁边有啥好玩的" | geocode("东来顺饭店") → search_nearby(坐标, "景点娱乐") |
| 2 | "故宫附近有咖啡馆吗" | geocode("故宫") → search_nearby(坐标, "咖啡") |
| 3 | "6号上午有什么安排" | get_trip_status → 返回 day 6 时间线 |
| 4 | "帮我在三里屯附近找火锅店加到行程" | geocode → search_nearby → 展示结果 → 用户选店 → 加行程 |
| 5 | "颐和园周边有什么好吃的" | geocode("颐和园") → search_nearby(坐标, "餐饮") |
