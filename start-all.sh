#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "============================================"
echo "  美团AI时空管家 — 一键启动"
echo "============================================"

# ---- 启动 Flask 后端 ----
echo ""
echo "🌐 启动 Python Flask 后端 (port 5000)..."
./start.sh

# ---- 启动 OpenClaw Gateway ----
echo ""
echo "💬 启动 OpenClaw Gateway (port 18789)..."
openclaw gateway --port 18789 --bind loopback &

sleep 3

echo ""
echo "============================================"
echo "  ✅ 全部服务已启动！"
echo ""
echo "  🌐 Web 控制台:  http://localhost:5000"
echo "  💬 OpenClaw:    http://localhost:18789"
echo ""
echo "  停止服务:"
echo "    lsof -ti :5000 | xargs kill"
echo "    lsof -ti :18789 | xargs kill"
echo "============================================"
