#!/bin/sh
# 启动 agent-bridge（Linux / macOS）：有 install.sh 建的 .venv 就用它，没有就用系统 python3。
#   ./run.sh                     浏览器打开 http://127.0.0.1:8765/
#   ./run.sh --no-browser        只起服务（服务器 / systemd 用）
#   ./run.sh --host 0.0.0.0      另开一个只认配对设备的局域网监听（管理界面仍只在本机）
#   ./run.sh desktop             桌面窗口（要先 ./install.sh --desktop）
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "找不到 python3：先装 Python 3.11+（如 sudo apt install python3 python3-venv）" >&2; exit 1; }
cd "$DIR" && exec "$PY" -m agent_bridge "$@"
