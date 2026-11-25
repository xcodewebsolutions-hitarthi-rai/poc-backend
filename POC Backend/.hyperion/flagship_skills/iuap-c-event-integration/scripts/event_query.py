#!/usr/bin/env python3
"""
事件订阅查询脚本

功能：
1. 根据名称模糊查询事件类型列表
2. 查询事件详情并解析报文

用法：
    python event_query.py --query 员工新增
    python event_query.py --detail dac0d1ae8cad4bbdab4f1688f0819761
"""

import argparse
import json
import os
import sys
import logging
from pathlib import Path

import yaml

# 添加脚本目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from iuap_common.bip_auth import (
    get_access_token,
    http_get_json,
    http_post_json,
    invalidate_token_cache,
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """从 config.yaml 加载配置"""
    # 查找 config.yaml：从脚本目录向上查找
    script_dir = Path(__file__).parent
    config_paths = [
        script_dir / "config.yaml",
        script_dir.parent / "config.yaml",
        script_dir.parent.parent / "config.yaml",
    ]

    for config_path in config_paths:
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)

    # 未找到配置文件，返回空字典
    return {}


# 加载配置
_config = load_config()

# API 配置（优先使用环境变量，其次使用配置文件）
_api_config = _config.get("api", {})
_event_config = _config.get("event_query", {})

API_BASE_URL = os.environ.get("API_BASE_URL", _api_config.get("base_url", "https://v5devkk1.yonyoucloud.com"))

# 租户ID：环境变量 > 配置文件 > 默认值
TENANT_ID = os.environ.get(
    "YONBIP_TENANT_ID",
    _event_config.get("default_tenant_id", "ozciy868")
)

# sysId：环境变量 > 配置文件 > 默认值
SYS_ID = os.environ.get(
    "YONBIP_SYS_ID",
    _event_config.get("default_sys_id", "current_yonbip_default_sys")
)

# API 路径（从配置读取，带占位符替换）
_list_path = _event_config.get("list_path", "/iuap-api-gateway/{tenantId}/{sysId}/AW/list/event")
_detail_path = _event_config.get("detail_path", "/iuap-api-gateway/{tenantId}/{sysId}/AW/openPortal/event/eventDetail/{eventId}")


def query_event_types(event_name: str = None, token: str = None) -> list:
    """
    根据名称查询事件类型列表

    Args:
        event_name: 事件名称（支持模糊查询）
        token: access_token

    Returns:
        事件类型列表
    """
    path = _list_path.format(tenantId=TENANT_ID, sysId=SYS_ID)
    body = {}
    if event_name:
        body["eventName"] = event_name

    result = http_post_json(API_BASE_URL, path, token, body)

    # 检查响应码
    if result.get("code") != "200":
        logger.warning("查询失败: %s - %s", result.get("code"), result.get("message"))

    return result.get("data", [])


def get_event_detail(event_id: str, token: str = None) -> dict:
    """
    查询事件详情

    Args:
        event_id: 事件ID
        token: access_token

    Returns:
        事件详情
    """
    path = _detail_path.format(tenantId=TENANT_ID, sysId=SYS_ID, eventId=event_id)
    result = http_get_json(API_BASE_URL, path, token, {"isAjax": "1"})

    if result.get("code") != "200" and not result.get("success"):
        logger.warning("查询失败: %s - %s", result.get("code"), result.get("message"))

    return result.get("data", {})


def build_message_from_paramlist(data: dict) -> dict:
    """
    构建（还原）完整的报文 JSON。

    规则（与 parse_event_data 统一，example 优先 + paramList 兜底）：
    1. 如果 example 有值且是有效的 JSON 对象或数组，返回 example 内容
    2. 否则（example 为空 / 非法 JSON / 标量）根据 paramList 递归构建报文结构
       - 有子节点且 paramType 为 array/list 时构建为数组 [{...}]
       - 有子节点且 paramType 为其他类型时构建为对象 {...}
       - 叶子节点按 paramType 解析 example 示例值
    """
    # 规则1: example 有值，尝试解析（必须是有效的 JSON 对象或数组）
    example = data.get("example")
    if example:
        try:
            parsed_example = json.loads(example)
            if isinstance(parsed_example, (dict, list)):
                return parsed_example
        except (json.JSONDecodeError, TypeError):
            pass  # example 非法或为标量 → 走规则2 兜底

    # 规则2: 根据 paramList 递归构建报文
    param_list = data.get("paramList", [])
    if param_list:
        return _build_message_from_items(param_list)
    return {}


def _build_message_from_items(items: list) -> dict:
    """递归构建报文结构"""
    result = {}

    for item in items:
        if not item or not isinstance(item, dict):
            continue

        name = item.get("name", "")
        if not name:
            continue

        param_type = item.get("paramType", "")
        children = item.get("children")
        example = item.get("example", "")

        # 如果有子节点（children 中存在 dict 类型元素），递归构建
        # 注：BIP API 叶子节点的 children 常为 [""]（含空串的列表），
        #     需过滤掉这类无效元素，避免被误判为"有子节点"而构建成 {}
        if children and isinstance(children, list) and any(isinstance(c, dict) for c in children):
            child_result = _build_message_from_items(children)

            # 判断是否是数组类型：仅按 paramType=array/list 判断，不依据 example 兜底
            if param_type and param_type.lower() in ("array", "list"):
                result[name] = [child_result]
            else:
                result[name] = child_result
        else:
            # 叶子节点，解析示例值
            result[name] = _parse_example_value(example, param_type)

    return result


def _parse_example_value(example: str, param_type: str) -> any:
    """根据示例值和类型解析为合适的 Python 对象"""
    if not example:
        return None

    # 清理各种引号 - 使用字节方式确保正确处理中文引号
    example = example.strip()
    # 逐字符检查并清理配对的引号
    if len(example) >= 2:
        first_char = example[0]
        last_char = example[-1]
        # 检查是否是配对的引号
        pairs = [('"', '"'), ("'", "'"), (chr(0x201C), chr(0x201D)), (chr(0x2018), chr(0x2019))]
        for left, right in pairs:
            if first_char == left and last_char == right:
                example = example[1:-1]
                break

    param_type_lower = param_type.lower() if param_type else ""

    try:
        if param_type_lower in ("int", "integer", "long"):
            return int(example)
        elif param_type_lower in ("decimal", "bigdecimal", "float", "double"):
            return float(example)
        elif param_type_lower in ("boolean", "bool"):
            return example.lower() in ("true", "1", "yes")
        elif param_type_lower == "string":
            return example
        else:
            # 尝试智能解析
            if example.lower() in ("true", "false"):
                return example.lower() == "true"
            if example.isdigit():
                return int(example)
            try:
                return float(example)
            except ValueError:
                return example
    except (ValueError, AttributeError):
        return example


def parse_event_data(data: dict) -> dict:
    """
    解析事件报文数据（用于字段展开展示）。

    规则（与 build_message_from_paramlist 统一，example 优先 + paramList 补充）：
    1. 如果 example 有值且是有效的 JSON 对象/数组：
       扁平化 example 得到完整字段路径集，再用 paramList 的 paramDesc/paramType/字段example 补充元数据
       —— 既保证字段完整（example），又保留字段描述（paramList）
    2. 否则（example 为空 / 非法 JSON / 标量）回退到纯 paramList 递归展开
    """
    result = {}

    # 规则1: example 有值，尝试解析
    example = data.get("example")
    parsed_example = None
    if example:
        try:
            parsed_example = json.loads(example)
        except (json.JSONDecodeError, TypeError):
            parsed_example = None

    if isinstance(parsed_example, (dict, list)):
        # 用 paramList 建立 路径 → 元数据 索引
        meta_index = {}
        param_list = data.get("paramList", [])
        if param_list:
            _build_meta_index(param_list, meta_index, "")
        # 扁平化 example，并补充 paramList 元数据
        _flatten_example(parsed_example, result, "", meta_index)
        return result

    # 规则2: example 为空/非对象 → 回退到纯 paramList 递归展开
    param_list = data.get("paramList", [])
    if param_list:
        _parse_children(param_list, result, "")
    return result


def _build_meta_index(items: list, index: dict, parent_key: str) -> None:
    """从 paramList 递归建立 路径 → 元数据（paramDesc/paramType/example）索引"""
    for item in items:
        if not item or not isinstance(item, dict):
            continue
        name = item.get("name", "")
        if not name:
            continue
        key = f"{parent_key}.{name}" if parent_key else name
        index[key] = {
            "paramDesc": item.get("paramDesc", ""),
            "paramType": item.get("paramType", ""),
            "example": item.get("example", ""),
        }
        children = item.get("children")
        if children and isinstance(children, list):
            _build_meta_index(children, index, key)


def _flatten_example(obj, result: dict, parent_key: str, meta_index: dict) -> None:
    """扁平化 example 对象/数组，用 meta_index 补充每条路径的元数据"""
    # 数组：取首个对象元素作为代表展开
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            obj = obj[0]
        else:
            return
    if not isinstance(obj, dict):
        return

    for k, v in obj.items():
        key = f"{parent_key}.{k}" if parent_key else k
        meta = meta_index.get(key, {})
        is_leaf = not isinstance(v, (dict, list))
        # 叶子节点：优先取 paramList 的字段 example，没有则用 example JSON 的实际值
        field_example = meta.get("example", "")
        if not field_example and is_leaf:
            field_example = _stringify_value(v)
        result[key] = {
            "name": k,
            "paramDesc": meta.get("paramDesc", ""),
            "paramType": meta.get("paramType", ""),
            "example": field_example,
        }
        if not is_leaf:
            _flatten_example(v, result, key, meta_index)


def _stringify_value(v) -> str:
    """将标量值转为字符串展示（字符串去引号、布尔转 true/false）"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    return str(v)


def _parse_children(items: list, result: dict, parent_key: str = ""):
    """递归解析children数组"""
    for item in items:
        if not item or not isinstance(item, dict):
            continue

        name = item.get("name", "")
        if not name:
            continue

        key = f"{parent_key}.{name}" if parent_key else name

        result[key] = {
            "name": name,
            "paramDesc": item.get("paramDesc", ""),
            "paramType": item.get("paramType", ""),
            "example": item.get("example", ""),
        }

        children = item.get("children")
        if children and isinstance(children, list):
            _parse_children(children, result, key)


def format_event_list(events: list) -> str:
    """格式化事件列表输出"""
    if not events:
        return "未找到匹配的事件"

    lines = []
    for i, event in enumerate(events, 1):
        lines.append(f"【{i}】{event.get('eventName', '')}")
        lines.append(f"    事件ID: {event.get('eventId', '')}")
        lines.append(f"    事件编码: {event.get('eventCode', '')}")
        lines.append(f"    描述: {event.get('eventDesc', '')}")
        lines.append(f"    分类: {event.get('classifyPath', '')}")
        lines.append("")
    return "\n".join(lines)


def format_event_detail(detail: dict) -> str:
    """格式化事件详情输出"""
    lines = []
    lines.append("=" * 60)
    lines.append("事件详情")
    lines.append("=" * 60)
    lines.append(f"事件名称: {detail.get('eventName', '')}")
    lines.append(f"事件编码: {detail.get('eventCode', '')}")
    lines.append(f"描述: {detail.get('description', '')}")
    lines.append(f"领域应用: {detail.get('domainAppCode', '')}")
    lines.append("")

    # 解析报文
    parsed = parse_event_data(detail)

    lines.append("-" * 60)
    lines.append("事件报文（按字段展开）")
    lines.append("-" * 60)

    for path, info in parsed.items():
        if not isinstance(info, dict):
            lines.append(f"{path}: {info}")
            lines.append("")
            continue

        param_desc = info.get("paramDesc", "")
        param_type = info.get("paramType", "")
        example = info.get("example", "")
        lines.append(f"{path}")
        if param_desc:
            lines.append(f"    描述: {param_desc}")
        if param_type:
            lines.append(f"    类型: {param_type}")
        if example:
            lines.append(f"    示例: {example}")
        lines.append("")

    # 输出 JSON 报文
    lines.append("-" * 60)
    lines.append("事件报文（JSON格式）")
    lines.append("-" * 60)
    message = build_message_from_paramlist(detail)
    lines.append(json.dumps(message, indent=2, ensure_ascii=False))

    return "\n".join(lines)


def format_event_json(detail: dict) -> str:
    """只输出事件报文 JSON"""
    message = build_message_from_paramlist(detail)
    return json.dumps(message, indent=2, ensure_ascii=False)


def format_event_full_json(detail: dict) -> str:
    """输出完整的事件详情 JSON（包含报文）"""
    return json.dumps(detail, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="事件订阅查询工具")
    parser.add_argument("--query", "-q", help="查询事件类型（支持模糊匹配）")
    parser.add_argument("--detail", "-d", help="查询事件详情（需要事件ID）")
    parser.add_argument("--select", "-s", type=int, help="选择事件列表中的第N个")
    parser.add_argument("--all", "-a", action="store_true", help="列出所有事件类型")
    parser.add_argument("--token", "-t", help="指定access_token（不自动获取）")
    parser.add_argument("--force-refresh", action="store_true", help="强制刷新token")
    parser.add_argument("--debug", action="store_true", help="启用调试日志")
    parser.add_argument("--json", "-j", action="store_true", help="只输出事件报文JSON（不输出字段说明）")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        # 获取token
        if args.token:
            token = args.token
            logger.info("使用指定的token")
        else:
            logger.info("正在获取access_token...")
            token = get_access_token(force_refresh=args.force_refresh)
            logger.info("获取成功")

        if args.query:
            # 查询事件类型
            logger.info("查询事件: %s", args.query)
            events = query_event_types(args.query, token)

            if not events:
                print("未找到匹配的事件")
                return

            # 如果使用 --json 参数且只有一个结果，只输出报文
            if args.json and len(events) == 1:
                event = events[0]
                event_id = event.get("eventId")
                logger.info("自动查询详情（事件ID: %s）", event_id)
                try:
                    detail = get_event_detail(event_id, token)
                    print(format_event_json(detail))
                except Exception as e:
                    print(f"详情查询失败: {e}")
                    print("提示: 部分环境的详情 API 未在网关注册，请通过联调测试获取报文格式")
                return

            print(f"找到 {len(events)} 个匹配的事件:\n")
            print(format_event_list(events))

            # 如果只有一个结果，自动查询详情
            if len(events) == 1:
                event = events[0]
                event_id = event.get("eventId")
                logger.info("自动查询详情（事件ID: %s）", event_id)
                try:
                    detail = get_event_detail(event_id, token)
                    print(format_event_detail(detail))
                except Exception as e:
                    print(f"\n详情查询失败: {e}")
                    print("提示: 部分环境的详情 API 未在网关注册，请通过联调测试获取报文格式")

        elif args.all:
            # 查询所有事件
            logger.info("查询所有事件类型...")
            events = query_event_types(None, token)

            if not events:
                print("未找到任何事件")
                return

            print(f"共 {len(events)} 个事件:\n")
            print(format_event_list(events))

        elif args.select:
            # 查询所有事件并选择
            logger.info("查询所有事件类型...")
            events = query_event_types(None, token)

            if not events:
                print("未找到任何事件")
                return

            print(f"共 {len(events)} 个事件:\n")
            print(format_event_list(events))

            idx = args.select - 1
            if 0 <= idx < len(events):
                event = events[idx]
                event_id = event.get("eventId")
                logger.info("查询详情（选择第%d个: %s）", args.select, event.get("eventName"))
                detail = get_event_detail(event_id, token)
                if args.json:
                    print(format_event_json(detail))
                else:
                    print(format_event_detail(detail))
            else:
                print(f"选择无效: {args.select}")

        elif args.detail:
            # 查询事件详情
            logger.info("查询事件详情: %s", args.detail)
            detail = get_event_detail(args.detail, token)
            if args.json:
                print(format_event_json(detail))
            else:
                print(format_event_detail(detail))

        else:
            parser.print_help()

    except Exception as e:
        logger.error("错误: %s", e)
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
