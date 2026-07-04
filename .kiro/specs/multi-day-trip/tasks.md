# 多日行程 — 实施任务

## Wave 1: 基础设施 ✅

- [x] **T1.1** 扩展 `session_state` + `_reset_session()` 支持多日字段
- [x] **T1.2** 实现 `_apairs()` 辅助函数
- [x] **T1.3** 添加 hotel/scenic/breakfast/shopping 品类（main.py, server.py, amap_poi.py）
- [x] **T1.4** 修复 requirements.md typo，创建 spec.json

## Wave 2: 热门推荐 + 设置面板 ✅

- [x] **T2.1** 实现 `GET /api/popular_attractions`
- [x] **T2.2** 实现 `POST /api/set_trip_config`
- [x] **T2.3** 构建设置面板 Modal UI（两步向导）
- [x] **T2.4** 实现聊天框补充 POI 流程

## Wave 3: 核心排程引擎 ✅

- [x] **T3.1** 创建 `skills/multi_day_scheduler/multi_day_scheduler.py`
- [x] **T3.2** 实现 `_cluster_by_geo()` (KMeans++ with scipy/纯Python回退)
- [x] **T3.3** 实现 `_balance_clusters()` (贪心负载均衡)
- [x] **T3.4** 实现每日TSP（最近邻 + 2-opt优化）
- [x] **T3.5** 实现 `_insert_meals()` + `_global_fine_tune()`
- [x] **T3.6** 实现 `POST /api/smart_schedule` 端点

## Wave 4: 多日 API + 前端展示 ✅

- [x] **T4.1** 实现 `/api/set_trip_days` + `/api/switch_day` + `/api/move_to_day`
- [x] **T4.2** 更新 `/api/edit_trip` 注入多日上下文
- [x] **T4.3** 构建日卡片视图（水平滑动 + 时间线摘要）
- [x] **T4.4** 构建日指示器 + 候选池栏
- [x] **T4.5** 实现前端多日状态管理

## Wave 5: 验证 ✅

- [x] **T5.1** API 端点编译和基础功能验证
- [ ] **T5.2** 端到端 2 日北京游完整流程手工验证
- [ ] **T5.3** 单日→多日切换 + 数据迁移验证
- [ ] **T5.4** 边界条件（空候选池 / k>POI 数 / scipy 不可用回退）
- [ ] **T5.5** 向后兼容：所有现有单日流程不中断
