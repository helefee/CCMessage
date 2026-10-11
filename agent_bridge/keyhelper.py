# -*- coding: utf-8 -*-
"""源码版的取 key 入口：python <这个文件> key get NAME。和当前目录无关（Claude / Codex 在项目目录里跑它）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_bridge import keystore  # noqa: E402

if __name__ == "__main__":
    sys.exit(keystore.main(sys.argv[2:] if sys.argv[1:2] == ["key"] else ["get", *sys.argv[1:2]]))
