#!/bin/bash
# pip_install.sh - 事件订阅查询脚本运行器
# 使用虚拟环境隔离依赖

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# 创建虚拟环境（如果不存在）
if [ ! -d "$VENV_DIR" ]; then
    echo "创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
fi

# 激活虚拟环境并安装依赖
source "$VENV_DIR/bin/activate"
pip install -q requests

# 运行脚本
python3 "$SCRIPT_DIR/event_query.py" "$@"
