# 多日行程 — 技术设计

## 一、架构概览

```
┌─────────────────────────────────────────────────┐
│                  index.html                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ 设置面板  │  │ 日卡片视图 │  │ 候选池底部栏  │  │
│  │ (Modal)  │  │(水平滑动) │  │ (多日模式)    │  │
│  └────┬─────┘  └────┬─────┘  └──────┬───────┘  │
│       │              │               │          │
└───────┼──────────────┼───────────────┼──────────┘
        │              │               │
   ┌────▼──────────────▼───────────────▼──────────┐
   │                  server.py                    │
   │  /api/set_trip_config  /api/smart_schedule    │
   │  /api/popular_attractions  /api/switch_day    │
   │  /api/move_to_day  /api/set_trip_days         │
   └──────────────────────┬───────────────────────┘
                          │
   ┌──────────────────────▼───────────────────────┐
   │     skills/multi_day_scheduler/              │
   │     multi_day_scheduler.py                   │
   │  5-Phase Pipeline:                           │
   │  Cluster → Balance → TSP → Meals → Tune      │
   └──────────────────────────────────────────────┘
```

## 二、数据模型

### session_state 扩展

```python
session_state = {
    # 现有字段（保持不变）
    "selected_pairs": [],   # 单日模式
    "task_list": [],
    "spatial_matrix": {},
    # ... 

    # 多日字段
    "trip_mode": "single" | "multi",
    "trip_days": 1-7,
    "trip_destination": "北京",
    "trip_transport": "步行优先",
    "trip_checkin_lat": None,
    "trip_checkin_lng": None,
    "active_day_index": 0,
    "days": [{day_index, label, selected_pairs, task_list, spatial_matrix, schedule_result, chat_history, transport_override}],
    "candidate_pool": [{shop_id, name, category, lat, lng, coord, rating, ...}],
}
```

### 向后兼容

`_apairs()` 辅助函数自动根据 `trip_mode` 返回正确的 selected_pairs：
- 单日模式 → 顶层 selected_pairs
- 多日模式 → 活跃天的 selected_pairs

## 三、智能排程引擎

### 5 阶段流水线

1. **地理聚类** — KMeans++ (scipy/纯Python回退)，k=天数
2. **负载均衡** — 贪心重分配，每天8h±15%
3. **每日TSP** — 最近邻贪心 + 2-opt 局部优化
4. **用餐插入** — 午餐11:30-13:30，晚餐17:30-19:30
5. **全局微调** — 跨天边界交换，减小总方差

### 算法选择

- KMeans++ 而非 DBSCAN：保证恰好k个簇=恰好N天
- 2-opt 后处理：在最近邻基础上提升5-15%
- Haversine 距离：与现有 route_planner 保持一致

### 成本函数

```
total_cost = Σ(0.6×移动时间 + 0.3×|日时长-目标| + 0.1×离酒店距离)
```

## 四、API 契约

### 新增端点

| 端点 | 方法 | 请求 | 响应 |
|------|------|------|------|
| `/api/popular_attractions` | GET | `?city=北京` | `{attractions: [...], total: N}` |
| `/api/set_trip_config` | POST | `{days, destination, transport}` | `{trip_mode, attractions: [...], ...}` |
| `/api/smart_schedule` | POST | `{start_time: "09:00"}` | `{days: [{timeline, pairs, route}], metadata}` |
| `/api/switch_day` | POST | `{day_index: 1}` | `{active_day_index, day_data}` |
| `/api/move_to_day` | POST | `{shop_id, from_day, to_day}` | `{moved_shop, message}` |
| `/api/set_trip_days` | POST | `{mode, days}` | `{trip_mode, trip_days}` |

### 现有端点修改

- `/api/start` — 多日模式下追加到 candidate_pool
- `/api/edit_trip` — 注入多日上下文到 LLM
- `/api/reset` — 清理多日字段
- `/api/choose_shop` — 多日模式下追加到 candidate_pool

## 五、前端组件

| 组件 | 技术方案 |
|------|---------|
| 模式切换按钮 | Header 天气和齿轮之间，📅图标 |
| 设置面板 Modal | 两步向导：Step1 天数+目的地+交通，Step2 Top20 选择 |
| 候选池底部栏 | 黄色背景栏，显示已选数量和排程按钮 |
| 日卡片视图 | 水平滑动容器，scroll-snap，点指示器导航 |
| 聊天补充 | 底部输入框支持自然语言添加POI到候选池 |

## 六、品类扩展

新增 4 个品类：hotel（酒店）、scenic（景点）、breakfast（早餐）、shopping（购物）

修改文件：`main.py`、`server.py`、`skills/amap_poi/amap_poi.py`
