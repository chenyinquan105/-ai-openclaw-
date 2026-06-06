# 餐厅排队监控 Skill — queue_monitor

## 概述
模拟美团餐厅实时排队监控。为每家餐厅维护虚拟排号状态（前方桌数、预计等待时间），支持排号、查询、状态推送。

## 输入契约

### 排号
```json
{
  "action": "enqueue",
  "shop_id": "shop_rest_01",
  "shop_name": "海底捞三里屯店",
  "party_size": 2
}
```

### 查询
```json
{
  "action": "query",
  "queue_id": "Q_xxx"
}
```

### 轮询（批量）
```json
{
  "action": "poll_all"
}
```

## 输出契约

### 排号返回
```json
{
  "status": "SUCCESS",
  "queue_id": "Q_1780720000",
  "shop_name": "海底捞三里屯店",
  "tables_ahead": 12,
  "estimated_wait_minutes": 35,
  "party_size": 2,
  "timestamp": "12:00"
}
```

### 轮询返回
```json
{
  "status": "SUCCESS",
  "queues": [
    {
      "queue_id": "Q_xxx",
      "shop_name": "海底捞三里屯店",
      "tables_ahead": 3,
      "estimated_wait_minutes": 8,
      "alert": "即将轮到 — 还剩 3 桌！建议准备出发"
    }
  ],
  "alerts": ["海底捞三里屯店: 还剩 3 桌 (约8分钟)，建议叫车出发"]
}
```

## 模拟规则
- 初始排队数：8-20 桌随机
- 消化速度：3-6 分钟/桌（午餐快/晚餐慢）
- 每 5 分钟虚拟时钟推进一次
- ≤5 桌时触发提醒
- ≤1 桌时触发"立即出发"告警

## 边界条件
- shop_id 不存在 → `{"status": "ERROR", "message": "店铺不存在"}`
- 重复排号 → `{"status": "ERROR", "message": "已排号"}`
