#!/usr/bin/env bash
#===============================================================================
# 三层缓存清理脚本
# 架构：当前会话 → 全局索引 → 备份文件
# 兼容：Windows (Git Bash) / Linux / Mac
#
# 索引文件路径（环境变量 METADATA_INDEX_PATH 可覆盖）:
#   默认: ${SCRIPT_DIR}/../../../.claude/metadata-cache-index.json
#===============================================================================

# 获取脚本自身目录的绝对路径（跨平台兼容）
get_script_dir() {
    local source="${BASH_SOURCE[0]}"
    while [[ -h "$source" ]]; do
        local link target
        link=$(readlink "$source")
        target=$(cd "$(dirname "$source")" && cd "$(dirname "$link")" && pwd)
        source="$target/$(basename "$link")"
    done
    cd "$(dirname "$source")" && pwd
}

# 获取缓存目录（零配置，基于脚本位置）
# 脚本: skills/iuap-c-metadata-info/scripts/cleanup_cache.sh
# 向上3级到达项目根目录，再/cache
get_cache_dir() {
    local script_dir; script_dir=$(get_script_dir)
    # 先确保目录存在，再规范化路径
    mkdir -p "${script_dir}/../../../cache" 2>/dev/null
    (cd "${script_dir}/../../../cache" && pwd)
}

# 获取索引文件路径（环境变量优先）
get_index_path() {
    [[ -n "${METADATA_INDEX_PATH:-}" ]] && { echo "$METADATA_INDEX_PATH"; return; }
    echo "$(get_cache_dir)/metadata-cache-index.json"
}

# 获取会话ID
get_session_id() {
    # 1. 环境变量优先
    [[ -n "${CLAUDE_SESSION_ID:-}" ]] && echo "$CLAUDE_SESSION_ID" && return
    # 2. 从 sessions/*.json 获取 sessionId
    local json_file; json_file=$(ls -t "$HOME/.hyperion/sessions"/*.json 2>/dev/null | head -1)
    [[ -n "$json_file" ]] && grep -o '"sessionId":"[^"]*"' "$json_file" 2>/dev/null | head -1 | sed 's/"sessionId":"//;s/"//' && return
    # 3. 查找 cache/sessions 下的最新目录
    local cache_sessions; cache_sessions="$(get_cache_dir)/sessions"
    local dir; dir=$(ls -t -d "${cache_sessions}"/*/ 2>/dev/null | head -1)
    [[ -n "$dir" ]] && basename "$dir" && return
    # 4. 生成时间戳会话ID
    date +%Y%m%d%H%M%S
}

# 获取工具结果目录
get_tool_results_dir() {
    local cache_dir sid
    cache_dir=$(get_cache_dir)
    sid=$(get_session_id)
    echo "${cache_dir}/sessions/${sid}/tool-results"
}

# 获取 Python 解释器
get_python() { command -v python3 >/dev/null 2>&1 && echo "python3" || echo "python"; }

# Python 脚本执行器（通过环境变量传递参数）
PYTHON_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PY_INDEX_PATH PY_BILLNAME PY_URI PY_SAFE_NAME PY_CACHE_FILE PY_TIMESTAMP PY_SESSION_ID

run_python() {
    local func="$1"
    local python; python=$(get_python)
    case "$func" in
        check)
            $python "${PYTHON_SCRIPT_DIR}/cleanup_cache.py" --action check 2>/dev/null
            ;;
        precise-delete)
            $python "${PYTHON_SCRIPT_DIR}/cleanup_cache.py" --action precise-delete 2>/dev/null
            ;;
        register)
            $python "${PYTHON_SCRIPT_DIR}/cleanup_cache.py" --action register 2>/dev/null
            ;;
        list-all)
            $python "${PYTHON_SCRIPT_DIR}/cleanup_cache.py" --action list-all 2>/dev/null
            ;;
    esac
}

init_index() {
    mkdir -p "$(dirname "$(get_index_path)")"
    [[ -f "$(get_index_path)" ]] || echo '{"version":3,"global":{"index":{}},"sessions":{}}' > "$(get_index_path)"
}

# check <billname> - 三层匹配
check() {
    local billname="$1" tool_dir
    tool_dir=$(get_tool_results_dir)

    # 第一层：当前会话
    [[ -d "$tool_dir" ]] && for f in "$tool_dir"/*.txt; do
        [[ -f "$f" ]] && grep -q "billname: ${billname}" "$f" 2>/dev/null && { echo "SESSION_HIT:$(basename "$f")"; return 0; }
    done

    # 第二层：全局索引
    export PY_INDEX_PATH PY_BILLNAME
    PY_INDEX_PATH=$(get_index_path)
    PY_BILLNAME="$billname"
    run_python check
}

# precise-delete <billname> - 精准删除
precise_delete() {
    export PY_INDEX_PATH PY_BILLNAME
    PY_INDEX_PATH=$(get_index_path)
    PY_BILLNAME="$1"
    init_index
    run_python precise-delete
}

# register <billname> <uri> - 注册新缓存
register() {
    local billname="$1" uri="$2" session_id tool_dir timestamp safe_name cache_file idx_path
    session_id=$(get_session_id)
    tool_dir=$(get_tool_results_dir)
    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    idx_path=$(get_index_path)

    mkdir -p "$tool_dir"
    safe_name=$(echo "$billname" | sed 's/[^a-zA-Z0-9一-龥]/_/g')
    cache_file="${tool_dir}/${safe_name}.txt"

    export PY_INDEX_PATH PY_BILLNAME PY_URI PY_SAFE_NAME PY_CACHE_FILE PY_TIMESTAMP PY_SESSION_ID
    PY_INDEX_PATH="$idx_path"
    PY_BILLNAME="$billname"
    PY_URI="$uri"
    PY_SAFE_NAME="$safe_name"
    PY_CACHE_FILE="$cache_file"
    PY_TIMESTAMP="$timestamp"
    PY_SESSION_ID="$session_id"

    run_python register
}

# list-all - 列出全局索引
list_all() {
    export PY_INDEX_PATH
    PY_INDEX_PATH=$(get_index_path)
    init_index
    echo "=== 全局索引 ==="
    run_python list-all
}

# list-session - 列出当前会话
list_session() {
    local sid tool_dir
    sid=$(get_session_id)
    tool_dir=$(get_tool_results_dir)
    echo "=== 当前会话: $sid ==="
    echo "目录: $tool_dir"
    echo ""
    if [[ -d "$tool_dir" ]]; then
        local c=0; for f in "$tool_dir"/*.txt; do [[ -f "$f" ]] && { echo "  - $(basename "$f")"; c=$((c+1)); }; done
        [[ $c -eq 0 ]] && echo "  (空)"
    else
        echo "  (目录不存在)"
    fi
}

help() {
    cat << 'EOF'
用法: cleanup_cache.sh <命令> [参数]

命令:
  check <billname>           三层匹配检查
  precise-delete <billname> 精准删除（否定词触发）
  register <billname> <uri> 注册新缓存
  list-all                  列出全局索引
  list-session              列出当前会话缓存
  -h, --help               显示帮助
EOF
}

main() {
    [[ $# -eq 0 ]] && { help; exit 0; }
    local cmd="$1"; shift
    case "$cmd" in
        check)           [[ $# -ge 1 ]] && check "$1" ;;
        precise-delete)   [[ $# -ge 1 ]] && precise_delete "$1" ;;
        register)         [[ $# -ge 2 ]] && register "$1" "$2" ;;
        list-all)         list_all ;;
        list-session)     list_session ;;
        -h|--help)       help ;;
        *)                echo "未知命令: $cmd"; help; exit 1 ;;
    esac
}

[[ "${BASH_SOURCE[0]}" == "${0}" ]] && main "$@"
