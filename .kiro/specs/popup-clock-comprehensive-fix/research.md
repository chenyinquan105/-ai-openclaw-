# Research Log

## Discovery Scope
**Type**: Extension (brownfield) — modifying existing reminder popup and clock systems.
**Feature**: popup-clock-comprehensive-fix

## Key Findings

### 1. Countdown Formula Analysis
**Finding**: `_tickMedCountdown` 公式 `remainSec = 300 - elapsed * 60` 在速度=1虚拟分钟/秒时导致 5 秒过期。
**Source**: 代码审查 `21cb6f3:index.html` line ~2873
**Implication**: 保持公式不变（虚拟时间驱动逻辑正确），问题在于默认速度过快。修正速度语义使 1x = 1/60 虚拟分钟/秒。

### 2. is_running Missing from API Responses
**Finding**: `time_master._build_output` 不包含 `is_running` 字段，前端 `clockUpdateUI` 收到 `undefined`。
**Source**: 代码审查 `21cb6f3:skills/time_master/time_master.py` line ~91
**Implication**: 在 `_build_output` 添加 `is_running`，一处修改覆盖所有 clock API 响应。

### 3. Popup Mounting Target
**Finding**: `initReminderDialog` 挂载到 `document.body`，使用 `position:fixed` 覆盖全视口。
**Source**: 代码审查 `21cb6f3:index.html`
**Implication**: 改为挂载到 `#main-phone-container`，使用 `position:absolute`。

### 4. Postpone Icon Leak
**Finding**: `handle_user_action` 延后分支将新节点直接 append 到 `schedule_nodes`，无标记区分。
**Source**: 代码审查 `task_reminder_skill.py` line ~275
**Implication**: 添加 `_postponed` 标记，在 `/api/reminder/tasks` 过滤。

## Design Decisions
- **保持 1 秒轮询驱动倒计时**: 复用现有基础设施，避免引入独立 setInterval 导致的时钟同步问题
- **速度语义修正而非公式修正**: 倒计时公式本身正确（虚拟时间驱动），问题在速度定义
- **_postponed 标记方案**: 最小侵入，不改变 schedule_nodes 结构

## Risks
- 速度语义变更影响现有演示节奏：1x 从 "1 虚拟分钟/秒" 变更为 "1 虚拟分钟/60 秒"
- `_postponed` 标记依赖所有读取 schedule_nodes 的代码正确过滤
