# 行程编辑交互流程修复 — 设计文档

## 架构概览

```
用户输入 → sendTripMessage() → /api/edit_trip
  → L1 正则 (_fallback_parse_edit) → 命中 → 执行 action
  → 未命中 → L2 LLM 解析 → 合法 action → 执行
  → 未命中/非法 → L3 clarify 兜底

action 执行:
  swap_current → _resolve_swap_target → 搜候选 → swap_selection
  replace_stop → [NEW] remove + search → need_shop_selection
  add_stop → search → need_shop_selection / 直接添加
  ...
```

## 改动设计

### D1: 换店面板独立选择逻辑（index.html）

在 `_showTripSwapPanel` 中不再复用 `toggleShopCard`，新增 `_toggleSwapCard` 函数：
- 互斥单选：同一面板内只允许一张卡片处于选中态
- 样式使用 `border-orange-400` + `meituan-shadow`
- 卡片添加 `data-swap-category` 属性用于互斥查找
- `_confirmTripSwap` 查找 `.shop-card.border-orange-400`（与 `_toggleSwapCard` 样式一致）

### D2: replace_stop action（server.py）

**L1 正则**（`_fallback_parse_edit`）：
- 新增模式匹配：`把X改成Y`、`X换成Y`、`不要X了改成Y`
- 提取 remove_name 和 add_keywords/category

**LLM System Prompt**：
- 新增 action #10: `replace_stop`
- params: `{remove_name: string, add_keywords: string, add_category: string}`
- few-shot 示例

**REST Handler**：
- 新增 `replace_stop` 分支（在 `swap_current` 之后、`add_stop` 之前）
- 先调用 remove 逻辑（从 selected_pairs 中移除 remove_name 匹配的 stop）
- 再搜索 add_keywords/add_category → need_shop_selection

**ALLOWED_ACTIONS**：
- 新增 `"replace_stop"`

### D3: swap_shop 品类匹配（server.py L2319-2324）

变更前：
```python
if cat == new_category or (not updated):
```
变更后：
```python
if cat == new_category:
```
匹配不到时返回 400 错误。

### D4: 选店后更新对话历史（index.html）

`_confirmTripShopAdd` 成功回调中追加：
```javascript
tripChatHistory.push({role: 'ai', text: '已添加 ' + shopName + ' 到行程 ✅'});
if (tripChatHistory.length > 6) tripChatHistory.shift();
```

### D5: 单 stop 代词换店（server.py L1333-1350）

在 `clarify` 分支的 `_looks_like_swap_intent` 检查中，当 `len(pairs) == 1` 时：
```python
if len(pairs) == 1:
    cat, sid, sname = pairs[0]
    # 直接搜索同品类候选 → swap_selection
    # (复用 swap_current 的搜索逻辑)
```

### D6: CHAT_TOOLS swap_target_shop_id（server.py ~L4401）

在 CHAT_TOOLS edit_trip handler 中，从 `func_args` 读取 `swap_target_shop_id`：
```python
swap_target_shop_id = (func_args.get("swap_target_shop_id") or "").strip()
if swap_target_shop_id and action == "swap_current":
    params["target_shop_id"] = swap_target_shop_id
```

## 未改动部分

- `_resolve_swap_target` 逻辑不变（已经正确）
- `_shop_name_matches` 不变
- `_run_schedule_from_session` 不变
- `sendTripMessage` 整体流程不变
