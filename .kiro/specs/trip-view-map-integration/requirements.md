# Requirements Document

## Introduction

本规格定义美团 AI 智能助手（Meituan AI Assistant）的行程视图重构与真实地图集成功能。当前用户点击"执行计划"后跳转至全屏遮罩层（`#exec-cover`），体验断裂且无地图可视化。本次改造将行程内容融入主界面，新增高德 JS API 真实地图，通过虚拟时钟驱动用户位置的时间动画。

## Boundary Context (Optional)
- **In scope**: 取消 exec-cover 跳转层、行程内容（防坑指南/出门提醒/时间轴）在主界面渲染、高德 JS API 2.0 地图面板（路线折线+途经点标记+时间驱动位置标记）、底部搜索栏双模式切换（搜索/行程聊天）、结束行程清理
- **Out of scope**: 贪心路径规划算法修改、虚拟时钟核心逻辑修改、POI 搜索 API 替换、天气 API 替换、后端排程引擎改动（仅新增 1 行坐标透传字段）
- **Adjacent expectations**: 路线规划模块 `route_planner` 继续输出含坐标的途经点数据；虚拟时钟模块 `time_master` 继续提供 `/api/clock/status` 接口；高德 JS API Key 已注册且有效

## Requirements

### Requirement 1: 取消 exec-cover 跳转，行程内容融入主界面
**Objective:** 作为用户，我希望点击"执行计划"后不再跳转到独立遮罩页，而是直接在当前位置看到行程内容，保持界面连贯性

#### Acceptance Criteria
1. When 用户点击"执行计划"按钮, the 系统 shall 移除店铺推荐卡片并在 `#dynamic-content` 中渲染行程视图
2. When 行程视图渲染完成, the 系统 shall 隐藏店铺推荐面板（包括店铺选择卡片和时间输入区）
3. When 行程视图渲染完成, the 系统 shall 保持顶部标题栏和底部输入栏可见不变

### Requirement 2: 防坑指南与出门提醒展示
**Objective:** 作为用户，我希望在行程页直接看到防坑指南和出门提醒，而不需要跳转到另一个页面

#### Acceptance Criteria
1. When 后端返回 `pitfall_reminders` 数据, the 系统 shall 以蓝色信息卡片形式展示所有出门提醒条目
2. When 后端返回 `pitfall_insights` 数据, the 系统 shall 以深色卡片（黄色标题+灰色正文）展示所有防坑指南条目
3. When 后端返回 `pitfall_triggers` 或 `anomaly_triggers` 数据, the 系统 shall 渲染对应的交互按钮（确认/忽略），点击确认后重新刷新行程视图
4. If 后端未返回任何防坑或提醒数据, the 系统 shall 跳过对应区块不显示空卡片

### Requirement 3: 地铁式时间轴展示
**Objective:** 作为用户，我希望通过横向滑动的时间轴直观查看完整行程安排，并能点击节点查看地图

#### Acceptance Criteria
1. When 后端返回 `timeline` 数据, the 系统 shall 以深色卡片内的横向滚动时间轴展示所有行程节点（出发/等待/执行/取回）
2. When 用户左右滑动时间轴, the 系统 shall 支持惯性滚动查看全部节点
3. When 用户点击时间轴上的任意节点, the 系统 shall 打开地图面板并聚焦到该节点对应的地理位置
4. The 系统 shall 在时间轴底部显示"左右滑动查看完整行程 · 点击节点查看地图"的操作提示

### Requirement 4: 真实地图面板
**Objective:** 作为用户，我希望看到基于高德地图的真实路线可视化，包括所有途经点标记和完整路线折线

#### Acceptance Criteria
1. When 用户点击时间轴节点, the 系统 shall 以缩放动画打开全屏地图面板
2. When 地图面板打开, the 系统 shall 加载高德 JS API 地图并以所有途经点的最佳视野展示
3. When 地图渲染完成, the 系统 shall 以黄色折线（Polyline）连接所有途经点，并在每个途经点放置带序号的圆形标记
4. When 用户点击途经点标记, the 系统 shall 弹出信息窗显示该点名称和时间
5. When 用户点击返回按钮, the 系统 shall 以缩放动画关闭地图面板并清理地图资源

### Requirement 5: 时间驱动的用户位置追踪
**Objective:** 作为用户，我希望在地图上看到代表"当前应该在哪"的动态标记，其位置由虚拟时钟驱动而非 GPS

#### Acceptance Criteria
1. When 地图面板打开且虚拟时钟处于活跃状态, the 系统 shall 每 5 秒轮询 `/api/clock/status` 获取当前虚拟时间
2. While 虚拟时间处于某两个途经点的时间区间内, the 系统 shall 通过线性插值计算用户当前坐标并将绿色脉冲标记移动到对应位置
3. When 用户当前坐标移出地图可视范围, the 系统 shall 自动平移地图将用户标记重新纳入视野
4. When 地图面板关闭, the 系统 shall 停止轮询并销毁位置追踪器

### Requirement 6: 底部输入栏模式切换
**Objective:** 作为用户，我希望在执行计划后底部输入栏自动切换为"行程中聊天"模式，方便我调整行程

#### Acceptance Criteria
1. When 用户点击"执行计划", the 系统 shall 将底部输入栏 placeholder 切换为"输入消息调整行程..."
2. While 处于行程模式, the 系统 shall 将发送消息路由至 `/api/edit_trip` 而非 `/api/chat/stream`
3. When 后端返回更新后的行程数据, the 系统 shall 刷新行程视图（时间轴+防坑指南+提醒）并在聊天区显示"已按你的要求更新行程"确认气泡
4. When 用户结束行程, the 系统 shall 将底部输入栏恢复为默认搜索模式（placeholder "输入您的需求"）

### Requirement 7: 结束行程清理
**Objective:** 作为用户，我希望结束行程后界面干净地回到初始状态

#### Acceptance Criteria
1. When 用户点击"结束行程"按钮, the 系统 shall 关闭地图面板（如已打开）并销毁地图实例
2. When 用户点击"结束行程"按钮, the 系统 shall 移除 `#dynamic-content` 中的行程视图容器
3. When 用户点击"结束行程"按钮, the 系统 shall 重置底部输入栏为搜索模式并显示"行程结束"确认提示
4. When 用户点击"结束行程"按钮, the 系统 shall 清空所有行程相关全局状态变量（包括坐标缓存和地图标记数组）
