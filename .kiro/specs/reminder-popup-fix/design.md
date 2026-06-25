# Design Document: reminder-popup-fix

## Overview
**Purpose**: 修复沙盒仿真模式下的提醒弹窗三个缺陷——弹窗瞬间消失、全页覆盖导致控制台不可操作、延后操作产生可见图标。采用方案 A：保留并修复倒计时系统，弹窗右上角显示跟随虚拟时间的倒计时，5 虚拟分钟后自动升级为下一级催促。
**Users**: 沙盒操作者（使用 `index.html` 控制台的用户）
**Impact**: 修复前端倒计时竞态 + 修复催促间隔计算 + 调整弹窗定位 + 后端延后节点标记

### Goals
- 弹窗右上角倒计时正确跟随虚拟时间递减（快进/拖动/倍速均实时反映）
- 倒计时 5 虚拟分钟后弹窗自动关闭并立即弹出下一级催促（不等额外间隔）
- 用户可在倒计时期间随时点击按钮操作
- 弹窗仅覆盖手机屏幕区域，保留左右控制台可交互
- 延后操作不创建可见的提醒任务图标

### Non-Goals
- 不改变后端三级催促时间判定（15/30/45min）
- 不改变虚拟时钟系统
- 不改变 SSE 推送机制
- 不改变倒计时显示样式

## Boundary Commitments

### This Spec Owns
- 前端弹窗的倒计时逻辑（`_startCountdown` / `_updateMedCountdown` / `_onCountdownExpired`）的竞态修复
- 倒计时过期后的催促升级流程（关闭 → 立即重开下一级）
- 弹窗在页面上的定位方式（从 `fixed` 改为 `absolute`，挂载到手机容器）
- 延后节点的可见性标记（`_postponed` flag）和去重
- 任务列表 API 对隐藏节点的过滤逻辑

### Out of Boundary
- 后端 `process_reminder_pipeline` 的时间差判定逻辑
- 虚拟时钟系统的运行机制（`time_master.py`）
- SSE 事件推送通道
- 催促升级的三级阈值配置
- 倒计时 UI 样式

### Allowed Dependencies
- `#main-phone-container` DOM 元素（需有 `position: relative`）
- `task_reminder_skill.py` 的 `handle_user_action` 函数
- `server.py` 的 `/api/reminder/tasks` 端点
- `_getCurrentVirtMinutes()` — 读取当前虚拟时间

### Revalidation Triggers
- 弹窗 DOM 结构变更
- `#main-phone-container` 的 CSS `position` 属性变更
- `schedule_nodes` 数据结构变更
- 倒计时时长（300 秒）变更

## Architecture

### Existing Architecture Analysis
当前倒计时系统有三层缺陷：
1. **竞态条件**：`setInterval` 使用真实时间间隔（500ms）检查虚拟时间；虚拟时间跳跃时，回调瞬间看到巨大 `elapsed` 值，触发 `_onCountdownExpired` 关闭弹窗
2. **催促间隔计算错误**：`_onCountdownExpired` 中 `elapsed * 60 >= 10` — `elapsed` 是整数虚拟分钟，`* 60` 后 1 分钟即 ≥10，实际只等了 1 虚拟分钟而非 10 虚拟秒
3. **无代际守卫**：旧回调无法识别新弹窗已打开，继续操作已关闭的弹窗

### Fix Strategy

**修复 1：代际计数器（Generation Counter）**
- 每次 `_startCountdown` 递增 `_medCountdownGen`
- `_updateMedCountdown` 和 `_onCountdownExpired` 的回调在闭包中捕获当前 gen
- 如果 `gen !== _medCountdownGen`，说明弹窗已被新弹窗替换，回调静默退出

**修复 2：简化催促升级流程**
- `_onCountdownExpired`：关闭弹窗 → 递增 level → **立即**调用 `_openMedDialog` 重开（不等额外间隔）
- 删除 `_medEscalationWaitStart` / `_medEscalationWaitActive` / `_medEscalationWaitTimer` 三个变量及其定时器逻辑
- 5 分钟等待已由倒计时本身提供，无需额外等待

**修复 3：虚拟时间跳跃处理**
- `_updateMedCountdown` 中：当 `elapsed` 导致 `remainSec <= 0` 时，正常触发过期（这是正确行为——倒计时应跟随虚拟时间）
- 代际守卫确保即使跳跃触发关闭，也不会影响新弹窗

### 修复后流程
```
SSE/轮询事件 → _openMedDialog()
  → _startCountdown() [gen++, setInterval 500ms]
    → _updateMedCountdown(gen) [检查 gen 匹配 + 虚拟时间]
      → 倒计时显示递减（跟随虚拟时间）
      → remainSec <= 0 → _onCountdownExpired()
        → closeReminderDialog()
        → level++ → _openMedDialog() [立即重开，新一轮 gen++]
  → 用户点击按钮 → closeReminderDialog() [gen++ 终止旧回调]
```

## File Structure Plan

### Modified Files
| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `index.html` | 修复倒计时竞态 + 简化催促升级 + 调整弹窗定位 | 修改 + 部分删除 |
| `skills/task_reminder_skill/task_reminder_skill.py` | 延后节点加 `_postponed` 标记 + 去重 | 修改 |
| `server.py` | `/api/reminder/tasks` 过滤 `_postponed` 节点 | 修改 1 行 |

### index.html 改动明细
```
新增:
- var _medCountdownGen = 0; (代际计数器)

删除:
- var _medEscalationWaitStart, _medEscalationWaitActive, _medEscalationWaitTimer
- _onCountdownExpired() 中的 setInterval 等待逻辑 (lines 2996-3011)
- _stopRinging() 中: _medEscalationWaitActive = false;
                   if (_medEscalationWaitTimer) { ... }

修改:
- _startCountdown(): 追加 _medCountdownGen++; 闭包捕获 gen
- _updateMedCountdown(): 函数签名改为接收 gen 参数；首行检查 gen !== _medCountdownGen → return
- _onCountdownExpired(): 移除等待定时器；closeReminderDialog() 后立即 level++ → _openMedDialog()
- _stopRinging(): 追加 _medCountdownGen++ 终止旧回调
- initReminderDialog(): position:fixed → position:absolute, z-index:99999 → z-index:100,
                      添加 border-radius:40px;overflow:hidden
- initReminderDialog(): document.body.appendChild → #main-phone-container.appendChild（带 fallback）
- closeReminderDialog(): 追加 _medEscalationLevel=0; _medPendingInfo=null

保留:
- _medCountdownTimer, _medCountdownActive, _medCountdownStartVirtMin (倒计时核心变量)
- _badgeWasRed (倒计时最后60秒红色脉冲)
- _startCountdown(), _updateMedCountdown() (修复后版本)
- _onCountdownExpired() (简化后版本，无等待定时器)
- _medEscalationLevel, _medContinuousRing, _medPendingInfo
```

### task_reminder_skill.py 改动明细
```
修改 (handle_user_action, user_input=="2" 分支):
- 添加 _postponed 节点前，先过滤掉同一 med_id 的旧 _postponed 节点
- 新节点追加 "_postponed": True
```

## Requirements Traceability

| Requirement | Summary | Components | Modified Files |
|-------------|---------|------------|----------------|
| 1: 弹窗停留行为 | 倒计时 5 分钟后正常升级，不瞬间消失 | 代际守卫 + 修复催促升级流程 | `index.html` |
| 2: 弹窗显示范围 | 弹窗仅覆盖手机屏幕 | `initReminderDialog()` 定位 + 挂载调整 | `index.html` |
| 3: 延后不可见排程 | 延后不在任务列表创建图标 + 去重 | `_postponed` 标记 + API 过滤 | `task_reminder_skill.py`, `server.py` |

## Components and Interfaces

### Frontend: Countdown System (index.html)

#### CountdownManager
| Field | Detail |
|-------|--------|
| Intent | 管理弹窗倒计时：递减显示 + 过期后触发催促升级 |
| Requirements | 1 |

**State Management**
- `_medCountdownGen`: 代际计数器，每次新倒计时启动 +1，回调检查匹配
- `_medCountdownTimer`: `setInterval` 句柄（500ms 真实间隔）
- `_medCountdownActive`: 倒计时是否激活
- `_medCountdownStartVirtMin`: 倒计时起始虚拟分钟数

**Implementation Notes**
- `_updateMedCountdown(gen)`: 闭包捕获的 gen vs `_medCountdownGen` — 不匹配则 return
- `_onCountdownExpired()`: 不再有独立的等待定时器；立即升级
- 虚拟时间跳跃导致 remainSec<=0 是正常行为 — 代际守卫防止副作用

### Frontend: ReminderDialog Positioning
| Field | Detail |
|-------|--------|
| Intent | 弹窗限定在手机屏幕容器内 |
| Requirements | 2 |

**Implementation Notes**
- CSS: `position:absolute;inset:0;z-index:100;border-radius:40px;overflow:hidden`
- 挂载: `#main-phone-container` (fallback: `document.body`)
- 前提: `#main-phone-container` 已有 `position: relative`

### Backend: Postpone Visibility
| Field | Detail |
|-------|--------|
| Intent | 延后节点不可见 + 多次延后去重 |
| Requirements | 3 |

**Implementation Notes**
- `task_reminder_skill.py`: 追加前过滤同 med_id 旧 `_postponed` 节点 → 追加带 `"_postponed": True` 的新节点
- `server.py`: `/api/reminder/tasks` 跳过 `_postponed` 节点

## Testing Strategy

### Unit Tests (手动验证)
- 倒计时从 5:00 开始递减，跟随虚拟时间（1x 下 ~5 分钟，60x 下 ~5 秒）
- 倒计时最后 60 秒变红脉冲
- 倒计时归零后弹窗自动关闭并立即弹出下一级催促
- 倒计时期间点击按钮正常工作

### Integration Tests
- 设置 13:00 药品提醒 → 到达 → 弹窗 + 倒计时 → 5 分钟后自动升级
- 拖动时间轴快进 10 分钟 → 倒计时瞬间归零 → 弹窗正常升级（不闪烁/不崩溃）
- 弹窗显示期间左右控制台可操作

### E2E Tests
- 完整流程：排程 → 弹窗 + 倒计时 → 延后 → 30 分钟后再次弹窗 → 确认服药
- JavaScript 语法检查通过
- Python 语法检查通过
