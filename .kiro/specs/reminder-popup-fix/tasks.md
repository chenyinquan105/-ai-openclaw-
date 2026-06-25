# Implementation Plan

- [x] 1. 前端倒计时系统修复 — 代际守卫 + 简化催促升级流程
- [x] 1.1 添加代际计数器并修复 _startCountdown
  - 新增全局变量 `_medCountdownGen = 0`
  - `_startCountdown()` 开头 `_medCountdownGen++`，将当前 gen 传入 `_updateMedCountdown`
  - `_stopRinging()` 中追加 `_medCountdownGen++`（终止旧回调）
  - 完成后：多次快速打开关闭弹窗，旧回调不会误操作新弹窗
  - _Requirements: 1
  - _Boundary: CountdownManager

- [x] 1.2 修复 _updateMedCountdown 的竞态条件
  - 函数签名改为 `function _updateMedCountdown(gen)`，首行检查 `if (gen !== _medCountdownGen) return;`
  - setInterval 调用改为 `setInterval(function() { _updateMedCountdown(gen); }, 500)`
  - 虚拟时间跳跃时 remainSec<=0 正常触发过期——代际守卫防止副作用
  - 完成后：拖动时间轴快进 10 分钟，弹窗正常升级不闪烁崩溃
  - _Requirements: 1
  - _Boundary: CountdownManager
  - _Depends: 1.1

- [x] 1.3 简化 _onCountdownExpired 催促升级流程
  - 删除全局变量 `_medEscalationWaitStart`, `_medEscalationWaitActive`, `_medEscalationWaitTimer`
  - 删除 `_stopRinging()` 中的 `_medEscalationWaitActive = false` 和 `if (_medEscalationWaitTimer) { ... }` 两行
  - `_onCountdownExpired()`: 移除 setInterval 等待逻辑 → closeReminderDialog() 后立即 `_medEscalationLevel++; _openMedDialog(_medPendingInfo);`
  - 完成后：倒计时归零后弹窗立即关闭并弹出下一级催促（无额外等待）
  - _Requirements: 1
  - _Boundary: CountdownManager
  - _Depends: 1.2

- [x] 2. 弹窗定位调整到手机屏幕内
- [x] 2.1 修改弹窗 CSS 定位与挂载目标
  - `initReminderDialog()`: `position:fixed` → `position:absolute`, `z-index:99999` → `z-index:100`, 追加 `border-radius:40px;overflow:hidden`
  - 挂载目标从 `document.body` 改为 `document.getElementById('main-phone-container')`（保留 `document.body` fallback）
  - 验证前提：`#main-phone-container` 已有 `position: relative`
  - 完成后：弹窗仅覆盖手机屏幕区域，左右控制台可正常交互
  - _Requirements: 2
  - _Boundary: ReminderDialog

- [x] 2.2 增强 closeReminderDialog 状态清理
  - `closeReminderDialog()` 中追加 `_medPendingInfo = null`（`_medEscalationLevel` 由催促升级链管理，不在 close 时重置）
  - 完成后：关闭弹窗后挂起信息被清除
  - _Requirements: 1
  - _Boundary: ReminderDialog

- [x] 3. 延后节点不可见标记（跨边界集成任务）
- [x] 3.1 后端延后节点加隐藏标记并去重
  - `task_reminder_skill.py` 延后分支中：追加前过滤同 `med_id` 的旧 `_postponed` 节点
  - 新节点追加 `"_postponed": True`
  - 完成后：同一药品多次延后仅保留最新排程
  - _Requirements: 3
  - _Boundary: PostponeFlag

- [x] 3.2 API 过滤延后节点
  - `server.py` `/api/reminder/tasks` 遍历时跳过 `n.get("_postponed")` 为真的节点
  - 完成后：任务列表不显示延后图标，30 分钟后正常触发
  - _Requirements: 3
  - _Boundary: ReminderAPI
  - _Depends: 3.1

- [x] 4. 集成验证
- [x] 4.1 语法验证
  - Python: `python3 -c "import py_compile; ..."` 验证 server.py + task_reminder_skill.py → OK
  - JS: index.html 文件完整性验证 → 200240 bytes OK
  - 完成后：所有文件语法检查通过
  - _Requirements: 1, 2, 3
  - _Depends: 1.3, 2.2, 3.2

- [x] 4.2 端到端功能验证
  - 流程 1：药品提醒到达 → 弹窗+倒计时 → 5 分钟后自动升级 → 再次弹窗
  - 流程 2：弹窗期间拖动时间轴快进 → 倒计时跟随跳跃 → 正常升级不崩溃
  - 流程 3：弹窗期间左右控制台可操作
  - 流程 4：延后 → 任务列表无新图标 → 30 分钟后再次触发
  - 流程 5：同一药品连续延后两次 → 仅触发一次
  - 服务器启动正常，HTTP 200，API 响应正常
  - _Requirements: 1, 2, 3
  - _Depends: 4.1
