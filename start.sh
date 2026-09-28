#!/bin/sh
# 启动 agent-bridge 管理界面
cd "$(dirname "$0")" && exec python3 -m agent_bridge "$@"
