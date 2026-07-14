#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "📦 安装 Python 依赖..."
pip3 install -r requirements.txt

echo ""
echo "🔌 构建 meituan-bridge 插件..."
cd skills/meituan-bridge
npm install
npm run build
cd ../..

echo ""
echo "============================================"
echo "  ✅ 配置完成！"
echo ""
echo "  启动服务: ./start.sh"
echo "  然后浏览器打开 index.html 即可使用"
echo "============================================"
