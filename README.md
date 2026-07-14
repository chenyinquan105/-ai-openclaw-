# 🚀 Meituan_Spatial_Butler — 基于 OpenClaw 的全天候时空沙盒数字管家

本项目利用开源智能体框架 OpenClaw 的 IM 原生交互与扩展机制，构建了一个具备**长期偏好动态谱**、**环境拓扑异动感知**与**异步流控催促闭环**的高级本地生活数字管家。

---

## 🏗️ 系统技术架构

采用 **“OpenClaw 控制面网关 + Python 算法专家节点”** 的解耦架构，通过 HTTP Bridge 契约发现与 SSE 实时流控中枢实现高语义、低延迟的本地生活决策。

```
用户交互层 (WebChat / IM)
        │
        ▼
┌───────────────────────────────────────────┐
│  OpenClaw Gateway (TypeScript)            │
│  ┌─────────────────────────────────────┐  │
│  │  skills/meituan-bridge 插件         │  │
│  │  - 14 个 Agent Tool 契约注册        │  │
│  │  - 会话生命周期 & IM 通道管理       │  │
│  └──────────────┬──────────────────────┘  │
└─────────────────┼─────────────────────────┘
                  │ HTTP localhost:5000
┌─────────────────▼─────────────────────────┐
│  Python Flask 后端 (server.py)            │
│  ┌─────────────────────────────────────┐  │
│  │  核心 Skill 矩阵 (9 个技能模块)     │  │
│  │  - 商户检索 / 排程引擎 / 路径规划   │  │
│  │  - 异常感知 / 防踩坑 / 排队监控    │  │
│  │  - 天气抽取 / 虚拟时钟 / 提醒状态机 │  │
│  └──────────────┬──────────────────────┘  │
│  ┌──────────────▼──────────────────────┐  │
│  │  管家记忆偏好谱引擎                 │  │
│  │  - 四维度长期偏好读写               │  │
│  │  - 语义检测自动更新 (LLM 驱动)      │  │
│  └─────────────────────────────────────┘  │
└─────────────────┬─────────────────────────┘
                  │ SSE + HTTP
┌─────────────────▼─────────────────────────┐
│  表现层：index.html                       │
│  - 虚拟时钟控制台（拖拽快进/跳转）        │
│  - 异常模拟面板（暴雨/停电/堵车注入）     │
│  - SSE 五步强闭环提醒弹窗                 │
│  - 排程时间线 3D 翻转卡片                │
└───────────────────────────────────────────┘
```

### 核心层级分工

| 层级 | 技术栈 | 职责 |
|------|--------|------|
| **控制面** | OpenClaw TS + meituan-bridge 插件 | LLM Tool Call 调度、IM 通道、会话状态管理 |
| **数据面** | Python Flask + 9 个 Skill 模块 | 时空约束求解、商户搜索、排程管线变异 |
| **推送面** | SSE + 守护线程 | 提醒广播、异常事件实时推送 |
| **表现面** | HTML5 单页 + Tailwind CSS | 沙盒可视化控制台、弹窗交互 |

---

## 🌟 核心功能

### 1. 长期偏好谱 (Long-Term Memory Profile)

系统实时维护 `管家记忆.md`，涵盖四大偏好维度：

| 维度 | 字段 | 示例值 | 注入目标 |
|------|------|--------|----------|
| 口味 | `taste_tolerance`, `dietary_restrictions`, `cuisine_preference` | 无辣, 日料优先 | POI 搜索过滤 |
| 通勤 | `walking_tolerance_meters`, `transport_priority` | 800m, 打车优先 | 路径规划 / 防踩坑 |
| 预算 | `price_level`, `rating_cutoff` | 中端, 4.3分 | POI 评分筛选 |
| 健康 | `hydration_interval_minutes`, `medication_schedule` | 90min, 08:00:降压药 | 提醒系统 |

- **读取**：每次搜索/排程前自动注入，偏好菜系置顶、忌口商户隐藏
- **写入**：交互结束后调用 `/api/memory/detect`，由 LLM 语义检测偏好变化并更新
- **存储**：Markdown 表格格式，人类可读 + LLM 可解析

### 2. Skill 矩阵

| Skill | 入口函数 | 功能 |
|-------|----------|------|
| `generic_poi_searcher` | `search_poi_matrix()` | 多维约束商户检索（品类/评分/距离/价格/忌口），Mock 14 家三里屯商户 |
| `concurrent_pipeline_scheduler` | `solve_concurrent_timeline()` | 并发排程引擎（空间重心策略 + 冲突检测 + CONFIRM_REQUIRED 交互） |
| `route_planner` | `plan_route()` | 多节点路径规划，融合天气衰减系数 |
| `destination_anti_pitfall` | `execute_anti_pitfall_skill()` | 防踩坑校验（步行超阈值 → 自动改打车 + `SWAP_NODE` 容灾） |
| `queue_monitor` | `handle()` | 餐厅排队监控（排号/查询/轮询，≤5 桌时触发叫车提醒） |
| `anomaly_sensor_skill` | `execute_anomaly_sensor_skill()` | 异常感知器（拓扑污染评估 → Plan B 变轨：SWAP/BYPASS/POSTPONE） |
| `weather_extractor` | `extract_weather()` | 天气数据模拟（坐标+日期确定性随机，输出步行惩罚系数+活动建议） |
| `time_master` | `get_master()` | 虚拟时钟芯片（快进/跳转/倍速/自动走时，排程节点触发联动） |
| `task_reminder_skill` | `ReminderStateManager` | 五步强闭环提醒状态机（喝水/吃药催促 → 45min 无响应联系紧急联系人） |

### 3. 异步实时推送 (SSE + 守护线程)

- **`_realtime_reminder_poller`** 守护线程：每 30 秒用系统真实时间独立轮询，检测 WATER/MED 提醒节点是否到期
- **SSE 端点** (`/api/sse/events`)：前端通过 `EventSource` 长连接接收推送
- **五步强闭环催促**：提醒到期 → 弹窗 → 确认拿药 → 确认吞服 → 延时再次催促 → 45min 无响应触发紧急联络人
- **不依赖虚拟时钟**：无论沙盒时钟是否在运行，真实时间的提醒独立生效

### 4. 24H 动态沙盒 (变轨容灾)

- **虚拟时钟控制台**：拖拽快进/跳转到任意时刻，自动触发排程节点完成和提醒事件
- **异常模拟面板**：一键注入"暴雨/停电/排号异常/堵车"四种外部异动
- **管线变异器**：`anomaly_sensor_skill` 捕获异常 → `SWAP_NODE` 平替 / `BYPASS_NODE` 跳过 / `POSTPONE_NODE` 延后 → UI 弹窗请求确认

---

## 📂 项目结构

```text
项目根目录/
├── index.html                    # 沙盒可视化前端主控台（~3600行）
├── server.py                     # Flask 核心后端（~2200行）：路由/SSE/偏好引擎/提醒轮询
├── main.py                       # 排程中枢：LLM 对话引擎 + 双轨排程（静态/沙盒）
├── start.sh                      # 一键启动：清端口 → gunicorn -w 4
├── 管家SOUL.md                   # 管家角色设定契约（身份/行为准则/Skill 映射）
├── 管家记忆.md                   # 用户长期偏好谱数据（Markdown 表格，系统自动维护）
├── 管家偏好谱需求规格.md          # 偏好 Schema 技术文档（字段定义/注入规范）
├── REPAIR_PLAN.md                # 竞赛漏洞修复计划（架构决策 + 5 Phase 实现记录）
├── .env                          # 环境变量（API Key）
│
├── skills/
│   ├── meituan-bridge/           # OpenClaw TypeScript 插件（14 个 Agent Tool HTTP Bridge）
│   │   ├── src/index.ts          # defineToolPlugin 实现
│   │   ├── openclaw.plugin.json  # 工具契约清单
│   │   └── package.json
│   ├── generic_poi_searcher/     # 商户多维检索器（Mock 14 家商户 DB）
│   ├── concurrent_pipeline_scheduler/  # 并发排程引擎
│   ├── route_planner/            # 多节点路径规划
│   ├── destination_anti_pitfall/ # 目的地防踩坑引擎
│   ├── queue_monitor/            # 餐厅排队状态机
│   ├── anomaly_sensor_skill/     # 异常环境感知器
│   ├── weather_extractor/        # 天气数据模拟
│   ├── time_master/              # 虚拟时钟芯片
│   └── task_reminder_skill/      # 五步强闭环提醒状态机
│
├── memory/                       # 开发日志
│   ├── 2026-06-05.md
│   ├── 2026-06-06.md
│   └── time_master.md
│
└── schemas/                      # JSON Schema 定义
    ├── reminder_input.schema.json
    └── reminder_output.schema.json
```

---

## 📥 从零开始（演示电脑一键部署）

> 🎯 **演示方只需 3 步，开箱即用，和开发者环境完全一致。**

### 完整部署（Web 控制台 + OpenClaw 聊天）

```bash
# 1. 克隆项目
git clone git@github.com:chenyinquan105/-ai-openclaw-.git
cd -ai-openclaw-

# 2. 一键配置（自动安装 Python/Node.js 依赖 + OpenClaw Gateway + 插件 + 配置）
chmod +x setup.sh && ./setup.sh

# 3. 一键启动所有服务
chmod +x start-all.sh && ./start-all.sh
```

启动后：
- 🌐 **Web 控制台**：浏览器打开 **http://localhost:5000**
- 💬 **OpenClaw 聊天**：通过 OpenClaw 客户端连接 Gateway（port 18789）

### 仅 Web 控制台（无需 OpenClaw）

如果你只需要 Web 沙盒控制台（排程/时钟/提醒/异常模拟），不需要 IM 聊天功能：

```bash
chmod +x setup.sh && ./setup.sh
chmod +x start.sh && ./start.sh
# 浏览器打开 http://localhost:5000
```

### 环境要求

| 依赖 | 版本 | 用途 |
|------|------|------|
| Python | 3.9+ | Flask 后端 + LLM 调用 |
| Node.js | 22.19+ | OpenClaw Gateway（仅完整部署需要） |

> ✅ `.env` 已预配置 DeepSeek + 高德地图 API Key，`openclaw-config.template.json` 已预配置 OpenClaw 全部设置，**clone 后无需任何手动配置**。

---

## 🛠️ 快速启动（手动分步）

### 1. 安装 Python 依赖

```bash
pip3 install -r requirements.txt
```

### 2. 启动 Python 后端

```bash
chmod +x start.sh
./start.sh
```

`start.sh` 自动清理 5000 端口占用，以 `gunicorn -w 1 --threads 4` 启动 Flask 服务。

控制台输出 `✅ 服务已启动: http://localhost:5000` 即表示后端就绪。

### 3. 部署 OpenClaw 桥接插件

```bash
cd skills/meituan-bridge
npm install
npm run build
cd ../..
```

插件将 14 个 Agent Tool 注册到 OpenClaw Gateway，通过 HTTP Bridge 桥接到 Python 后端。

### 4. 访问沙盒控制台

浏览器打开项目根目录下的 `index.html`，即可进入 24H 时空沙盒控制台：
- 左侧：异常模拟面板（注入暴雨/停电/堵车）
- 中间：排程时间线与 POI 搜索结果
- 右侧：虚拟时钟控制台（快进/跳转/倍速）

---

## 🔌 Agent Tool 清单（14 个）

| Tool 名称 | HTTP 路由 | 功能 |
|-----------|-----------|------|
| `meituan_search_poi` | `POST /api/start` | POI 搜索 + 选店 + 排程全链路 |
| `meituan_clock_status` | `GET /api/clock/status` | 虚拟时钟状态查询 |
| `meituan_clock_forward` | `POST /api/clock/offset` | 虚拟时钟快进 N 分钟 |
| `meituan_clock_jump` | `POST /api/clock/jump` | 虚拟时钟跳转到指定时刻 |
| `meituan_clock_events` | `GET /api/clock/events` | 获取时钟推进触发的事件 |
| `meituan_preference_read` | `GET /api/profile/get` | 读取管家偏好谱 |
| `meituan_preference_update` | `POST /api/profile/set` | 更新管家偏好谱 |
| `meituan_reminder_add` | `POST /api/reminder/add_task` | 添加喝水/吃药提醒 |
| `meituan_reminder_remove` | `POST /api/reminder/remove_task` | 删除提醒任务 |
| `meituan_anomaly_inject` | `POST /api/anomaly/inject` | 动态注入异常事件 |
| `meituan_pitfall_check` | `POST /api/pitfall/check` | 防踩坑全链路检查 |
| `meituan_route_plan` | `POST /api/route/plan` | 多节点路径规划 |
| `meituan_queue_monitor` | `POST /api/queue/:action` | 排队监控（排号/查询/轮询） |
| `meituan_weather` | `POST /api/weather` | 天气查询 |

---

## 🧠 技术决策记录

1. **不重写为纯 TypeScript 插件**：6 个核心 Python Skill 共 ~2500 行业务逻辑，HTTP Bridge 方案保留全部现有代码，仅创建薄层 TS 桥接插件（详见 `REPAIR_PLAN.md`）
2. **虚拟时钟 vs 真实时间分离**：SSE 提醒轮询线程使用系统真实时间，不依赖虚拟时钟的运行状态，确保真实世界的喝水/吃药提醒始终生效
3. **偏好谱用 Markdown 表格**：人类可读 + LLM 可解析，通过正则按 `## 口味/通勤/预算/健康作息` 分节解析，无外部数据库依赖
4. **Mock 数据封闭**：所有商户、天气、排队数据均为本地 Mock，不调用任何真实三方 API，确保 Demo 可复现
5. **gunicorn 多 worker**：`start.sh` 使用 `gunicorn -w 4` 启动，支持并发请求；SSE 客户端列表和提醒锁在进程内共享

---

## 📋 开发历程

```
d81e7c1  交付版
004763c  终版5.0
4f0111d  终版4.0
12ee051  终版3.2
...
5f633d8  管家长期记忆1.1 — 偏好注入全覆盖
b85bd00  管家长期记忆1.0 — 偏好谱读写引擎 + API + 搜索注入
```

详细修复计划见 `REPAIR_PLAN.md`（Phase 1-4 全覆盖）。
