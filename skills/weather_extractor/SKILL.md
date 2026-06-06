# 天气活动抽取 Skill — weather_extractor

## 概述
提供天气状况查询与活动推荐。模拟天气数据（含随机变化），根据天气推荐/警示户外活动。

## 输入契约

```json
{
  "coord": "39.93,116.45",
  "date": "2026-06-06"
}
```

## 输出契约

```json
{
  "status": "SUCCESS",
  "weather": {
    "condition": "小雨",
    "condition_en": "light_rain",
    "temperature_c": 22,
    "humidity": 78,
    "wind_kmh": 15,
    "uv_index": 3,
    "hourly": [
      {"time": "12:00", "condition": "阴", "temp": 21},
      {"time": "13:00", "condition": "小雨", "temp": 22},
      {"time": "14:00", "condition": "中雨", "temp": 20}
    ]
  },
  "activity_advice": {
    "outdoor": "不推荐",
    "reason": "小雨将持续至下午，路面湿滑",
    "suggestions": ["建议带伞", "户外活动建议改期", "步行路段可能较慢"],
    "alternative": "推荐室内活动：电影院/商场/咖啡馆"
  },
  "transport_impact": {
    "walking_penalty": 0.8,
    "traffic_risk": "中等",
    "advice": "雨天路滑，建议减少步行路段"
  }
}
```

## 模拟规则
- 基于日期和坐标生成伪随机天气（保证同一天同坐标结果一致）
- 天气条件：晴/多云/阴/小雨/中雨/大雨/暴雨/小雪/大雪
- 每小时有 15% 概率变化
- 极端天气（暴雨/大雪）触发出行警告

## 边界条件
- 坐标无效 → `{"status": "ERROR", "message": "坐标格式无效"}`
