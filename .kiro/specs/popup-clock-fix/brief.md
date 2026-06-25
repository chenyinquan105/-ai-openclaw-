# Brief: popup-clock-fix

## Problem
1. 弹窗在首次出现后仍然瞬间消失。上一轮添加的代际守卫（`_medCountdownGen`）解决了倒计时回调竞态，但未解决事件重复投递问题：`_pollAndHandleEvents`（直接 fetch）与 patcher 链（通过 `clockFetch` 拦截）双重调用 `handleReminderEvent`，导致 `renderReminderDialog` 被调用两次。第二次调用重置 `_medEscalationLevel=1` 并重新 `_openMedDialog`，在弹窗尚未完全显示时触发新的倒计时。
2. 虚拟时间控制台拖动滑块或快进后时钟停止自动走表。`/api/clock/jump` 端点未保持时钟运行状态，且前端 `clockSliderChange` 未在跳转后恢复自动走时。

## Approach
方案 A（最小修复）：
- 弹窗：在 `renderReminderDialog` 中添加防重入锁，确保同一 medId 的弹窗在 500ms 内不会被重复创建
- 时钟：`clock_jump` 后端保持运行状态；前端跳转后自动恢复走时

## Scope
- **In**: 弹窗防重复创建、时钟跳转后保持运行
- **Out**: 不改变后端催促管道、不改变 SSE 推送机制
