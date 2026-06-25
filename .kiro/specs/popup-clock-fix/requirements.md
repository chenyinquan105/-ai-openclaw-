# Requirements

## Boundary Context
- **In scope**: 弹窗防重复创建、虚拟时钟跳转后走时状态保持
- **Out of scope**: 后端催促管道、SSE 推送、倒计时显示逻辑

## Requirements

### Requirement 1: 弹窗不重复创建
**Objective:** 防止同一提醒事件被双重投递导致弹窗瞬间消失。

#### Acceptance Criteria
1. When 同一 medId 的 MED 事件在 500ms 内被重复投递，提醒弹窗系统 shall 仅创建一次弹窗
2. When 弹窗已被用户关闭，同一 medId 的新事件，提醒弹窗系统 shall 正常创建新弹窗

### Requirement 2: 时钟跳转后保持运行
**Objective:** 拖动时间轴或快进后虚拟时钟继续自动走表。

#### Acceptance Criteria
1. When 用户在时钟运行状态下拖动时间轴滑块跳转，虚拟时钟 shall 在跳转完成后继续保持自动走时
2. When 用户在时钟运行状态下点击快进按钮，虚拟时钟 shall 在快进完成后继续保持自动走时
3. When 用户在时钟暂停状态下拖动时间轴，虚拟时钟 shall 保持暂停状态
