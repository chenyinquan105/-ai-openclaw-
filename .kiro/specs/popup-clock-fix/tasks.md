# Implementation Plan

- [ ] 1. 弹窗防重入
- [ ] 1.1 renderReminderDialog 添加防重入锁
  - 新增全局变量 `_lastMedDialogOpenTs = 0`
  - 函数开头：若同一 medId 且距上次打开 <500ms → 跳过
  - 打开弹窗后更新 `_lastMedDialogOpenTs = Date.now()`
  - 完成后：同一事件重复投递不会创建两个弹窗
  - _Requirements: 1
  - _Boundary: ReminderDialog

- [ ] 2. 时钟跳转保持运行
- [ ] 2.1 后端 clock_jump 保持运行状态
  - 跳转前记录 `was_running = cs.is_running`
  - `_process_clock_triggers` 后若 `was_running` 则 `tm.start_auto_tick`
  - 完成后：跳转 API 响应后时钟继续走时
  - _Requirements: 2
  - _Boundary: ClockAPI

- [ ] 2.2 前端跳转操作后恢复走时
  - `clockSliderChange`: jump 成功后若 `clockPowerOn && wasRunning` 则调 `clockFetch('POST', '/api/clock/start')`
  - `clockSkip`: 同理
  - 完成后：拖动滑块/快进后时钟自动继续
  - _Requirements: 2
  - _Boundary: ClockUI
  - _Depends: 2.1

- [ ] 3. 验证
- [ ] 3.1 语法 + 功能验证
  - Python 语法、index.html 完整性
  - 弹窗不重复消失、时钟跳转后继续走表
  - _Requirements: 1, 2
