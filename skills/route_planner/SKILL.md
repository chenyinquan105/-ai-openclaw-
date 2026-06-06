# 路径规划 Skill — route_planner

## 概述
为美团 AI 沙盒提供多节点路径规划能力。给定出发坐标 + 目的地列表 + 交通偏好，计算最优访问顺序与接驳方式。

## 输入契约

```json
{
  "start_coord": "39.93,116.45",
  "waypoints": [
    {"id": "shop_rest_01", "name": "海底捞三里屯店", "coord": "39.936,116.449", "duration_minutes": 60},
    {"id": "shop_hair_01", "name": "沙宣三里屯店",   "coord": "39.934,116.453", "duration_minutes": 45}
  ],
  "transport_preference": "打车优先",
  "walking_tolerance_meters": 800,
  "weather_condition": null
}
```

### 参数说明
| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `start_coord` | `str` | ✅ | 出发坐标 "lat,lng" |
| `waypoints` | `list` | ✅ | 途经点列表，每个包含 id/name/coord/duration_minutes |
| `transport_preference` | `str` | ✅ | "步行优先"/"打车优先"/"地铁优先" |
| `walking_tolerance_meters` | `int` | ✅ | 步行容忍距离（米），超过此距离自动建议打车 |
| `weather_condition` | `str` | 可选 | 天气状况，雨天/雪天 → 缩短步行容忍距离 |

## 输出契约

```json
{
  "status": "SUCCESS",
  "route": [
    {
      "order": 0,
      "from": "起点",
      "to": "沙宣三里屯店",
      "to_coord": "39.934,116.453",
      "transport_mode": "步行",
      "distance_meters": 513,
      "duration_minutes": 6,
      "activity": {"name": "理发", "duration_minutes": 45},
      "arrive_time": "09:06",
      "depart_time": "09:51"
    },
    {
      "order": 1,
      "from": "沙宣三里屯店",
      "to": "海底捞三里屯店",
      "to_coord": "39.936,116.449",
      "transport_mode": "打车",
      "distance_meters": 750,
      "duration_minutes": 4,
      "activity": {"name": "吃火锅", "duration_minutes": 60},
      "arrive_time": "09:55",
      "depart_time": "10:55"
    }
  ],
  "total_distance_meters": 1263,
  "total_travel_minutes": 10,
  "total_activity_minutes": 105,
  "alerts": [
    "步行至沙宣 513m 在容忍范围内(800m)，建议步行",
    "沙宣→海底捞 750m 超出步行容忍(800m × 天气修正=640m)，建议打车"
  ]
}
```

## 核心算法
1. **TSP 近似**：按距离贪心排序（最近邻优先）
2. **交通模式判定**：距离 ≤ walking_tolerance_meters × 天气修正 → 步行；否则按 transport_preference 选择
3. **天气修正**：暴雨 → 0.5, 小雨 → 0.8, 大雪 → 0.3, 正常 → 1.0
4. **接驳耗时估算**：步行 80m/min, 打车 400m/min, 地铁 300m/min, 公交 180m/min

## 边界条件
- 空 waypoints → `{"status": "ERROR", "message": "无途经点"}`
- 单节点 → 直接计算起点→该点的路径
- 坐标格式错误 → `{"status": "ERROR", "message": "坐标格式无效"}`
