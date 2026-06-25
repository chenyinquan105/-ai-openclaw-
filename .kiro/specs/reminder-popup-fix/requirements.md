# Requirements Document

## Introduction
修复沙盒仿真模式下的提醒弹窗三个问题：弹窗出现后瞬间消失、弹窗覆盖全页面导致控制台不可操作、延后操作在任务列表生成可见图标。参照旧版行为，弹窗应保持显示直到用户主动操作。

## Boundary Context
- **In scope**: 弹窗的显示/停留/关闭行为；弹窗在页面上的定位范围；延后操作对任务列表的可见性影响
- **Out of scope**: 后端催促管道的时间判定逻辑（15/30/45分钟三级）；虚拟时钟系统的运行机制；SSE 事件推送通道
- **Adjacent expectations**: 后端 `process_reminder_pipeline` 继续基于时间差生成催促升级事件（MED_URGE_LIGHT / MED_URGE_HEAVY / MED_ESCALATION_CRITICAL）；虚拟时钟正常推进，排程节点到达时正常触发事件

## Requirements

### Requirement 1: 弹窗停留行为
**Objective:** 作为沙盒操作者，我希望提醒弹窗在触发后保持显示，等待我做出操作决策，以便我有充足时间选择「确认服药」或「延后提醒」。

#### Acceptance Criteria
1. When 虚拟时间到达药品提醒时刻，提醒弹窗系统 shall 在手机屏幕区域内显示提醒弹窗
2. While 弹窗处于显示状态且用户未操作，提醒弹窗系统 shall 保持弹窗可见，不自动关闭
3. When 用户点击「确认服药」按钮，提醒弹窗系统 shall 关闭弹窗并记录服药完成
4. When 用户点击「延后提醒」按钮，提醒弹窗系统 shall 关闭当前弹窗

### Requirement 2: 弹窗显示范围
**Objective:** 作为沙盒操作者，我希望弹窗仅覆盖手机屏幕区域，保留左右控制台可操作，以便在弹窗显示期间仍能操作虚拟时钟和异常模拟功能。

#### Acceptance Criteria
1. When 提醒弹窗显示时，提醒弹窗系统 shall 将弹窗限定在手机屏幕容器区域内
2. While 弹窗处于显示状态，提醒弹窗系统 shall 仅对手机屏幕区域施加半透明遮罩
3. While 弹窗处于显示状态，提醒弹窗系统 shall 保持左侧虚拟时钟控制台和右侧异常模拟控制台可交互

### Requirement 3: 延后提醒的不可见排程
**Objective:** 作为沙盒操作者，我希望点击「延后提醒」后不在任务列表中看到新图标，以便任务列表保持简洁，仅展示原始排程节点。

#### Acceptance Criteria
1. When 用户点击「延后30分钟提醒」，提醒弹窗系统 shall 在内部记录延后触发时间，不在提醒任务列表中创建可见图标
2. When 延后的30虚拟分钟到达时，提醒弹窗系统 shall 再次弹出提醒弹窗
3. If 同一药品被多次延后，提醒弹窗系统 shall 仅保留最近一次延后排程
