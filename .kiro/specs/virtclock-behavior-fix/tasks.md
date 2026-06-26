# Implementation Plan

- [ ] 1. 删除虚拟时钟操作后的出行计划重排调用
  - 在 `clockTogglePower()` 开机分支中删除 `_rescheduleWithVirtualTime()` 调用
  - 在 `clockSliderChange()` 成功回调中删除 `_rescheduleWithVirtualTime()` 调用
  - 在 `clockFastForward()` 成功回调中删除 `_rescheduleWithVirtualTime()` 调用
  - 保留 `_rescheduleWithVirtualTime()` 函数定义（PlanB 执行覆盖层仍需要）
  - 验证：开机/拖滑块/快进后，出行计划时间线均不变
  - _Requirements: 1.1, 1.2, 1.3, 1.4_
  - _Boundary: 虚拟时钟控制台前端_

- [ ] 2. 修复滑块拖动过程中显示被轮询覆盖
  - 在 `clockSliderPreview()`（oninput 回调）中设 `_clockJumpLocked = true`，拖的过程中就锁住
  - 保留 `clockSliderChange()`（onchange 回调）中的 2.5 秒解锁定时器
  - 验证：拖动滑块过程中显示跟随手指不弹回，松手后服务端确认时间正确同步
  - _Requirements: 2.1, 2.2, 2.3_
  - _Boundary: 虚拟时钟控制台前端_

- [ ] 3. 分离暂停走时与关机状态
  - 修改 `/api/clock/stop` 接收 `power_off` 参数（默认 false）
  - `power_off=true` 时设 `clock_enabled=false`；`power_off=false`（暂停）时保持 `clock_enabled` 不变
  - 修改 `clockTogglePower()` 关机分支调用 `/api/clock/stop` 时传 `{power_off: true}`
  - 修改 `clockStop()` 函数调用 `/api/clock/stop` 时传 `{power_off: false}`
  - 验证：暂停后虚拟时钟面板仍可操作、提醒由虚拟时间驱动；关机后面板变灰、真实时间轮询激活
  - _Requirements: 3.1, 3.2, 3.3, 3.4_
  - _Boundary: 虚拟时钟 API, 虚拟时钟控制台前端_

- [ ] 4. 真实时间提醒轮询与虚拟时钟互斥
  - 在 `_realtime_reminder_poller()` 循环体开头增加 `clock_enabled` 判断
  - `clock_enabled=true` 时跳过本轮真实时间检查（sleep 30s 后 continue）
  - 验证：虚拟时钟开启时真实时间到达提醒点不弹窗；虚拟时钟关闭时真实时间到达提醒点正常弹窗
  - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - _Boundary: 提醒轮询守护线程_

- [ ] 5. 端到端行为验证
  - 设出行计划 → 开启虚拟时钟 → 确认计划时间线不变 → 拖滑块 → 确认不变 → 快进 → 确认不变
  - 开启虚拟时钟 → 拖动滑块 → 过程中显示跟随手指不被轮询覆盖
  - 开机 → 点播放 → 点暂停 → 确认面板仍可操作 → 关机 → 确认面板变灰
  - 虚拟时钟关闭时设当前时间+1分钟的提醒 → 1分钟后弹窗正常
  - 虚拟时钟开启时等真实时间到提醒点 → 不弹窗 → 拖滑块到提醒点 → 正常弹窗
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4_
  - _Depends: 1, 2, 3, 4_
