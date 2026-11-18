#!/usr/bin/env bash
# 一键自测（本地无 Docker 环境也可运行）：
#   1) Lua 熔断状态机确定性单测（7 项，含迟到失败、租约过期、持久化模拟）
#   2) 端到端 HTTP 测试（5 项，真实 uvicorn 进程 + 真实 Redis）
#
# 依赖：本机 Redis（REDIS_URL，默认 redis://127.0.0.1:6379/0）与项目 venv
# Docker Compose 环境下的等价验证命令见 README.md「自测」一节。
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-$HOME/venv/bin/python}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"

echo "================ 1/2 Lua 状态机单测 ================"
"$PY" -m pytest tests/test_breaker_lua.py -v

echo "================ 2/2 端到端 HTTP 测试 ================"
"$PY" -m pytest tests/test_e2e_http.py -v

echo
echo "全部自测通过。"
