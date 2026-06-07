#!/bin/bash
cd "/Users/kaijimima1234/项目/美团ai openclaw版本"
lsof -ti :5000 | xargs kill -9 2>/dev/null
sleep 1
gunicorn -w 4 -b 0.0.0.0:5000 --timeout 120 server:app 2>&1 &
sleep 2
echo "✅ 服务已启动: http://127.0.0.1:5000"
