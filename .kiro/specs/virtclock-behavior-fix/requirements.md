# Requirements Document

## Introduction
修正虚拟时间控制台（Virtual Time Console）的四个行为缺陷。核心原则：**虚拟时钟开关只改变项目走时的时钟源（真实时间 vs 虚拟时间），不改变任何已有计划和设置。出行计划是用户设定好的，虚拟时间变化不应自动重算计划时间线。提醒系统在虚拟时钟开启时完全由虚拟时间驱动，关闭时完全由真实时间驱动，两者互斥。**

## Boundary Context（边界上下文）
- **In scope**: 虚拟时钟开关操作对出行计划的影响范围、滑块拖动的 UI 稳定性、暂停/关机状态分离、真实时间与虚拟时间提醒的互斥关系
- **Out of scope**: 出行计划的排程算法本身、提醒管线（task_reminder_skill）的催促判定规则、time_master 的自动走时核心循环、SSE 推送通道架构
- **Adjacent expectations**: 虚拟时钟开关时必须正确维护 `clock_enabled` 标志位；真实时间提醒轮询线程需要能读取 `clock_enabled` 状态

## Requirements

### Requirement 1: 虚拟时钟操作不影响出行计划
**Objective:** As a 沙盒演示操作者，I want 虚拟时钟的开关/滑块跳转/快进操作只改变虚拟时间读数而不改动已设定的出行计划时间线，so that 出行计划保持用户原始设定，虚拟时间只是时间源而非计划修改器。

#### Acceptance Criteria
1. When 用户开启虚拟时钟控制台，the 虚拟时钟系统 shall 将当前虚拟时间初始化为 12:00，但不触发后端出行计划重排（不调用 /api/replan）
2. When 用户拖动时间滑块跳转到目标时间，the 虚拟时钟系统 shall 将虚拟时间更新为目标时间，但不触发后端出行计划重排
3. When 用户点击快进按钮（+10/+20/+30 分钟），the 虚拟时钟系统 shall 将虚拟时间向前推进对应分钟数，但不触发后端出行计划重排
4. The 虚拟时钟系统 shall 在关机时停止自动走时并恢复真实时间显示，但不修改出行计划

### Requirement 2: 滑块拖动时显示不被轮询覆盖
**Objective:** As a 沙盒演示操作者，I want 拖动虚拟时间滑块时显示的时间始终跟随我的手指位置，so that 我能精确选择目标时间而不会被服务端状态回推覆盖。

#### Acceptance Criteria
1. While 用户正在拖动时间滑块（oninput 事件期间），the 前端 UI shall 锁定滑块位置不被 1 秒轮询的 clockUpdateUI 覆盖
2. When 用户松开滑块完成跳转（onchange 事件），the 前端 UI shall 在服务端确认后同步最终时间并解除锁定
3. If 服务端跳转响应返回的时间与用户设定目标不一致，then the 前端 UI shall 以服务端确认的时间为准更新显示

### Requirement 3: 暂停走时与关机状态分离
**Objective:** As a 沙盒演示操作者，I want 点击暂停按钮（⏸）只停止自动走时而虚拟时钟仍处于开机状态，so that 暂停期间真实时间提醒轮询不会错误激活，提醒仍由虚拟时间驱动。

#### Acceptance Criteria
1. When 用户点击暂停按钮（⏸），the 虚拟时钟系统 shall 停止自动走时定时器，但保持 clock_enabled 状态为 true
2. When 用户点击关机按钮（关闭虚拟时钟主开关），the 虚拟时钟系统 shall 停止自动走时定时器，并将 clock_enabled 状态设为 false
3. While 虚拟时钟处于暂停状态（clock_enabled=true, is_running=false），the 前端 UI shall 显示播放按钮（▶）表示可继续走时
4. While 虚拟时钟处于暂停状态，the 提醒系统 shall 继续由虚拟时间驱动，真实时间轮询保持静默

### Requirement 4: 真实时间与虚拟时间提醒互斥
**Objective:** As a 沙盒演示操作者，I want 提醒系统在虚拟时钟开启时完全由虚拟时间驱动、关闭时完全由真实时间驱动，so that 不会出现同一提醒被两个时间源重复触发的情况。

#### Acceptance Criteria
1. While 虚拟时钟处于开启状态（clock_enabled=true），the 提醒系统 shall 仅通过虚拟时间推进触发提醒（经由 time_master._slice_triggered 扫描），真实时间后台轮询线程不执行提醒检查
2. While 虚拟时钟处于关闭状态（clock_enabled=false），the 提醒系统 shall 仅通过系统真实时间轮询触发提醒（经由 _realtime_reminder_poller 每 30 秒检查）
3. When 用户从关闭状态开启虚拟时钟，the 提醒系统 shall 立即从真实时间驱动切换到虚拟时间驱动
4. When 用户从开启状态关闭虚拟时钟，the 提醒系统 shall 立即从虚拟时间驱动切换到真实时间驱动
