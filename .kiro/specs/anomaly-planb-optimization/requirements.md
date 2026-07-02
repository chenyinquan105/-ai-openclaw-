# Requirements Document

## Introduction
美团AI管家虚拟控制台（Virtual Console）是行程沙盒仿真系统的操作面板。当前存在三个交互缺陷和一个增强需求：异常注入按钮无激活态视觉反馈、暴雨PlanB依赖本地缓存而非实时API、换店操作擅自修改目的地且通知消息静默丢失、异常弹窗缺少响铃提示。本规格定义这四项优化的用户可观测行为要求。

## Boundary Context
- **In scope**: 左侧异常控制面板按钮交互、PlanB弹窗响铃、暴雨场景饮品店推荐数据源与推荐形态、换店/排号异常的用户确认流程与通知、行程时间轴栏的动态更新
- **Out of scope**: 后端排程算法改动、新增异常类型、提醒模块（吃药/喝水）的行为变更、地图面板改动、商户搜索逻辑改动
- **Adjacent expectations**: 高德API可用性影响推荐数据新鲜度（需有降级策略）；响铃复用现有提醒模块的Web Audio实现；行程时间轴栏的渲染机制（`renderMetroTimelineForMainFace`）已正常工作

## Requirements

### Requirement 1: 异常按钮激活态红框切换
**Objective:** As a 沙盒测试用户, I want 点击左侧面板异常注入按钮时看到明显的激活态视觉反馈（红框），再次点击时取消激活，so that 我能直观了解当前哪些异常正在影响行程。

#### Acceptance Criteria
1. When 用户点击左侧面板异常按钮（下暴雨/餐厅停电/排号异常/堵车）且该异常未激活，the 控制台 shall 将该按钮外框变为红色（`border-red-500`）并打开PlanB推荐弹窗
2. When 用户再次点击已激活的异常按钮，the 控制台 shall 移除红色外框恢复灰色边框，并取消该异常状态
3. While 多个异常同时激活，the 控制台 shall 各自独立显示红框状态，互不干扰
4. When 异常被取消（通过按钮toggle或chip移除），the 控制台 shall 同步移除对应按钮的红框状态
5. The 控制台 shall 在左侧面板显示"激活的异常"chip列表，每个chip展示异常类型名称，点击chip可移除该异常

### Requirement 2: 暴雨PlanB接入高德实时API推荐饮品店
**Objective:** As a 行程中的用户, I want 触发暴雨异常并选择避雨时获得基于高德实时API查询的最近饮品店推荐（含店名和距离），so that 我能基于准确、实时的信息做出避雨决策。

#### Acceptance Criteria
1. When 用户选择暴雨PlanB的"就近找奶茶/咖啡店避雨"选项，the 系统 shall 调用高德API `search_nearby` 实时查询用户当前位置3km范围内的咖啡厅/茶饮店铺
2. When 高德API返回结果，the 系统 shall 展示最近饮品店的店名、评分和距离信息
3. When 展示推荐饮品店，the 系统 shall 同时提供三种操作选项：前往该店避雨、打车前往原目的地、取消
4. If 高德API不可用或返回空结果，the 系统 shall 降级使用本地poi_cache数据并告知用户数据来源
5. When 用户选择"前往该店避雨"，the 系统 shall 先展示确认消息询问用户是否确认前往，用户确认后才执行避雨节点插入

### Requirement 3: 换店操作须经用户确认且直接更新时间轴
**Objective:** As a 行程中的用户, I want 在排号异常或餐厅停电等需要更换目的地的场景中，系统必须先询问我确认后再执行更换，并通过行程聊天区明确告知变更内容且直接更新顶部时间轴，so that 我始终对行程变更拥有最终决定权且能清晰感知变更结果。

#### Acceptance Criteria
1. When 用户在二级弹窗中选择替换店铺并点击确认，the 系统 shall 不在此时直接执行API更换，而是在行程聊天区展示橙色确认消息，明确列出异常原因、替代店名、评分、距离，并询问"是否确认将[事项]地点更换为此店"
2. When 用户点击确认消息中的"确认更换"按钮，the 系统 shall 执行店铺更换API，成功后更新顶部水平时间轴栏（`trip-timeline-bar`）并发送蓝色通知消息告知变更结果
3. When 用户点击确认消息中的"取消"按钮，the 系统 shall 不执行任何更换操作，并发送通知消息"已取消更换，原[事项]计划保持不变"
4. When 更换成功，the 系统 shall 不弹出深色行程卡片（`bg-gray-900` schedule card），仅更新顶部时间轴和行程视图
5. When 异常被手动解除（如停电恢复），the 系统 shall 在行程聊天区显示恢复通知消息（如"餐厅供电已恢复，该店铺已正常营业，可前往用餐"）

### Requirement 4: 异常PlanB弹窗伴随响铃
**Objective:** As a 多任务操作的用户, I want 异常PlanB推荐弹窗出现时伴随一声提示音，so that 我不会错过突发的异常提醒。

#### Acceptance Criteria
1. When 异常PlanB推荐弹窗（`#demo-exception-modal`）显示时，the 系统 shall 播放一声通知提示音（复用现有 `_playNotificationSound` 的Web Audio双音叮咚）
2. When 用户关闭弹窗而未执行PlanB，the 系统 shall 不重复播放提示音
3. The 提示音 shall 与现有提醒模块（吃药/喝水提醒）使用相同的音效实现，保持一致的用户体验
