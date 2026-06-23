# Technology Stack

## Architecture

**"OpenClaw 控制面网关 + Python 算法专家节点"** 解耦架构：

- **控制面**：OpenClaw TypeScript Gateway → `skills/meituan-bridge/` 插件注册 14 个 Agent Tool → HTTP localhost:5000 桥接到 Python 后端
- **数据面**：Python Flask + 9 个 Skill 模块 → 业务逻辑全部在 Python 侧
- **推送面**：SSE (`/api/sse/events`) + 守护线程 `_realtime_reminder_poller`（每 30s 轮询）
- **表现面**：HTML5 单页 + Tailwind CSS + EventSource 长连接

## Core Technologies

- **Language**: Python 3.9+ (后端), TypeScript (meituan-bridge 插件)
- **Framework**: Flask + Flask-CORS
- **Runtime**: Node.js 18+ (插件编译), gunicorn -w 4 (Flask 多 worker)
- **LLM**: DeepSeek Chat (via OpenAI SDK)

## Key Libraries

- `flask`, `flask-cors` — HTTP API + SSE 端点
- `gunicorn` — 多 worker 生产服务器
- `openai` — DeepSeek Chat LLM 调用
- `python-dotenv` — 环境变量管理
- `difflib` (stdlib) — 店名模糊匹配纠偏

## Development Standards

### LLM 交互规范
- 所有 LLM 调用通过 `MeituanAgent._call_llm()` 统一入口
- Tool Call 重试机制：最多 5 次重试确保 LLM 调用搜索工具
- 系统提示词用中文，工具参数用英文（snake_case）

### HTTP API 规范
- 路由前缀 `/api/`，按功能分：`/api/start`, `/api/clock/*`, `/api/profile/*`, `/api/reminder/*`, `/api/anomaly/*`, `/api/pitfall/*`, `/api/route/*`, `/api/queue/*`, `/api/weather`
- 响应格式统一为 `{"status": "SUCCESS"|"ERROR"|"CONFIRM_REQUIRED", ...}`
- SSE 端点 `/api/sse/events` 使用 `text/event-stream`

### 偏好存储
- 使用 Markdown 表格文件 `管家记忆.md`，人类可读 + LLM 可解析
- 正则按 `## 口味/通勤/预算/健康作息` 分节解析
- 无外部数据库依赖

### 代码组织
- 每个 Skill 一个目录，入口函数命名：`search_poi_matrix()`, `solve_concurrent_timeline()`, `plan_route()`, etc.
- 从 `skills/` 目录动态 import，通过 `sys.path` 追加
- 异常处理：所有外部调用包裹 try/except，提供降级默认值

## Development Environment

### Required Tools
- Python 3.9+
- Node.js 18+
- OpenClaw Gateway >= 2026.5.17

### Common Commands
```bash
# 启动后端：./start.sh（清端口 → gunicorn -w 4）
# 构建插件：cd skills/meituan-bridge && npm install && npm run build
# 访问沙盒：浏览器打开 index.html
```

## Key Technical Decisions

1. **不重写为纯 TS**：6 个核心 Python Skill 共 ~2500 行业务逻辑，HTTP Bridge 方案保留全部现有代码
2. **虚拟时钟 vs 真实时间分离**：SSE 提醒轮询线程使用系统真实时间，不依赖虚拟时钟状态
3. **偏好谱用 Markdown**：人类可读 + LLM 可解析，正则分节，零数据库依赖
4. **Mock 数据封闭**：所有商户/天气/排队数据本地 Mock，确保 Demo 可复现
5. **gunicorn 多 worker**：支持并发请求，SSE 客户端列表和提醒锁在进程内共享
6. **双轨设计**：`main.py` 同时支持 CLI 交互模式（模式1）和沙盒仿真模式（模式2），共享同一套 Skill
