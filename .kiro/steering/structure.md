# Project Structure

## Organization Philosophy

**功能域分层 + 技能插件化**。顶层是入口层（main.py/server.py/index.html），核心业务逻辑全部封装在 `skills/` 目录下的独立技能模块中。每个 Skill 是一个自包含的 Python 包，有明确的单一职责和对外契约。

## Directory Patterns

### 入口层
**Location**: `/` (根目录)
**Purpose**: 应用入口、配置、文档
**Example**: `server.py` (Flask HTTP 桥 + SSE + 偏好引擎), `main.py` (排程中枢 + LLM 对话引擎), `index.html` (沙盒控制台)

### Skill 模块
**Location**: `/skills/<skill_name>/`
**Purpose**: 自包含的业务能力模块，每个 Skill 解决一个垂直领域问题
**Example**: `skills/generic_poi_searcher/` — 商户搜索；`skills/concurrent_pipeline_scheduler/` — 排程引擎

### 桥接插件
**Location**: `/skills/meituan-bridge/`
**Purpose**: OpenClaw TypeScript 插件，注册 14 个 Agent Tool 并通过 HTTP Bridge 桥接到 Python 后端
**Example**: `skills/meituan-bridge/src/index.ts` — `defineToolPlugin` 实现

### 管家记忆
**Location**: `/管家记忆.md`
**Purpose**: 用户长期偏好谱数据，Markdown 表格格式，系统自动读写
**Example**: 四维度字段：口味/通勤/预算/健康作息

### 治理文档
**Location**: `/*.md` (根目录)
**Purpose**: 项目级规范文档
**Example**: `管家SOUL.md` (角色设定), `REPAIR_PLAN.md` (修复计划), `能力边界.md` (能力边界分析)

### 配置与 Schema
**Location**: `/schemas/`
**Purpose**: JSON Schema 定义，用于提醒系统的输入/输出校验
**Example**: `reminder_input.schema.json`, `reminder_output.schema.json`

### 开发日志
**Location**: `/memory/`
**Purpose**: 按日期的开发记录
**Example**: `2026-06-05.md`

## Naming Conventions

- **Python 文件**: snake_case (`generic_poi_searcher.py`, `task_reminder_skill.py`)
- **Skill 目录**: snake_case (`anomaly_sensor_skill/`, `route_planner/`)
- **TypeScript 文件**: kebab-case 或标准小写 (`index.ts`, `openclaw.plugin.json`)
- **函数**: snake_case (`search_poi_matrix`, `solve_concurrent_timeline`, `plan_route`)
- **API 路由**: `/api/<domain>/<action>` (`/api/clock/status`, `/api/reminder/add_task`)
- **Markdown 文档**: 中文语义化命名 (`管家SOUL.md`, `管家记忆.md`, `能力边界.md`)

## Import Organization

```python
# 标准库优先
import json, os, sys, re
from datetime import datetime

# 第三方库
from dotenv import load_dotenv
from flask import Flask, request, jsonify

# 本地 Skill 模块（通过 sys.path 动态追加）
from skills.time_master import time_master as skill_time
from skills.generic_poi_searcher import generic_poi_searcher as skill_poi
```

**Path Setup**:
```python
base_dir = os.path.dirname(os.path.abspath(__file__))
skills_path = os.path.join(base_dir, "skills")
sys.path.insert(0, base_dir)
sys.path.append(skills_path)
```

## Code Organization Principles

1. **Skill 自治**：每个 Skill 模块自包含，有明确的入口函数名（如 `handle()`, `plan_route()`, `search_poi_matrix()`）
2. **入口薄层**：`server.py` 只做路由分发和 SSE 管理，业务逻辑全部委托给 Skill
3. **统一定义在顶部**：常量映射表（`CATEGORY_MAP`, `CATEGORY_NAME_CN`）集中定义在 `main.py` 顶部
4. **双轨共享**：`main.py` 的静态排程和沙盒仿真共享同一套 Skill 和 LLM 调用入口
5. **Mock 封闭**：所有数据源在 Skill 内部用字典/列表硬编码，不发起外部 HTTP 请求
6. **文档在同级**：项目级规范文档放根目录，Skill 的技术文档放 Skill 目录内（如有）
