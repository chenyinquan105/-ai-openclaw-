# Brief: reminder-popup-fix

## Problem
沙盒仿真模式下，虚拟时间到达药品提醒时刻后，弹窗出现后瞬间自动消失（<1秒），用户来不及操作。同时弹窗覆盖全页面导致左右控制台无法使用，点击「延后30分钟提醒」会在任务列表生成可见的新图标。

## Current State
- `index.html` 在 `3d84c7b` 提交中加入了一套倒计时系统（`_startCountdown` / `_updateMedCountdown` / `_onCountdownExpired`），使用真实时间 `setInterval` 检查虚拟时间，当时钟跳跃时产生竞态导致弹窗立即关闭
- `941a10a` 旧版本无倒计时系统，弹窗正常停留等待用户点击
- 弹窗当前为 `position:fixed;inset:0`，覆盖整个视口
- 延后操作在后端 `time_master` 添加新排程节点，但 `GET /api/reminder/tasks` 未过滤，导致前端渲染为新图标

## Desired Outcome
1. 弹窗出现后保持显示，等待用户操作（点击「确认服药」或「延后提醒」），不自动消失
2. 弹窗仅覆盖手机屏幕区域（`#main-phone-container`），左右控制台在弹窗期间保持可操作
3. 点击「延后30分钟提醒」不创建可见的提醒图标，仅内部静默排程

## Approach
方案 B：参照 `941a10a` 旧版，完全移除 `3d84c7b` 引入的倒计时系统（`_startCountdown` / `_updateMedCountdown` / `_onCountdownExpired` 及相关全局变量）。弹窗行为简化为「出现 → 等待用户操作 → 关闭」。催促升级依赖后端 `process_reminder_pipeline` 的时间差判定机制。同时调整弹窗 CSS 定位和后端延后节点标记。

## Scope
- **In**:
  - 移除前端倒计时系统
  - 弹窗定位从 `fixed` 改为 `absolute`，挂载到手机容器
  - 延后节点加 `_postponed` 标记，API 过滤
  - 关闭弹窗时清理催促状态
- **Out**:
  - 不改变后端催促管道时间判定逻辑（15/30/45min 三级）
  - 不改变虚拟时钟系统
  - 不改变 SSE 推送机制

## Boundary Candidates
- 前端弹窗 UI 层：`index.html` 中的 reminder dialog 逻辑
- 后端延后标记：`task_reminder_skill.py` + `server.py` API 过滤

## Constraints
- 必须保持与现有虚拟时钟系统的兼容
- 必须保持 SSE 事件推送通道正常工作
- 不引入新的 JS 依赖
