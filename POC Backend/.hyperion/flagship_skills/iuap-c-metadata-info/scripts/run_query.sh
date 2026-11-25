#!/usr/bin/env bash
#===============================================================================
# 跨平台元数据查询执行脚本
# 自动处理 Windows (Git Bash/WSL) 和 Linux/Mac 的路径差异
#===============================================================================

set -e

# 最大回溯层数
MAX_DEPTH=10

# 策略1: 从脚本自身位置向上找 .claude（适用于任意 CWD）
find_scripts_dir_from_self() {
    local script_path="${BASH_SOURCE[0]}"
    if [[ "$script_path" != /* ]]; then
        script_path="$(cd "$(dirname "$script_path")" && pwd)/$(basename "$script_path")"
    fi
    local dir="$(dirname "$script_path")"
    local depth=0

    while [[ $depth -lt $MAX_DEPTH ]]; do
        if [[ -d "$dir/.hyperion" ]]; then
            for folder in skills flagship_skills premium_skills; do
                for skill_scripts in "$dir/.hyperion/$folder"/*/scripts; do
                    if [[ -f "$skill_scripts/run_query.sh" ]]; then
                        echo "$skill_scripts"
                        return 0
                    fi
                done
            done
        fi
        local parent="$(dirname "$dir")"
        if [[ "$parent" == "$dir" ]]; then
            break
        fi
        dir="$parent"
        ((depth++))
    done
    return 1
}

# 策略2: 从 CWD 向上找（适用于从项目目录内调用）
find_scripts_dir_from_cwd() {
    local dir="$(pwd)"
    local depth=0

    while [[ $depth -lt $MAX_DEPTH ]]; do
        if [[ -f "$dir/run_query.sh" ]]; then
            echo "$dir"
            return 0
        fi
        if [[ -d "$dir/.hyperion" ]]; then
            for folder in skills flagship_skills premium_skills; do
                for skill_scripts in "$dir/.hyperion/$folder"/*/scripts; do
                    if [[ -f "$skill_scripts/run_query.sh" ]]; then
                        echo "$skill_scripts"
                        return 0
                    fi
                done
            done
        fi
        local parent="$(dirname "$dir")"
        if [[ "$parent" == "$dir" ]]; then
            break
        fi
        dir="$parent"
        ((depth++))
    done
    return 1
}

# 获取脚本目录：优先使用当前脚本自身目录，包装脚本场景再向上搜索
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR=""
if [[ -f "$SELF_DIR/ultimate_metadata_query.py" && -f "$SELF_DIR/../config.yaml" ]]; then
    SCRIPT_DIR="$SELF_DIR"
elif ! SCRIPT_DIR=$(find_scripts_dir_from_self); then
    if ! SCRIPT_DIR=$(find_scripts_dir_from_cwd); then
        echo "错误: 无法定位 scripts 目录" >&2
        echo "当前目录: $(pwd)" >&2
        echo "请确保从包含 .hyperion/{skills,flagship_skills,premium_skills}/*/scripts/ 目录树的位置调用。" >&2
        exit 1
    fi
fi

# 切换到脚本目录（确保工作目录正确）
cd "$SCRIPT_DIR"

# config.yaml 相对于脚本的位置
CONFIG_PATH="../config.yaml"

# 显示帮助
show_help() {
    cat << EOF
用法: $(basename "$0") [选项]

选项:
  --allbillname <名称>    中文单据/元数据名称（必填）
  --query-uri <uri>       元数据实体 URI（多义时使用）
  --is-include-sub <Y/N>  是否包含子实体/子表
  --doc-fields <字段>     字段显示名过滤
  --is-sql <Y/N>         是否拉取参照结构
  --table-template <模板>  报表表头模板
  -v, --verbose           详细输出
  -h, --help              显示帮助

两步查询模式（含子表 + 多义词）：
  1) $(basename "$0") --allbillname "付款单"
     → 确认标准实体的 URI（如 earap.payment.PaymentHeader）
  2) $(basename "$0") --allbillname "付款单" --query-uri "earap.payment.PaymentHeader" --is-include-sub Y
     → 使用确认的 URI 查询完整主表+子表

示例:
  $(basename "$0") --allbillname "销售订单"
  $(basename "$0") --allbillname "销售订单" --is-include-sub Y
  $(basename "$0") --allbillname "客户档案" --query-uri "aa.merchant.Merchant"

EOF
}

# 主函数
main() {
    # 处理 help（支持 -h 和 --help）
    for arg in "$@"; do
        case "$arg" in
            -h|--help)
                show_help
                exit 0
                ;;
        esac
    done

    # 无参数时显示帮助
    if [ $# -eq 0 ]; then
        show_help
        exit 0
    fi

    # 检查 config.yaml
    if [ ! -f "$CONFIG_PATH" ]; then
        echo "错误: 配置文件不存在: $(pwd)/$CONFIG_PATH" >&2
        echo "请确保在 scripts 目录中执行此脚本" >&2
        exit 1
    fi

    # 复用 pip_install.sh 的 venv 管理（已包含路径解析 + 依赖安装 + Python 选择）
    # 内部自动切换到技能根目录执行
    # 注意: 不传 --config，让 Python 脚本使用其默认路径（基于脚本自身位置计算，跨平台正确）
    exec bash "$SCRIPT_DIR/pip_install.sh" run ultimate_metadata_query.py "$@"
}

# 运行
main "$@"
