# 行程编辑交互流程修复 — 需求文档

## 背景

上一轮实现了 `swap_current` 目标解析 + `clarify_swap_target` 多目的地选择器。但通过实际追踪所有交互流程，发现 7 处逻辑链路不完整或存在 bug，导致用户体验断裂（"话说一半没下文"）。

## 需求列表

### R1: 换店面板必须只能单选

**当前问题**：换店候选面板（`_showTripSwapPanel`）的卡片使用 `toggleShopCard` 处理点击，但该函数用 `border-yellow-400` 做选中样式，而换店面板用 `border-orange-400`。且卡片缺少 `data-category` 属性导致无法互斥。确认按钮 `_confirmTripSwap` 查找 `.shop-card.border-orange-400`，但因为样式错配，永远取到第一张卡。

**期望行为**：
- 换店面板的卡片互斥单选：点一张，其他自动取消
- 确认按钮取到用户实际点击的那张卡片
- 选中样式与面板视觉一致（橙色系）

### R2: 支持跨品类替换

**当前问题**：用户说"把火锅改成理发"，系统只能执行单一 action（add_stop 或 swap_current），导致只加了理发但没去掉火锅。

**期望行为**：
- 新增 `replace_stop` action：`{action: "replace_stop", params: {remove_name, add_keywords, add_category}}`
- L1 正则识别"改成"模式
- LLM system prompt 新增 replace_stop 规则
- 后端 handler：先移除旧 stop，再搜索新品类 → need_shop_selection

### R3: swap_shop API 品类匹配修复

**当前问题**：`api_swap_shop()` 中 `cat == new_category or (not updated)` 条件导致第一轮迭代必定匹配第一个 pair，不管品类是否一致。

**期望行为**：只按品类匹配替换，`or (not updated)` 条件移除。品类不匹配时返回错误而非静默替换错误的 stop。

### R4: 选店后更新对话历史

**当前问题**：`_confirmTripShopAdd` 成功后只刷新视图，不更新 `tripChatHistory`。导致后续 LLM 调用缺少上下文（不知道刚加了哪家店）。

**期望行为**：选店确认后往 `tripChatHistory` push 一条 AI 消息，格式与 `sendTripMessage` 保持一致。

### R5: 单目的地代词换店直接走 swap

**当前问题**：行程只有 1 个 stop，用户说"这家不要"，LLM 解析失败后落入通用 `clarify`（"抱歉没理解"），而非直接弹换店面板。

**期望行为**：`len(pairs) == 1` 且 `_looks_like_swap_intent` 为 true 时，直接搜索同品类候选 → swap_selection，不追问。

### R6: 多目的地换店追问提供上下文

**当前问题**：`clarify_swap_target` 把所有目的地平等列出，不区分刚加的和原有的。

**期望行为**：追问消息包含上下文提示，帮助用户理解当前有哪些可选目的地。

### R7: CHAT_TOOLS 路径注入 swap_target_shop_id

**当前问题**：REST 端点会注入 `swap_target_shop_id`，但 CHAT_TOOLS handler 不会。

**期望行为**：CHAT_TOOLS 的 edit_trip handler 同样从 tool arguments 读取并注入 `swap_target_shop_id`。
