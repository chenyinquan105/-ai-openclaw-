#!/bin/bash
cd "$(dirname "$0")"
lsof -ti :5000 | xargs kill -9 2>/dev/null
sleep 1

# 1 worker + 4 threads: 共享 session_state（线程安全由 GIL 保证），同时处理 SSE 长连接 + 普通请求
gunicorn -w 1 \
  --threads 4 \
  -b 0.0.0.0:5000 \
  --timeout 120 \
  --capture-output \
  --max-requests 500 \
  --max-requests-jitter 50 \
  --error-logfile gunicorn_error.log \
  --access-logfile gunicorn_access.log \
  server:app 2>&1 &

sleep 2
echo "✅ 服务已启动: http://localhost:5000"
