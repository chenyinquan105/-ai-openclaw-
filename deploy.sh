#!/bin/bash
# ============================================================
# 美团AI时空管家 — 阿里云 ECS 一键部署脚本
# 适用系统: Ubuntu 20.04/22.04/24.04
# 使用方法:
#   1. 将本脚本上传到服务器: scp deploy.sh root@<你的IP>:~/
#   2. SSH 登录: ssh root@<你的IP>
#   3. 运行: chmod +x deploy.sh && ./deploy.sh
# ============================================================
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
REPO_URL="https://github.com/chenyinquan105/-ai-openclaw-.git"
APP_DIR="/opt/meituan-butler"
PORT=5000

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  美团AI时空管家 — 阿里云部署脚本${NC}"
echo -e "${GREEN}========================================${NC}"

# ---- 1. 安装系统依赖 ----
echo -e "${YELLOW}[1/6] 安装系统依赖...${NC}"
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git nginx curl

# ---- 2. 克隆项目 ----
echo -e "${YELLOW}[2/6] 克隆项目...${NC}"
if [ -d "$APP_DIR" ]; then
    cd "$APP_DIR" && git pull
else
    git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

# ---- 3. 配置 .env ----
echo -e "${YELLOW}[3/6] 配置环境变量...${NC}"
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "已从 .env.example 创建 .env，请编辑填入真实的 API Key:"
        echo "  vim $APP_DIR/.env"
    else
        cat > .env << 'EOF'
DEEPSEEK_API_KEY=你的DeepSeek_API_Key
AMAP_API_KEY=你的高德地图_API_Key
FAST_LLM_MODEL=deepseek-v4-pro
USE_PRE_CACHED=true
EOF
    fi
    echo -e "${YELLOW}⚠️  请编辑 .env 填入真实 API Key 后重新运行本脚本${NC}"
    echo "  vim $APP_DIR/.env"
    exit 1
fi

# ---- 4. 安装 Python 依赖 ----
echo -e "${YELLOW}[4/6] 安装 Python 依赖...${NC}"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# ---- 5. 配置 systemd 服务（开机自启 + 后台运行） ----
echo -e "${YELLOW}[5/6] 配置 systemd 服务...${NC}"
cat > /etc/systemd/system/meituan-butler.service << EOF
[Unit]
Description=美团AI时空管家
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn -w 1 --threads 4 -b 0.0.0.0:$PORT --timeout 120 server:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable meituan-butler
systemctl restart meituan-butler

# ---- 6. 检查状态 ----
echo -e "${YELLOW}[6/6] 检查服务状态...${NC}"
sleep 2
systemctl status meituan-butler --no-pager || true
curl -s "http://localhost:$PORT/api/clock/status" | head -c 200 || echo "API 还未响应，请稍等几秒..."

PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "未知")
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  ✅ 部署完成！${NC}"
echo -e "${GREEN}  访问地址: http://${PUBLIC_IP}:${PORT}${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "常用命令:"
echo "  查看状态: systemctl status meituan-butler"
echo "  重启服务: systemctl restart meituan-butler"
echo "  查看日志: journalctl -u meituan-butler -f"
