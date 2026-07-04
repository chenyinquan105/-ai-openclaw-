# 行程编辑交互流程修复 — 任务分解

## T1: 修复 swap_shop API 品类匹配（server.py L2319）
- 去掉 `or (not updated)` 条件
- 品类不匹配时返回 400 错误
- **估时**: 5min

## T2: 新增 replace_stop action（server.py）
- L1 `_fallback_parse_edit`：识别"改成"模式，提取 remove_name + add_keywords
- LLM system prompt：新增 action #10 replace_stop + few-shot
- `ALLOWED_ACTIONS`：新增 `"replace_stop"`
- REST handler：新增 `replace_stop` 分支（先 remove 再 search → need_shop_selection）
- **估时**: 25min

## T3: 单 stop 代词换店直接 swap（server.py L1333-1350）
- `_looks_like_swap_intent` + `len(pairs) == 1` → 直接搜索同品类 → swap_selection
- **估时**: 10min

## T4: CHAT_TOOLS 注入 swap_target_shop_id（server.py ~L4401）
- 从 func_args 读取 swap_target_shop_id
- 注入到 params
- **估时**: 5min

## T5: 换店面板独立选择逻辑（index.html）
- 新增 `_toggleSwapCard` 函数（互斥单选，橙色系样式）
- `_showTripSwapPanel`：卡片加 `data-swap-category`，绑定 `_toggleSwapCard`
- `_confirmTripSwap`：确保查找 `.shop-card.border-orange-400`
- **估时**: 20min

## T6: 选店后更新对话历史（index.html）
- `_confirmTripShopAdd` 成功后 push 到 `tripChatHistory`
- **估时**: 5min

## T7: 重启验证
- 重启 Flask server
- 验证 6 个场景
- **估时**: 10min
