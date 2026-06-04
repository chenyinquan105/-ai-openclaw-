# time_master 虚拟时钟 skill

## 业务定位
沙盒系统的虚拟时钟芯片，只负责时间维度的纯数学计算。用于演示 Demo 的虚拟时间控制台。

## 架构原则
- **单一时钟源**：服务端持有最终状态，客户端只发指令，不传当前时间
- **纯数学计算**：不涉及任何系统真实时间
- **倍速走时**：服务端定时器驱动，客户端只需发 start/stop
- **排程联动**：推进/跳转后返回 triggered_events，由上层调用方处理节点完成

## 控制模式
| 模式 | API | 行为 |
|------|-----|------|
| QUICK_FORWARD | POST /api/clock/offset {delta_minutes} | 相对快进 N 分钟 |
| SLIDER_DRAG | POST /api/clock/jump {target_time} | 绝对跳转到指定时间 |
| AUTO_TICK | POST /api/clock/start {speed} → .../stop | 服务端定时器自动走时 |

## API 端点
- `POST /api/clock/offset` — 相对偏移
- `POST /api/clock/jump` — 绝对跳转
- `POST /api/clock/start` — 启动自动走时
- `POST /api/clock/stop` — 停止自动走时
- `POST /api/clock/schedule` — 注册排程节点
- `GET /api/clock/status?session_id=xxx` — 获取状态

## 与排程引擎的联动
- 排程引擎生成计划后调用 `/api/clock/schedule` 注册节点
- 虚拟时钟推进时自动检查触发节点并返回 triggered_events
- 触发的节点从待触发列表移除，标记为已触发
- 前端轮询 `/api/clock/status` 获取最新状态和已触发列表

## 时间显示
前端所有时间显示（倒计时、进度条等）全部从 `/api/clock/status` 读取虚拟时间。

## 会话隔离
- 每个 session_id 有独立的 ClockState
- 支持多会话并发，互不干扰
