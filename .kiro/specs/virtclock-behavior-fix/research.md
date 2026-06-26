# Research Log

## Discovery Scope
Bug fix on existing virtual time console module. No external research needed — all findings derived from codebase audit.

## Key Findings

### Finding 1: `_rescheduleWithVirtualTime()` 被错误调用
- **Source**: `index.html` lines 2545, 2639, 2773
- **Root cause**: 设计时假设虚拟时间变更需要重算出行计划，但用户明确虚拟时钟只改时间源不改计划
- **Decision**: 删除三处调用，保留函数定义供 PlanB 使用

### Finding 2: 滑块 oninput 期间未锁定
- **Source**: `index.html` lines 2586-2598 (preview), 2628 (lock only in change)
- **Root cause**: `_clockJumpLocked` 仅在 onchange 时设锁，oninput 时 1 秒轮询可覆盖显示值
- **Decision**: 在 `clockSliderPreview` 中提前设锁

### Finding 3: `/api/clock/stop` 暂停与关机不分
- **Source**: `server.py` line 2186
- **Root cause**: 暂停和关机共用同一端点未区分，导致暂停时错误设 `clock_enabled=false`
- **Decision**: 增加 `power_off` 参数区分

### Finding 4: 真实时间轮询不检查 `clock_enabled`
- **Source**: `server.py` lines 2322-2412
- **Root cause**: `_realtime_reminder_poller` 设计时注释写"不依赖虚拟时钟"，但未考虑互斥需求
- **Decision**: 循环体内加 `clock_enabled` 提前 continue

## Architecture Decisions
- 不重构 `_rescheduleWithVirtualTime`：保留函数以备 PlanB 执行覆盖层刷新使用
- 不创建新 API 端点：通过 `power_off` 参数扩展现有 `/api/clock/stop`，减少变更面
- 滑块锁最小化变更：仅在 `clockSliderPreview` 追加一行 `_clockJumpLocked = true`

## Risks
- 前端时钟 API 调用方可能遗漏 `power_off` 参数 → 默认值 `false` 向后兼容，旧调用等价于暂停
