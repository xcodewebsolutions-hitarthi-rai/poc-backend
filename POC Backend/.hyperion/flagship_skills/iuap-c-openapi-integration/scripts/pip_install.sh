#!/usr/bin/env bash
# =============================================================================
# 智能环境安装脚本
# 功能：管理虚拟环境 + 智能路径解析（跨平台：Windows/Mac/Linux）
# =============================================================================
set -eo pipefail

# -----------------------------------------------------------------------------
# 基础路径定义
# -----------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
REQS_FILE="${SCRIPT_DIR}/requirements.txt"
SENTINEL="${VENV_DIR}/.deps_installed"
# SKILL_ROOT: skill 目录（pip_install.sh 的父目录）
SKILL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# ORIGINAL_CWD: 用户调用时的当前工作目录
ORIGINAL_CWD="${PWD:-$(pwd)}"

# 需要解析相对路径的参数（白名单）
PATH_PARAMS=("--config" "--output" "--input" "--file" "--dir")

# 全局变量（用于 run 模式）
script_path=""
candidate=""
resolved_args=()
is_path_param=0
resolved_path=""

# -----------------------------------------------------------------------------
# 跨平台路径检测
# -----------------------------------------------------------------------------
is_absolute_path() {
    local path="$1"
    # Unix/Linux/Mac: 以 / 开头
    [[ "$path" == /* ]] && return 0
    # Windows: 以盘符开头 D:/ 或 D:\ 或 UNC 路径 //server/share
    [[ "$path" =~ ^[A-Za-z]:[/\\] ]] && return 0
    [[ "$path" == //* ]] && return 0
    return 1
}

# -----------------------------------------------------------------------------
# 核心函数：智能路径解析
# -----------------------------------------------------------------------------
# resolve_path <relative_or_absolute_path>
# 解析逻辑（按优先级）：
#   1. 绝对路径 → 直接返回
#   2. ../ 开头 → 基于 SKILL_ROOT 解析（用户期望从 skill 目录引用文件）
#   3. 普通相对路径 → 多级查找，找到第一个存在的文件
#   4. 都找不到 → 尝试基于 ORIGINAL_CWD（默认行为，兼容旧逻辑）
resolve_path() {
    local input_path="$1"
    local search_paths=()

    # 情况1：绝对路径直接返回
    if is_absolute_path "$input_path"; then
        echo "$input_path"
        return 0
    fi

    # 情况2：../ 开头 → 基于 SKILL_ROOT 解析
    if [[ "$input_path" == ../* ]]; then
        local relative="${input_path#../}"  # 去掉 ../ 前缀
        local candidate="${SKILL_ROOT}/${relative}"
        if [[ -f "$candidate" ]] || [[ -d "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
        # 文件不存在，但路径逻辑正确，使用该路径
        echo "$candidate"
        return 0
    fi

    # 情况3：普通相对路径 → 多级查找
    search_paths=(
        "${ORIGINAL_CWD}/${input_path}"      # 基于用户调用目录
        "${SCRIPT_DIR}/${input_path}"         # 基于 scripts 目录
        "${SKILL_ROOT}/${input_path}"         # 基于 skill 目录
    )

    for path in "${search_paths[@]}"; do
        if [[ -f "$path" ]] || [[ -d "$path" ]]; then
            echo "$path"
            return 0
        fi
    done

    # 情况4：回退到 ORIGINAL_CWD（默认行为）
    echo "${ORIGINAL_CWD}/${input_path}"
    return 0
}

# -----------------------------------------------------------------------------
# 虚拟环境管理
# -----------------------------------------------------------------------------
sync_venv_executables() {
    if [[ -x "${VENV_DIR}/bin/python" ]]; then
        VENV_PY="${VENV_DIR}/bin/python"
        VENV_PIP="${VENV_DIR}/bin/pip"
    elif [[ -x "${VENV_DIR}/Scripts/python.exe" ]]; then
        VENV_PY="${VENV_DIR}/Scripts/python.exe"
        VENV_PIP="${VENV_DIR}/Scripts/pip.exe"
    else
        VENV_PY="${VENV_DIR}/bin/python"
        VENV_PIP="${VENV_DIR}/bin/pip"
    fi
}
sync_venv_executables

ensure_venv() {
    # 快速路径：标记文件存在且比 requirements 文件新 → 跳过
    if [[ -f "${SENTINEL}" ]] && [[ "${SENTINEL}" -nt "${REQS_FILE}" ]]; then
        return 0
    fi

    # 虚拟环境不存在 → 创建并安装
    if [[ ! -x "${VENV_PY}" ]]; then
        echo "[INFO] Creating virtual environment..."
        python3 -m venv "${VENV_DIR}"
        sync_venv_executables
        echo "[INFO] Installing dependencies..."
        "${VENV_PIP}" install --quiet -r "${REQS_FILE}"
        touch "${SENTINEL}"
        return 0
    fi

    # 虚拟环境存在但标记缺失或过期 → 仅安装依赖
    echo "[INFO] Installing missing dependencies..."
    "${VENV_PIP}" install --quiet -r "${REQS_FILE}"
    touch "${SENTINEL}"
}

# -----------------------------------------------------------------------------
# 主入口：run 模式
# -----------------------------------------------------------------------------
if [[ "${1:-}" == "run" ]] && [[ $# -ge 2 ]]; then
    ensure_venv
    shift  # 移除 run

    # 解析脚本路径
    script_path="$1"
    if ! is_absolute_path "$script_path"; then
        for dir in "$ORIGINAL_CWD" "$SCRIPT_DIR" "$SKILL_ROOT"; do
            candidate="${dir}/${script_path}"
            if [[ -f "$candidate" ]]; then
                script_path="$candidate"
                break
            fi
        done
    fi

    shift  # 移除脚本名

    # 解析所有路径参数
    resolved_args=()
    while [[ $# -gt 0 ]]; do
        is_path_param=0
        for param in "${PATH_PARAMS[@]}"; do
            if [[ "$1" == "$param" ]]; then
                is_path_param=1
                break
            fi
        done

        if [[ $is_path_param -eq 1 ]] && [[ $# -ge 2 ]]; then
            resolved_path="$(resolve_path "$2")"
            resolved_args+=("$1" "$resolved_path")
            shift 2
        else
            resolved_args+=("$1")
            shift
        fi
    done

    sync_venv_executables
    exec "${VENV_PY}" "${script_path}" "${resolved_args[@]}"
else
    ensure_venv
    echo "[INFO] Environment ready. Activate with: source ${VENV_DIR}/bin/activate"
    echo "[INFO] Or run scripts directly: ./pip_install.sh run <script.py> [args...]"
fi
