#!/usr/bin/env bash
# =============================================================================
# OpenAPI 接口查询入口脚本
# 功能：智能调用 business_interface_query.py，支持 Windows/Mac/Linux
# 用法：bash run_query.sh --all-interface-desc "xxx" [其他参数]
# =============================================================================
set -eo pipefail

# 获取脚本自身路径（支持相对/绝对路径调用）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 调用 pip_install.sh run 执行主脚本
exec "${SCRIPT_DIR}/pip_install.sh" run business_interface_query.py --config "${SKILL_ROOT}/config.yaml" "$@"
