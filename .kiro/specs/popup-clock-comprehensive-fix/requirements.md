# Requirements Document

## Introduction
修复 `4d82a41` 版本的 5 个联动缺陷：弹窗倒计时过快消失、虚拟时钟操作后状态不同步、弹窗覆盖全视口、延后提醒产生可见图标、时钟速度定义前后端不一致。本规格基于根因分析生成，确保弹窗在虚拟时间驱动下正确停留 5 虚拟分钟后自动升级，同时保持用户在真实时间的操作窗口。

## Boundary Context（边界上下文）
- **In scope**: 弹窗倒计时显示与自动升级、虚拟时钟状态同步（is_running 字段）、弹窗定位在手机容器内、延后节点过滤、时钟速度枚举语义修正
- **Out of scope**: SSE 双通道架构重构、time_master 自动走时核心逻辑重写、提醒管线（task_reminder_skill）业务规则变更（延后标记除外）
- **Adjacent expectations**: 虚拟时钟的 start/stop/jump/offset/speed API 需在响应中包含 is_running 字段；任务列表 API 需过滤 _postponed 节点；弹窗 DOM 挂载目标从 body 改为手机容器

## Requirements

### Requirement 1: 弹窗倒计时与自动催促升级
**Objective:** As a 用户，I want 提醒弹窗在虚拟时间到达提醒时刻后显示倒计时并停留 5 虚拟分钟后自动升级催促，so that 即使我不操作，系统也能按正确的时间间隔推进催促流程。

#### Acceptance Criteria
1. When 虚拟时钟到达提醒时刻，the 提醒弹窗系统 shall 在手机屏幕内显示弹窗，并在右上角显示「⏱️ 5:00」倒计时徽章
2. While 弹窗显示中 and 倒计时未归零，the 提醒弹窗系统 shall 每秒更新倒计时数字，递减速度跟随虚拟时钟当前倍速（1x/60x/300x）
3. When 倒计时归零（5 虚拟分钟已过）and 催促等级 < 3，the 提醒弹窗系统 shall 关闭当前弹窗、提升催促等级并立即打开升级后的弹窗，开始新一轮 5 虚拟分钟倒计时
4. When 倒计时归零（5 虚拟分钟已过）and 催促等级 = 3，the 提醒弹窗系统 shall 触发紧急联络人通知界面，显示联络人姓名和电话
5. If 用户在倒计时期间点击弹窗按钮（确认服药/延后提醒），then the 提醒弹窗系统 shall 立即响应用户操作并停止当前倒计时
6. The 提醒弹窗系统 shall 在倒计时剩余 ≤ 60 虚拟秒时，将倒计时徽章变为红色警示样式

### Requirement 2: 虚拟时钟操作后状态保持
**Objective:** As a 用户，I want 在使用时间跳转、快进、倍速操作后时钟自动走时状态正确显示，so that 我能准确判断时钟是否在运行。

#### Acceptance Criteria
1. When 用户执行时间跳转（jump）操作成功，the 虚拟时钟系统 shall 在响应中包含 is_running 字段反映当前自动走时状态
2. When 用户执行快进（offset）操作成功，the 虚拟时钟系统 shall 在响应中包含 is_running 字段反映当前自动走时状态
3. When 用户执行倍速设置（speed）操作成功，the 虚拟时钟系统 shall 在响应中包含 is_running 字段反映当前自动走时状态
4. While 虚拟时钟 is_running 为 true，the 前端 UI 播放按钮 shall 显示 ⏸（暂停）图标
5. While 虚拟时钟 is_running 为 false，the 前端 UI 播放按钮 shall 显示 ▶（播放）图标
6. When 用户拖动时间滑块完成跳转后，the 虚拟时钟系统 shall 保持跳转前的自动走时状态不变

### Requirement 3: 弹窗仅覆盖手机屏幕区域
**Objective:** As a 用户，I want 提醒弹窗仅在手机屏幕区域内弹出并灰屏，so that 左右两侧的虚拟时间控制台和异常模拟控制台在弹窗期间仍可正常操作。

#### Acceptance Criteria
1. When 提醒弹窗触发，the 提醒弹窗系统 shall 将弹窗 DOM 挂载到手机屏幕容器（#main-phone-container）内
2. While 弹窗显示中，the 弹窗遮罩层（半透明灰色背景）shall 仅覆盖手机屏幕区域
3. While 弹窗显示中，the 左侧虚拟时间控制台 all 按钮和滑块 shall 保持可交互
4. While 弹窗显示中，the 右侧异常模拟控制台 all 按钮 shall 保持可交互

### Requirement 4: 延后提醒不创建可见任务图标
**Objective:** As a 用户，I want 点击「延后 30 分钟提醒」后不产生新的可见提醒图标，so that 提醒列表保持整洁，延后信息仅在内部排程中静默等待二次触发。

#### Acceptance Criteria
1. When 用户点击「延后 30 分钟提醒」，the 提醒系统 shall 在内部排程中添加 30 分钟后的触发节点并标记为 _postponed
2. When 任务列表 API 返回数据，the 提醒系统 shall 过滤掉标记为 _postponed 的节点
3. When 延后的 30 分钟到期，the 提醒系统 shall 正常弹出提醒弹窗
4. The 延后操作 shall 不改变提醒列表中已有可见任务的数量和内容

### Requirement 5: 虚拟时钟速度定义一致
**Objective:** As a 用户，I want 虚拟时钟的倍速标签与实际时间推进速度一致，so that 我能准确理解当前演示速度。

#### Acceptance Criteria
1. The 虚拟时钟系统 shall 在 1x 倍速下，每 60 真实秒推进 1 虚拟分钟（模拟真实时间流逝）
2. The 虚拟时钟系统 shall 在 60x 倍速下，每 1 真实秒推进 1 虚拟分钟（快进演示模式）
3. The 虚拟时钟系统 shall 在 300x 倍速下，每 1 真实秒推进 5 虚拟分钟（超快进模式）
4. When 用户切换倍速，the 前端 UI shall 高亮对应倍速按钮，清除其他按钮高亮
