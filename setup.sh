#!/bin/bash
set -e
cd "$(dirname "$0")"

# ===================================================================
# 美团AI时空管家 — 一键配置脚本
# ===================================================================

OPENCLAW_MIN_VERSION="2026.5.17"
NODE_MIN_MAJOR=22
NODE_MIN_MINOR=19

echo "============================================"
echo "  美团AI时空管家 — 环境配置"
echo "============================================"

# ---- Python ----
echo ""
echo "📦 [1/4] 安装 Python 依赖..."
pip3 install -r requirements.txt
echo "   ✅ Python 依赖就绪"

# ---- Node.js ----
echo ""
echo "🔧 [2/4] 检查 Node.js..."
if ! command -v node &>/dev/null; then
    echo "   ❌ 未检测到 Node.js（需要 >= ${NODE_MIN_MAJOR}.${NODE_MIN_MINOR}）"
    echo "   请先安装 Node.js: https://nodejs.org/"
    echo "   安装后重新运行: ./setup.sh"
    exit 1
fi

NODE_VERSION=$(node -v | sed 's/v//')
NODE_MAJOR=$(echo "$NODE_VERSION" | cut -d. -f1)
NODE_MINOR=$(echo "$NODE_VERSION" | cut -d. -f2)

if [ "$NODE_MAJOR" -lt "$NODE_MIN_MAJOR" ] || { [ "$NODE_MAJOR" -eq "$NODE_MIN_MAJOR" ] && [ "$NODE_MINOR" -lt "$NODE_MIN_MINOR" ]; }; then
    echo "   ❌ Node.js $NODE_VERSION 过低，需要 >= ${NODE_MIN_MAJOR}.${NODE_MIN_MINOR}"
    echo "   请升级 Node.js: https://nodejs.org/"
    exit 1
fi
echo "   ✅ Node.js $NODE_VERSION"

# ---- OpenClaw Gateway ----
echo ""
echo "🚀 [3/4] 配置 OpenClaw Gateway..."
if ! command -v openclaw &>/dev/null; then
    echo "   📥 安装 OpenClaw Gateway..."
    npm install -g openclaw
    echo "   ✅ OpenClaw 安装完成"
else
    OPENCLAW_VER=$(openclaw --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    echo "   ✅ OpenClaw $OPENCLAW_VER 已安装"
fi

# 恢复 OpenClaw 配置
OPENCLAW_CONFIG_DIR="$HOME/.openclaw"
mkdir -p "$OPENCLAW_CONFIG_DIR"

if [ -f "openclaw-config.template.json" ]; then
    echo "   📝 恢复 OpenClaw 配置..."
    sed "s|__OPENCLAW_HOME__|$OPENCLAW_CONFIG_DIR|g" openclaw-config.template.json > "$OPENCLAW_CONFIG_DIR/openclaw.json"
    echo "   ✅ 配置已写入 ~/.openclaw/openclaw.json"
else
    echo "   ⚠️  未找到 openclaw-config.template.json，跳过配置恢复"
fi

# ---- meituan-bridge 插件 ----
echo ""
echo "🔌 [4/4] 构建并注册 meituan-bridge 插件..."
cd skills/meituan-bridge
npm install
npm run build

# 安装插件到 OpenClaw
TARBALL="meituan-bridge-1.0.0.tgz"
if [ -f "$TARBALL" ]; then
    openclaw plugins install "$TARBALL" 2>/dev/null || echo "   ⚠️  插件注册失败（可能已注册），请手动执行: openclaw plugins install skills/meituan-bridge/$TARBALL"
    echo "   ✅ meituan-bridge 插件已注册"
else
    echo "   ⚠️  未找到 $TARBALL，请手动打包: cd skills/meituan-bridge && npm pack"
fi
cd ../..

# ---- 完成 ----
echo ""
echo "============================================"
echo "  ✅ 全部配置完成！"
echo ""
echo "  🚀 一键启动所有服务:"
echo "     chmod +x start-all.sh && ./start-all.sh"
echo ""
echo "  🌐 或仅启动 Web 控制台:"
echo "     chmod +x start.sh && ./start.sh"
echo "     浏览器打开 http://localhost:5000"
echo ""
echo "  💬 仅启动 OpenClaw Gateway:"
echo "     openclaw gateway --port 18789 --bind loopback"
echo "============================================"
