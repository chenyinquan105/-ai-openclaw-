# Design

## Overview
两处小修复：弹窗防重入 + 时钟跳转保持运行状态。

## Fix 1: 弹窗防重入锁

**根因**: `_pollAndHandleEvents`（直接 fetch）+ patcher 链（clockFetch 拦截）双重投递同一事件，`renderReminderDialog` 的防频闪检查依赖 `display === 'flex'`，但第一次调用时弹窗尚未完成渲染。

**修复**: 在 `renderReminderDialog` 中添加时间戳锁 `_lastMedDialogOpenTs`，同一 medId 500ms 内忽略重复调用。

**修改文件**: `index.html` — `renderReminderDialog` 函数（line 3392）

## Fix 2: 时钟跳转保持运行

**根因**: 
- 后端 `/api/clock/jump` 未恢复 auto_tick
- 前端 `clockSliderChange` + `clockSkip` 跳转后未检查并恢复运行状态

**修复**:
- 后端 `clock_jump`: 跳转前记录 `is_running`，跳转后若之前运行中则自动重启
- 前端 `clockSliderChange` + `clockSkip`: 跳转后若时钟电源开启且之前运行中，自动调用 start

**修改文件**: `server.py` — `clock_jump` (line 2032), `index.html` — `clockSliderChange` (line 2617), `clockSkip` (line 2762)

## File Structure Plan
| 文件 | 改动 | 行数 |
|------|------|------|
| `index.html` | renderReminderDialog 防重入 + clockSliderChange/clockSkip 恢复走时 | ~15行 |
| `server.py` | clock_jump 保持运行状态 | ~5行 |
