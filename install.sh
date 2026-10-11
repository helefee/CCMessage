#!/usr/bin/env bash
# agent-bridge 在 Linux 上的安装：建 .venv、装依赖，可选装成 systemd 用户服务（开机 / 登录后自动跑）。
#
#   ./install.sh                       建 .venv，装 qrcode（手机扫码配对用）
#   ./install.sh --service             另外装 systemd --user 服务 agent-bridge.service 并启动
#   ./install.sh --service --host 0.0.0.0
#                                      服务另开局域网监听（只认配对过的设备；管理界面始终只在 127.0.0.1）
#   ./install.sh --port 8765           端口（缺省 8765）
#   ./install.sh --desktop             另外装 pywebview（桌面窗口模式；还要系统的 GTK WebKit，见 README）
#   ./install.sh --uninstall-service   停掉并删掉 systemd 服务（.venv 和数据不动）
#
# 不需要 root；数据在 ~/.agent-bridge/。会话的钩子用系统的 python3，不用 .venv 里的。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
UNIT_NAME="agent-bridge.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE=0; DESKTOP=0; UNINSTALL=0; HOST=""; PORT=8765

while [ $# -gt 0 ]; do
  case "$1" in
    --service) SERVICE=1 ;;
    --desktop) DESKTOP=1 ;;
    --uninstall-service) UNINSTALL=1 ;;
    --host) HOST="${2:?--host 后面要跟地址}"; shift ;;
    --host=*) HOST="${1#*=}" ;;
    --port) PORT="${2:?--port 后面要跟端口}"; shift ;;
    --port=*) PORT="${1#*=}" ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "不认识的参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

if [ "$UNINSTALL" = 1 ]; then
  systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT_NAME"
  systemctl --user daemon-reload 2>/dev/null || true
  say "已删掉 systemd 用户服务 $UNIT_NAME"
  exit 0
fi

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "找不到 python3：先装 Python 3.11+（Debian / Ubuntu：sudo apt install python3 python3-venv）" >&2; exit 1; }
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  for c in python3.13 python3.12 python3.11; do
    command -v "$c" >/dev/null && { PY="$(command -v "$c")"; break; }
  done
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "Python 太旧（$("$PY" -V 2>&1)），要 3.11 或更新（Ubuntu 22.04：sudo apt install python3.11 python3.11-venv）" >&2; exit 1; }
fi
if [ ! -x "$VENV/bin/python" ]; then
  say "建虚拟环境 $VENV"
  "$PY" -m venv "$VENV" 2>/dev/null || {
    echo "建 venv 失败：Debian / Ubuntu 要先 sudo apt install python3-venv" >&2; exit 1; }
fi
say "装依赖"
"$VENV/bin/python" -m pip install --disable-pip-version-check -q --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/python" -m pip install --disable-pip-version-check -q "qrcode>=7" "tomlkit>=0.12"
if [ "$DESKTOP" = 1 ]; then
  say "装 pywebview（桌面窗口模式）"
  "$VENV/bin/python" -m pip install --disable-pip-version-check -q "pywebview>=5,<7" || true
  "$VENV/bin/python" -c 'import gi' 2>/dev/null || cat <<'TIP'
  桌面窗口还要系统的 GTK + WebKit 绑定，venv 里装不了，用系统包装好后重建 venv 时带上系统包：
    Debian / Ubuntu：sudo apt install python3-gi gir1.2-webkit2-4.1
    Fedora：         sudo dnf install python3-gobject webkit2gtk4.1
  然后：rm -rf .venv && python3 -m venv --system-site-packages .venv && ./install.sh --desktop
  不装也没关系：浏览器打开 http://127.0.0.1:PORT/ 功能一样。
TIP
fi
chmod +x "$DIR/run.sh" "$DIR/start.sh" 2>/dev/null || true

say "检查环境"
"$VENV/bin/python" -m agent_bridge doctor || true

if [ "$SERVICE" = 1 ]; then
  command -v systemctl >/dev/null || { echo "没有 systemctl，装不了服务；用 ./run.sh --no-browser 自己起" >&2; exit 1; }
  mkdir -p "$UNIT_DIR"
  ARGS="--no-browser --port $PORT"
  [ -n "$HOST" ] && ARGS="$ARGS --host $HOST"
  cat > "$UNIT_DIR/$UNIT_NAME" <<UNIT
[Unit]
Description=agent-bridge（Claude / Codex 会话消息总线管理界面）
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
ExecStart=$DIR/run.sh $ARGS
Restart=on-failure
RestartSec=3
Environment=PYTHONUTF8=1

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now "$UNIT_NAME"
  say "systemd 用户服务已装好并启动：systemctl --user status $UNIT_NAME"
  say "日志：journalctl --user -u $UNIT_NAME -f"
  say "想不登录也一直跑：sudo loginctl enable-linger $USER"
fi

say "装好了。启动：$DIR/run.sh   （打开 http://127.0.0.1:$PORT/）"
[ -n "$HOST" ] && say "局域网设备配对：$DIR/run.sh pair --port $PORT   （出一个一次性链接 / 二维码）"
exit 0
