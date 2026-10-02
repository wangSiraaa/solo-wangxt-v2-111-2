#!/usr/bin/env bash
# 前端开发服务器（/api 代理到 127.0.0.1:8000）。
set -euo pipefail
cd "$(dirname "$0")/.."
./node_modules/.bin/ng serve --proxy-config proxy.conf.json --host 127.0.0.1 --port 4200 "$@"
