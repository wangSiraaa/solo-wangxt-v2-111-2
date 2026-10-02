#!/usr/bin/env bash
# 写入/重置虚构演示数据（原料、多版化验单、湿基化验单、缺测/零分母演示料）。
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=. python3 -m app.seed
