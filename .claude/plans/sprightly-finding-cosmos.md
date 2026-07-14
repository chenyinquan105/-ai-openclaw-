# 避雨饮品店：优化前往确认消息 + 顶部横向计划栏同步更新

## Context

当用户在行程中触发"下暴雨"异常并选择"就近找奶茶/咖啡店避雨"后，当前行为：
1. 聊天消息为"已为您更换行程，前往 X ☕ 避雨" — 没有明确告知"现在前往"的紧迫感
2. `renderSchedule(insRes)` 渲染到 `#dynamic-content`，然后 `_planbFlushExecCover()` 再调 `_refreshTripView()` 重建 trip view — 存在冗余渲染，且顶部横向栏的更新依赖这个链条

用户期望：
- 清晰告知"已将 X 添加至行程，现在前往避雨"
- 顶部 `#trip-timeline-bar` 横向计划栏同步更新，饮品店作为第一个前往节点显示

## 根因

1. `_doInsertShelter()` 中成功回调的消息文案不够明确
2. 成功回调中调用了 `renderSchedule(insRes)`（渲染到 schedule card 区域，在 trip mode 下可能冗余）+ `_planbFlushExecCover()`（再调 `_refreshTripView()`），两个调用冗余且可能引起渲染闪烁

后端 `/api/insert_shelter` 已正确将 cafe 插入 `selected_pairs[0]` 并重跑排程，返回的 timeline 中 cafe 已是第一个 MOVE_AND_EXEC 节点。

## 解决方案

### 前端改动（index.html）

**修改 `_doInsertShelter()` 函数（约第 4532-4547 行）**

改动点：
1. **更新成功消息文案**：从 `'已为您更换行程，前往 <b>' + name + '</b> ☕ 避雨。...'` 改为 `'已将 <b>' + name + '</b> 添加至行程，现在前往避雨 ☕。雨停后可通过聊天框说「雨停了继续行程」恢复计划。'`
2. **简化渲染调用**：用 `_refreshTripView()` 替代 `renderSchedule(insRes) + _planbFlushExecCover()`：
   - 先设置 `lastScheduleData = insRes`
   - 再调用 `_refreshTripView()` 统一刷新（内部调用 `renderTripView` + `renderMetroTimelineForMainFace` + `buildWaypointCoords`）
   - 若 `lastScheduleData` 已有 `pitfall_insights`/`anomaly_triggers` 等 Plan B 字段，一并保留

### 不改动

- `server.py` — `/api/insert_shelter` 已正确将饮品店插入 selected_pairs[0]
- `renderMetroTimelineForMainFace()` — 逻辑无需改动，从 timeline 数据渲染顶部栏即可
- 多日行程 — 避雨功能目前仅单日行程使用（异常模拟控制台触发），多日行程的 timeline 栏同步由 `_syncMultiDayTimelineToClock` 独立管理

## 数据流

```
用户选择 "去 X 避雨"
  → _doInsertShelter(shopId, shopName)
    → POST /api/insert_shelter { shop_id }
      → selected_pairs.insert(0, ("cafe", shopId, name))
      → _run_schedule_from_session() 重跑排程
      → 返回新 timeline（cafe 为第一个 MOVE_AND_EXEC）
    → lastScheduleData = insRes
    → _refreshTripView()
      → renderTripView(lastScheduleData)     ← 主内容区行程视图
      → renderMetroTimelineForMainFace()     ← 顶部横向计划栏（饮品店在第一位置）
      → buildWaypointCoords()               ← 地图标记
    → 显示消息："已将 X 添加至行程，现在前往避雨 ☕"
```

## 验证

1. 启动 server，打开浏览器
2. 正常完成一次单日行程排程 → 点击"执行计划"进入 trip mode
3. 点击左侧"🌧️ 下暴雨"异常按钮 → 选择"☕ 就近找奶茶/咖啡店避雨"
4. ✅ 弹出二级弹窗，显示最近饮品店（名称 + 距离）
5. 点击"去 X 避雨"
6. ✅ 聊天消息显示："已将 X 添加至行程，现在前往避雨 ☕"
7. ✅ 顶部 `#trip-timeline-bar` 横向栏更新，饮品店出现在 DEPART 之后的第一个位置
8. ✅ 主行程视图也同步更新，显示饮品店为第一站
