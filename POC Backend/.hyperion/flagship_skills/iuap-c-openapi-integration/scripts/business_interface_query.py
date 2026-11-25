#!/usr/bin/env python3
"""
业务接口查询（增强版）

Features:
    - 通用 HTTP 请求抽象（消除重复代码）
    - 并行查询支持
    - 响应缓存（减少重复查询）
    - 限流保护
    - 分页支持
    - 更完善的错误处理

"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# 添加 iuap_common 目录到 sys.path，导入共享模块
# 路径: scripts/business_interface_query.py → scripts/ → iuap_common/
_script_dir = Path(__file__).resolve().parent
_common_dir = _script_dir / "iuap_common"
if str(_common_dir) not in sys.path:
    sys.path.insert(0, str(_common_dir))

import requests

from iuap_common.bip_auth import (
    get_access_token,
    invalidate_token_cache,
    _should_retry_with_new_token,
)
from iuap_common.console_utf8 import configure_stdio_utf8
from iuap_common.python_version_check import require_python_version
from iuap_common.retry_utils import retry_on_failure, RateLimiter
from iuap_common.utils import ExitCode, load_dotenv, resolve_config

logger = logging.getLogger(__name__)


# =============================================================================
# 配置加载
# =============================================================================

def _skill_dir() -> Path:
    # scripts/business_interface_query.py → scripts/ → skill root
    return Path(__file__).resolve().parent.parent


# =============================================================================
# 数据结构
# =============================================================================

@dataclass
class QueryResult:
    """查询结果封装"""
    success: bool
    description: str
    text: str
    api_id: Optional[str] = None
    api_name: Optional[str] = None
    error: Optional[str] = None
    duration_ms: float = 0.0
    # 推荐相关字段
    recommended_api: Optional[Dict[str, Any]] = None  # 推荐的API
    recommend_reason: Optional[str] = None  # 推荐原因
    candidates: List[Dict[str, Any]] = field(default_factory=list)  # 候选列表
    # 多API详情支持（需求确认模式）
    multi_apis: List[Dict[str, Any]] = field(default_factory=list)  # 多个API详情列表
    match_type: Optional[str] = None  # 匹配类型: single, multi_exact, multi_fuzzy, none
    # 分类选择支持
    category_summary: List[tuple] = field(default_factory=list)  # 分类摘要 [(分类名, 数量, 开始序号, 结束序号, 分类序号)]
    need_category_select: bool = False  # 是否需要先选择分类


@dataclass
class QueryOptions:
    """查询选项"""
    max_workers: int = 3  # 并行查询线程数
    use_cache: bool = True  # 是否启用缓存
    cache_ttl_seconds: float = 300.0  # 缓存 TTL
    page_size: int = 20  # 分页大小
    include_raw: bool = False  # 是否包含原始响应
    confirm_mode: bool = False  # 需求确认模式（Y=严格确认，N=交互选择）


# =============================================================================
# 缓存实现
# =============================================================================

class ResponseCache:
    """简单的内存缓存"""

    def __init__(self, ttl_seconds: float = 300.0):
        self._cache: Dict[str, tuple[Any, float]] = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def _key(self, endpoint: str, params: Dict[str, Any]) -> str:
        """生成缓存键"""
        raw = json.dumps({"endpoint": endpoint, "params": params}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, endpoint: str, params: Dict[str, Any]) -> Optional[Any]:
        key = self._key(endpoint, params)
        with self._lock:
            if key in self._cache:
                data, expires_at = self._cache[key]
                if time.time() < expires_at:
                    logger.debug("缓存命中: %s", endpoint)
                    return data
                else:
                    del self._cache[key]
        return None

    def set(self, endpoint: str, params: Dict[str, Any], data: Any) -> None:
        key = self._key(endpoint, params)
        with self._lock:
            self._cache[key] = (data, time.time() + self._ttl)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


# =============================================================================
# 特征组详情查询（Step 7）
# =============================================================================

def _collect_characteristic_codes(params: list, path_prefix: str = "") -> List[tuple[str, str]]:
    """
    递归收集所有 characteristic 类型的参数。

    Returns:
        List of (code, path) tuples
    """
    results = []
    for p in params:
        if not isinstance(p, dict):
            continue
        param_type = p.get("参数类型") or ""
        if "characteristic" in param_type.lower():
            name = p.get("参数名") or ""
            full_path = f"{path_prefix}.{name}" if path_prefix else name
            results.append((name, full_path))
        # 递归处理子参数
        children = p.get("子参数")
        if isinstance(children, list) and children:
            current_path = f"{path_prefix}.{p.get('参数名', '')}" if path_prefix else p.get('参数名', '')
            results.extend(_collect_characteristic_codes(children, current_path))
    return results


def query_characteristic_detail(
    cfg: dict,
    api_id: str,
    full_name: str,
    field_name: str,
) -> Optional[Dict[str, Any]]:
    """
    查询特征组详情。

    Args:
        cfg: 配置字典
        api_id: API ID（从步骤4查询API详情获取）
        full_name: 特征组全名（如 znbzbx.commonexpensebill.CommonExpenseBillVO）
        field_name: 字段名（如 expensebillDcs）

    Returns:
        特征组字段列表，失败返回 None
    """
    char_cfg = cfg.get("characteristic") or {}
    detail_path = char_cfg.get("detail_path", "")
    if not detail_path:
        logger.warning("未配置特征组详情查询路径")
        return None

    # 替换路径中的租户ID占位符
    tenant_placeholder = char_cfg.get("detail_path_tenant_placeholder", "{tenantId}")
    tenant_id = os.environ.get("YONBIP_TENANT_ID") or char_cfg.get("tenant_id", "")
    if tenant_placeholder in detail_path and tenant_id:
        detail_path = detail_path.replace(tenant_placeholder, tenant_id)
        logger.debug("特征组API路径(租户替换后): %s", detail_path)

    # 构建查询参数（GET 请求使用 query_params）
    query_params = {
        char_cfg.get("request_api_id_field", "apiId"): api_id,
        char_cfg.get("request_full_name_field", "fullName"): full_name,
        char_cfg.get("request_field_name_field", "fieldName"): field_name,
        char_cfg.get("request_type_field", "type"): "originalIsv",
        char_cfg.get("request_is_ajax_field", "isAjax"): "1",
    }

    # 尝试从特征组缓存读取
    cache_key = {"apiId": api_id, "fullName": full_name, "fieldName": field_name}
    cached = _characteristic_cache.get(detail_path, cache_key)
    if cached is not None:
        logger.debug("特征组缓存命中: %s", full_name)
        return cached

    try:
        resp = _execute_http_request(
            cfg,
            HttpRequest(
                method="GET",
                path=detail_path,
                query_params=query_params,
            ),
            use_cache=False,  # 使用独立的特征组缓存
            characteristic_request=True,  # 标记为特征组请求
        )
    except Exception as e:
        logger.warning("查询特征组详情失败 [%s]: %s", full_name, e)
        return None

    resp_body = resp.body
    if not _code_ok(resp_body, char_cfg.get("success_code", "200")):
        logger.warning("查询特征组详情响应异常 [%s]: %s", full_name, resp_body)
        return None

    # 解析字段列表
    data = resp_body.get("data")
    if data is None:
        logger.warning("查询特征组详情 data 为 null [%s]", field_name)
        return None

    # 支持两种格式：data 直接是列表，或 data.fields
    if isinstance(data, list):
        fields = data
    elif isinstance(data, dict):
        fields_key = char_cfg.get("response_fields_key", "fields")
        fields = data.get(fields_key)
        if not isinstance(fields, list):
            logger.warning("查询特征组详情无 fields [%s]", field_name)
            return None
    else:
        logger.warning("查询特征组详情 data 类型异常 [%s]: %s", field_name, type(data))
        return None

    # 写入特征组缓存
    _characteristic_cache.set(detail_path, cache_key, fields)

    return fields


def enrich_characteristic_fields(params: list, cfg: dict, api_id: str) -> list:
    """
    递归解析 characteristic 类型的字段，查询特征组详情并补充子字段结构。

    Args:
        params: 入参列表
        cfg: 配置字典
        api_id: API ID（从步骤4查询API详情获取）

    Returns:
        增强后的入参列表
    """
    char_cfg = cfg.get("characteristic") or {}
    if not char_cfg:
        return params

    result = []
    for p in params:
        if not isinstance(p, dict):
            result.append(p)
            continue

        # 深拷贝当前参数
        enhanced = dict(p)

        # 检查是否是 characteristic 类型
        param_type = enhanced.get("参数类型") or ""
        if "characteristic" in param_type.lower():
            name = enhanced.get("参数名") or ""
            full_name = enhanced.get("fullName") or ""
            fields = None
            if full_name and name:
                # 使用 api_id、fullName 和 field_name 查询特征组详情
                fields = query_characteristic_detail(
                    cfg,
                    api_id=api_id,
                    full_name=full_name,
                    field_name=name,
                )
            if fields:
                # 构建特征组子字段结构（按用户要求的数组格式）
                sub_fields: List[Dict[str, Any]] = []
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    code = field.get(char_cfg.get("response_field_code_key", "fieldCode"), "")
                    if code:
                        field_info: Dict[str, Any] = {
                            "参数名": code,
                            "参数描述": field.get(char_cfg.get("response_field_name_key", "fieldName"), ""),
                            "参数类型": field.get(char_cfg.get("response_field_type_key", "fieldType"), ""),
                            "请求参数类型": "BodyParam",
                        }
                        # 添加参照uri字段（取每个字段独立的fullName）
                        field_fullname_key = char_cfg.get("response_field_fullname_key", "fullName")
                        field_fullname = field.get(field_fullname_key) if field_fullname_key else None
                        if field_fullname:
                            field_info["参照uri"] = field_fullname
                        sub_fields.append(field_info)
                if sub_fields:
                    enhanced["特征组字段"] = sub_fields

        # 递归处理子参数
        children = enhanced.get("子参数")
        if isinstance(children, list) and children:
            enhanced["子参数"] = enrich_characteristic_fields(children, cfg, api_id)

        result.append(enhanced)

    return result


# 全局缓存实例
_global_cache: ResponseCache = ResponseCache()

# 特征组详情缓存（独立的缓存实例，TTL更长）
_characteristic_cache: ResponseCache = ResponseCache(ttl_seconds=3600.0)


# =============================================================================
# HTTP 请求抽象
# =============================================================================

def _normalize_api_path(path: str) -> str:
    """规范化 API 路径"""
    p = (path or "").strip()
    return p if p.startswith("/") else f"/{p}"


def _api_timeout_verify(cfg: dict) -> tuple[int, bool]:
    """获取超时和 TLS 配置"""
    api = cfg.get("api") or {}
    return int(api.get("http_timeout_seconds", 120)), not bool(api.get("insecure_tls"))


@dataclass
class HttpRequest:
    """HTTP 请求配置"""
    method: str  # GET, POST
    path: str
    body: Optional[Dict[str, Any]] = None
    query_params: Optional[Dict[str, str]] = None


@dataclass
class HttpResponse:
    """HTTP 响应封装"""
    status_code: int
    headers: Dict[str, str]
    body: Any
    raw_body: Any  # 原始响应


def _execute_http_request(
    cfg: dict,
    request: HttpRequest,
    *,
    use_cache: bool = True,
    _retry: bool = True,
    characteristic_request: bool = False,
) -> HttpResponse:
    """
    通用 HTTP 请求执行器。

    支持:
        - GET/POST
        - Token 自动注入
        - 缓存
        - 自动重试
        - 特征组请求（模拟浏览器Ajax）
    """
    api = cfg.get("api") or {}
    base = (api.get("base_url") or "").strip().rstrip("/")
    path = _normalize_api_path(request.path)
    url = f"{base}{path}"
    timeout, verify = _api_timeout_verify(cfg)

    # 特征组请求特殊处理
    if characteristic_request:
        # 特征组API需要浏览器的登录态，模拟Ajax请求
        token = get_access_token(cfg)
        params = {
            "access_token": token,
        }
        if request.query_params:
            params.update(request.query_params)

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{base}/",
            "X-Requested-With": "XMLHttpRequest",
        }
        r = requests.get(url, params=params, timeout=timeout, verify=verify, headers=headers)
    else:
        # 获取 token
        token = get_access_token(cfg)
        params = {"access_token": token}
        if request.query_params:
            params.update(request.query_params)

        # 尝试从缓存读取
        cache_key_data = {"url": url, "params": params, "body": request.body}
        if use_cache and request.method == "GET":
            cached = _global_cache.get(request.path, cache_key_data)
            if cached is not None:
                return HttpResponse(
                    status_code=200,
                    headers={},
                    body=cached,
                    raw_body=cached
                )

        # 发送请求（添加 Referer 头以避免 403 错误）
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{base}/",
        }
        if request.method.upper() == "POST":
            r = requests.post(url, params=params, json=request.body, timeout=timeout, verify=verify, headers=headers)
        else:
            r = requests.get(url, params=params, timeout=timeout, verify=verify, headers=headers)

    # 解析响应
    try:
        payload = r.json()
    except ValueError:
        payload = None

    # Token 失效重试（包括特征组请求）
    if _retry and _should_retry_with_new_token(cfg, r.status_code, payload):
        logger.warning("请求 Token 失效，刷新后重试")
        invalidate_token_cache(cfg)
        try:
            get_access_token(cfg, force_refresh=True)
        except Exception as e:
            logger.error("Token 刷新失败: %s", e)
        return _execute_http_request(cfg, request, use_cache=False, _retry=False, characteristic_request=characteristic_request)

    r.raise_for_status()

    if payload is None:
        try:
            payload = json.loads(r.content.decode("utf-8"))
        except ValueError as e:
            snippet = (r.content.decode("utf-8", errors="replace") or "")[:500]
            raise RuntimeError(f"响应非 JSON: {snippet}") from e

    # 写入缓存（非特征组请求）
    if not characteristic_request and use_cache and request.method == "GET":
        _global_cache.set(request.path, cache_key_data, payload)

    return HttpResponse(
        status_code=r.status_code,
        headers=dict(r.headers),
        body=payload,
        raw_body=payload
    )


# =============================================================================
# 业务接口智能匹配
# =============================================================================

def jaccard_similarity(s1: str, s2: str) -> float:
    """计算两个字符串的 Jaccard 相似度（基于字符集合）"""
    if not s1 or not s2:
        return 0.0
    set1 = set(s1.lower())
    set2 = set(s2.lower())
    intersection = len(set1 & set2)
    union = len(set1 | set2)
    return intersection / union if union > 0 else 0.0


def group_by_name_similarity(items: List[Dict[str, Any]], threshold: float = 0.3) -> List[List[Dict[str, Any]]]:
    """
    根据 apiName 相似度将接口动态分组。

    使用贪心算法：
    1. 选择第一个未分组的接口作为基准
    2. 将与基准相似度 >= threshold 的接口归为一组
    3. 重复直到所有接口都分组完成

    Args:
        items: 接口列表
        threshold: 相似度阈值（0-1），低于此值的归为不同组

    Returns:
        分组列表，每个元素是一组相似接口
    """
    if not items:
        return []

    groups: List[List[Dict[str, Any]]] = []
    used: set[int] = set()

    for i, item in enumerate(items):
        if i in used:
            continue

        # 创建新组，以当前接口为基准
        group = [item]
        used.add(i)
        base_name = item.get("apiName", "")

        # 查找与基准相似的接口
        for j, other in enumerate(items):
            if j in used:
                continue

            other_name = other.get("apiName", "")
            sim = jaccard_similarity(base_name, other_name)

            if sim >= threshold:
                group.append(other)
                used.add(j)

        groups.append(group)

    return groups


def classify_apis_by_category(candidates: List[Dict[str, Any]], user_input: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    将接口列表按 API category 一级分类。

    Args:
        candidates: 候选接口列表
        user_input: 用户原始输入（用于相似度排序）

    Returns:
        按 category 组织的接口字典
    """
    api_category_map: Dict[str, List[Dict[str, Any]]] = {}

    for item in candidates:
        cat = item.get("category") or "其他"
        if cat not in api_category_map:
            api_category_map[cat] = []
        api_category_map[cat].append(item)

    # 计算相似度得分（用于排序）
    def calc_score(item: Dict[str, Any]) -> int:
        keyword_lower = user_input.lower()
        api_name = (item.get("apiName", "") or "").lower()
        if api_name == keyword_lower:
            return 1000
        elif api_name.startswith(keyword_lower):
            return 900
        elif keyword_lower in api_name:
            return 800
        return 0

    # 对每个一级分类下的接口按相似度排序
    result: Dict[str, List[Dict[str, Any]]] = {}
    for cat, items in api_category_map.items():
        items.sort(key=calc_score, reverse=True)
        result[cat] = items

    return result


def format_candidate_table(candidates: List[Dict[str, Any]], user_input: str) -> str:
    """
    格式化候选接口列表为简单表格输出（Confirm=N 场景使用）。

    直接显示所有候选接口，按相似度排序。

    Args:
        candidates: 候选接口列表
        user_input: 用户原始输入

    Returns:
        格式化的表格字符串
    """
    if not candidates:
        return "未找到匹配的接口"

    # 中文字符宽度计算
    def str_display_width(s: str) -> int:
        width = 0
        for c in s:
            if '一' <= c <= '鿿':
                width += 2
            else:
                width += 1
        return width

    # 按显示宽度截断字符串
    def truncate_to_width(s: str, width: int) -> str:
        result = []
        cur = 0
        for c in s:
            char_w = 2 if '一' <= c <= '鿿' else 1
            if cur + char_w > width:
                break
            result.append(c)
            cur += char_w
        return ''.join(result)

    # 辅助函数：生成固定宽度字段
    def make_cell(text: str, width: int, align: str = 'left') -> str:
        text_w = str_display_width(text)
        if text_w >= width:
            return truncate_to_width(text, width)
        pad = width - text_w
        if align == 'right':
            return ' ' * pad + text
        elif align == 'center':
            left = pad // 2
            right = pad - left
            return ' ' * left + text + ' ' * right
        else:
            return text + ' ' * pad

    # 辅助函数：将长字符串按显示宽度换行
    def wrap_text(s: str, width: int) -> list:
        if str_display_width(s) <= width:
            return [s]
        lines_wrap = []
        cur_line = []
        cur_width = 0
        for c in s:
            char_w = 2 if '一' <= c <= '鿿' else 1
            if cur_width + char_w > width:
                lines_wrap.append(''.join(cur_line))
                cur_line = [c]
                cur_width = char_w
            else:
                cur_line.append(c)
                cur_width += char_w
        if cur_line:
            lines_wrap.append(''.join(cur_line))
        return lines_wrap

    lines = []
    lines.append("")
    lines.append(f"发现 {len(candidates)} 个「{user_input}」相关接口，请输入序号选择：")
    lines.append("")

    # 计算列宽
    max_seq_disp = max(str_display_width(str(i)) for i in range(1, len(candidates) + 1))
    max_name_disp = max(str_display_width(item.get("apiName", "")) for item in candidates)
    max_api_disp = max(len(item.get("apiId", "")) for item in candidates)

    COL_SEQ = max(max_seq_disp + 2, 4)
    COL_NAME = max(max_name_disp, 40)
    COL_API = max_api_disp

    border_top = "┌" + "─" * COL_SEQ + "┬" + "─" * COL_NAME + "┬" + "─" * COL_API + "┐"
    border_mid = "├" + "─" * COL_SEQ + "┼" + "─" * COL_NAME + "┼" + "─" * COL_API + "┤"
    border_bottom = "└" + "─" * COL_SEQ + "┴" + "─" * COL_NAME + "┴" + "─" * COL_API + "┘"

    lines.append(border_top)
    lines.append(f"│{make_cell('序号', COL_SEQ, 'center')}│{make_cell('接口名称', COL_NAME, 'center')}│{make_cell('API ID', COL_API, 'center')}│")
    lines.append(border_mid)

    for i, item in enumerate(candidates, 1):
        api_name = item.get("apiName", "")
        api_id = item.get("apiId", "")

        name_lines = wrap_text(api_name, COL_NAME)
        seq_str = make_cell(str(i), COL_SEQ, 'right')
        api_str = api_id.ljust(COL_API)

        first_name_line = name_lines[0] if name_lines else ""
        lines.append(f"│{seq_str}│{make_cell(first_name_line, COL_NAME)}│{api_str}│")

        for extra_line in name_lines[1:]:
            lines.append(f"│{'': ^{COL_SEQ}}│{make_cell(extra_line, COL_NAME)}│{'': ^{COL_API}}│")

    lines.append(border_bottom)
    lines.append("")
    lines.append(f"请输入序号选择（1-{len(candidates)}）：")
    lines.append("")

    return "\n".join(lines)


def smart_match_api(
    data_node: List[Dict[str, Any]],
    user_input: str,
    *,
    explicit_api_name: Optional[str] = None,
    explicit_index: Optional[int] = None,
    confirm_mode: bool = False,
) -> tuple[Optional[Dict[str, Any]], str, List[Dict[str, Any]]]:
    """
    智能匹配 API 接口。

    匹配策略（按优先级）:
    1. 精确指定 apiName → 完全匹配 apiName
    2. 精确指定索引 → 直接按索引选择
    3. 精确匹配 → apiName == user_input（唯一）
    4. 模糊匹配 → apiName 包含 user_input（唯一）
    5. 多候选 → confirm_mode=True 返回多候选，confirm_mode=False 提示用户选择

    Args:
        data_node: API 列表数据
        user_input: 用户原始输入
        explicit_api_name: 命令行精确指定的 apiName
        explicit_index: 命令行精确指定的索引
        confirm_mode: 需求确认模式（Y=严格确认，N=交互选择）

    Returns:
        (选中的接口, 匹配说明, 候选列表)
    """
    if not data_node:
        return None, "无候选接口", []

    # 策略1: 精确指定 apiName（最高优先级）
    if explicit_api_name:
        for item in data_node:
            if item.get("apiName") == explicit_api_name:
                return item, f"精确指定 apiName: {explicit_api_name}", data_node
        # 精确指定但没找到，返回多候选
        return None, f"未找到指定的 apiName: {explicit_api_name}", data_node

    # 策略2: 精确指定索引（次高优先级，用户明确指定，1-based）
    if explicit_index is not None:
        # 转换为 0-based 索引
        idx_0based = explicit_index - 1
        if 0 <= idx_0based < len(data_node):
            return data_node[idx_0based], f"索引选择: 第 {explicit_index} 个", data_node
        else:
            return None, f"索引 {explicit_index} 超出范围 (1-{len(data_node)})", data_node

    # 策略3: 精确匹配（apiName == user_input）
    exact_matches = [
        item for item in data_node
        if item.get("apiName") == user_input
    ]
    if len(exact_matches) == 1:
        return exact_matches[0], f"精确匹配: {exact_matches[0].get('apiName')}", data_node
    if len(exact_matches) > 1:
        # 需求确认模式：返回所有精确匹配的接口
        if confirm_mode:
            return None, "EXACT_MULTI", exact_matches
        # 交互模式：返回多候选让用户选择
        return None, f"存在 {len(exact_matches)} 个精确匹配的接口", exact_matches

    # 策略4: 模糊匹配（apiName 包含 user_input）
    fuzzy_matches = [
        item for item in data_node
        if user_input in (item.get("apiName") or "")
    ]

    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0], f"模糊匹配: {fuzzy_matches[0].get('apiName')}", data_node

    if len(fuzzy_matches) > 1:
        # 需求确认模式：返回所有模糊匹配的接口
        if confirm_mode:
            return None, "FUZZY_MULTI", fuzzy_matches
        # 交互模式：返回多候选让用户选择
        return None, f"存在 {len(fuzzy_matches)} 个模糊匹配的接口", fuzzy_matches

    # 策略5: 无匹配
    if confirm_mode:
        return None, "NONE", []
    # 交互模式：返回所有候选
    return None, f"存在 {len(data_node)} 个候选接口，请明确指定", data_node


def collect_all_candidates(data_node: List[Dict[str, Any]], user_input: str) -> List[Dict[str, Any]]:
    """
    收集所有与用户输入相关的候选接口。

    包含: 精确匹配 + 模糊匹配 + 完全包含用户输入的
    """
    if not data_node:
        return []

    candidates = []
    seen_ids = set()

    # 精确匹配
    for item in data_node:
        if item.get("apiName") == user_input and item.get("apiId") not in seen_ids:
            candidates.append(item)
            seen_ids.add(item.get("apiId"))

    # 模糊匹配
    for item in data_node:
        api_name = item.get("apiName") or ""
        if user_input in api_name and item.get("apiId") not in seen_ids:
            candidates.append(item)
            seen_ids.add(item.get("apiId"))

    # 完全包含
    for item in data_node:
        api_name = item.get("apiName") or ""
        if api_name and api_name.startswith(user_input) and item.get("apiId") not in seen_ids:
            candidates.append(item)
            seen_ids.add(item.get("apiId"))

    return candidates


# =============================================================================
# 业务接口查询
# =============================================================================

def _code_ok(node: dict, expected: str) -> bool:
    """检查响应码是否匹配"""
    c = node.get("code")
    return str(c).strip() == str(expected).strip()


def build_input_param_node(param: dict) -> dict:
    """构建入参节点（含递归子参数）"""
    node: Dict[str, Any] = {
        "参数名": param.get("name") or "",
        "参数描述": param.get("paramDesc") or "",
        "参数类型": param.get("paramType") or "",
        "请求参数类型": param.get("requestParamType") or "",
    }
    # 提取 fullName（特征组详情查询需要）
    if param.get("fullName"):
        node["fullName"] = param.get("fullName")
    children = param.get("children")
    if isinstance(children, list) and children:
        node["子参数"] = [
            build_input_param_node(c)
            for c in children
            if isinstance(c, dict)
        ]
    return node


def build_field_node(field: dict) -> dict:
    """构建出参字段节点"""
    node: Dict[str, Any] = {
        "描述": field.get("paramDesc") or "",
        "类型": field.get("paramType") or "",
    }
    children = field.get("children")
    if isinstance(children, list) and children:
        children_node: Dict[str, Any] = {}
        for child in children:
            if not isinstance(child, dict):
                continue
            cn = child.get("name") or ""
            if cn:
                children_node[cn] = build_field_node(child)
        if children_node:
            node["子字段"] = children_node
    return node


def split_interface_input_hints(text: str) -> List[str]:
    """将话术中的入参关注点拆成关键词列表"""
    if not text or not str(text).strip():
        return []
    raw = str(text).replace("，", ",").replace("、", ",")
    return [p.strip() for p in raw.split(",") if p.strip()]


def _param_row_matches_hint(name: str, desc: str, hint: str) -> bool:
    """判断参数行是否匹配提示词"""
    h = hint.strip()
    if not h:
        return False
    n, d = name or "", desc or ""

    # 精确包含匹配
    if h in n or h in d:
        return True

    # 大小写不敏感匹配
    hl, nl = h.lower(), n.lower()
    if hl and (hl in nl or hl in (d or "").lower()):
        return True

    return False


def collect_matching_input_params(
    params: List[dict], hints: List[str], path_prefix: str = ""
) -> List[Dict[str, Any]]:
    """收集所有匹配的入参（含递归）"""
    rows: List[Dict[str, Any]] = []
    for p in params:
        if not isinstance(p, dict):
            continue
        pname = str(p.get("参数名") or "")
        pdesc = str(p.get("参数描述") or "")
        full_path = f"{path_prefix}.{pname}" if path_prefix else pname

        for h in hints:
            if _param_row_matches_hint(pname, pdesc, h):
                rows.append({
                    "匹配关键词": h,
                    "参数路径": full_path or pname,
                    "参数名": pname,
                    "参数描述": pdesc,
                    "参数类型": p.get("参数类型") or "",
                    "请求参数类型": p.get("请求参数类型") or "",
                })
                break

        # 递归处理子参数
        children = p.get("子参数")
        if isinstance(children, list) and children:
            rows.extend(collect_matching_input_params(children, hints, full_path or pname))

    return rows


def build_ai_friendly_structure(detail_data: dict) -> dict:
    """构建 AI 友好的响应结构"""
    result: Dict[str, Any] = {
        "接口地址": detail_data.get("completeProxyUrl") or "",
        "请求协议": detail_data.get("requestProtocol") or "HTTP",
        "请求方式": detail_data.get("serviceHttpMethod") or "POST",
    }

    # 入参列表
    input_params: List[dict] = []
    pdtos = detail_data.get("paramDTOS")
    if isinstance(pdtos, list):
        for p in pdtos:
            if isinstance(p, dict):
                input_params.append(build_input_param_node(p))
    result["入参列表"] = input_params

    # 出参结构
    out_struct: Dict[str, Any] = {}
    prdtos = detail_data.get("paramReturnDTOS")
    if isinstance(prdtos, list):
        for rp in prdtos:
            if not isinstance(rp, dict):
                continue
            name = rp.get("name") or ""
            if name:
                out_struct[name] = build_field_node(rp)
    result["出参结构"] = out_struct

    # 代码生成提示
    result["代码生成提示"] = _generate_code_hint(result)

    return result


def _generate_code_hint(structure: dict) -> str:
    """生成代码调用提示"""
    method = structure.get("请求方式") or "POST"
    url = structure.get("接口地址") or ""
    return (
        "基于以上接口信息，你可以：\n"
        "1. 使用OpenAPI调用方式：参考 getOpenApiCall 工具获取完整的OpenAPI调用流程\n"
        "2. 使用RestProxy前端调用：参考 getRestProxyGuide 工具获取前端REST调用指南\n"
        f"3. 后端HTTP调用：使用HttpClient或RestTemplate发送{method}请求到{url}"
    )


def enrich_with_input_hints(friendly: dict, hints: List[str]) -> dict:
    """在 AI 友好结构中附加入参关注点匹配结果"""
    if not hints:
        return friendly

    out = dict(friendly)
    out["用户话术中的入参关注点"] = hints

    inputs = out.get("入参列表")
    if isinstance(inputs, list):
        matched = collect_matching_input_params(inputs, hints)
        out["与关注点匹配的入参"] = matched
        if not matched:
            out["与关注点匹配的入参说明"] = (
                "未在接口入参名/描述中找到与上述关注点直接匹配项，请以完整「入参列表」为准或调整关键词。"
            )
    else:
        out["与关注点匹配的入参"] = []
        out["与关注点匹配的入参说明"] = "接口详情中无入参列表结构，请查看原始返回。"

    return out


def _is_failed_block(block: str) -> bool:
    """判断输出块是否为失败信息"""
    prefixes = (
        "错误",
        "查询失败",
        "未找到",
        "查询接口列表失败",
        "查询接口详情失败",
    )
    return any(block.startswith(p) for p in prefixes)


# =============================================================================
# 需求确认模式：批量查询多个API详情
# =============================================================================

def query_api_detail(
    cfg: dict,
    api_info: Dict[str, Any],
    interface_input_hints: Optional[str],
    options: QueryOptions,
) -> Optional[Dict[str, Any]]:
    """
    查询单个API的详情。

    Args:
        cfg: 配置字典
        api_info: API信息（包含 apiId, apiName, category）
        interface_input_hints: 入参关注点
        options: 查询选项

    Returns:
        API详情结构，失败返回 None
    """
    api_id = api_info.get("apiId")
    api_name = api_info.get("apiName") or ""
    category = api_info.get("category") or ""

    if not api_id:
        return None

    bi = cfg.get("business_interface") or {}
    detail_ok = str(bi.get("detail_success_code", "200"))

    try:
        detail_path = bi.get("detail_path", "").replace("{apiId}", str(api_id))
        detail_resp = _execute_http_request(
            cfg,
            HttpRequest(method="GET", path=detail_path),
            use_cache=options.use_cache,
        )
    except Exception as e:
        logger.warning("查询API详情失败 [%s]: %s", api_name, e)
        return None

    detail_node = detail_resp.body
    if not _code_ok(detail_node, detail_ok):
        logger.warning("查询API详情响应异常 [%s]: %s", api_name, detail_node.get("message"))
        return None

    detail_data = detail_node.get("data")
    if not isinstance(detail_data, dict):
        return None

    # 构建输出
    friendly = build_ai_friendly_structure(detail_data)

    # 特征组详情查询
    if cfg.get("characteristic") and api_id:
        input_params = friendly.get("入参列表", [])
        if input_params:
            friendly["入参列表"] = enrich_characteristic_fields(input_params, cfg, api_id)

    # 入参关注点匹配
    hints = split_interface_input_hints(interface_input_hints or "")
    if hints:
        friendly = enrich_with_input_hints(friendly, hints)

    return {
        "api_id": api_id,
        "api_name": api_name,
        "category": category,
        "detail": friendly,
    }


def query_all_details(
    cfg: dict,
    candidates: List[Dict[str, Any]],
    interface_input_hints: Optional[str],
    options: QueryOptions,
) -> List[Dict[str, Any]]:
    """
    批量查询多个API详情。

    Args:
        cfg: 配置字典
        candidates: API候选列表
        interface_input_hints: 入参关注点
        options: 查询选项

    Returns:
        多个API详情列表
    """
    results = []

    # 使用 ThreadPoolExecutor 并行查询
    with ThreadPoolExecutor(max_workers=min(options.max_workers, 5)) as executor:
        futures = {
            executor.submit(
                query_api_detail, cfg, api, interface_input_hints, options
            ): api
            for api in candidates
        }

        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    results.append(result)
            except Exception as e:
                api_name = futures[future].get("apiName", "未知")
                logger.warning("查询API详情异常 [%s]: %s", api_name, e)

    return results


def format_multi_details(
    description: str,
    match_type: str,
    details: List[Dict[str, Any]],
    interface_input_hints: Optional[str] = None,
) -> str:
    """
    格式化多API详情输出。

    Args:
        description: 查询描述
        match_type: 匹配类型
        details: API详情列表
        interface_input_hints: 入参关注点

    Returns:
        格式化的输出字符串
    """
    lines = []

    # 根据匹配类型生成标题
    if match_type == "EXACT_MULTI":
        title = f"【需求确认】接口「{description}」精确匹配到 {len(details)} 个，查询所有详情："
    elif match_type == "FUZZY_MULTI":
        title = f"【需求确认】接口「{description}」精确匹配0个，模糊匹配到 {len(details)} 个，查询所有详情："
    else:
        title = f"【需求确认】接口「{description}」匹配到 {len(details)} 个，查询所有详情："

    lines.append(f"\n{'=' * 70}")
    lines.append(title)
    lines.append(f"{'=' * 70}")

    # 入参关注点
    if interface_input_hints:
        hints = split_interface_input_hints(interface_input_hints)
        if hints:
            lines.append("")
            lines.append("=== 从用户话术梳理的入参关注点 ===")
            for h in hints:
                lines.append(f"- {h}")

    # 生成API列表表格（智能换行版，不截断）
    lines.append("")

    # 表格列宽定义（根据内容自动调整，不截断）
    COL_NUM = 4      # 序号列
    COL_CATEGORY = 10  # 分类列
    COL_METHOD = 10   # 请求方式列
    COL_NAME_MIN = 20   # 接口名称最小宽度
    COL_PATH_MIN = 20   # 请求地址最小宽度

    # 计算需要的列宽（不设最大限制，确保不截断）
    max_name_len = max(COL_NAME_MIN, max(_text_display_width(item.get("api_name", "")) for item in details) + 2)
    max_path_len = max(COL_PATH_MIN, max(_text_display_width(item.get("detail", {}).get("接口地址", "")) for item in details) + 2)

    # 确保能容纳表头
    max_name_len = max(max_name_len, _text_display_width("接口名称"))
    max_path_len = max(max_path_len, _text_display_width("请求地址"))

    # 表头
    header_line1 = f"┌{'─' * COL_NUM}┬{'─' * COL_CATEGORY}┬{'─' * max_name_len}┬{'─' * COL_METHOD}┬{'─' * max_path_len}┐"
    header_line2 = f"│ {'#':^{COL_NUM - 2}} │ {'分类':^{COL_CATEGORY - 2}} │ {'接口名称':^{max_name_len - 2}} │ {'请求方式':^{COL_METHOD - 2}} │ {'请求地址':^{max_path_len - 2}} │"
    header_line3 = f"├{'─' * COL_NUM}┼{'─' * COL_CATEGORY}┼{'─' * max_name_len}┼{'─' * COL_METHOD}┼{'─' * max_path_len}┤"

    lines.append(header_line1)
    lines.append(header_line2)
    lines.append(header_line3)

    # 逐行数据
    for i, item in enumerate(details, 1):
        category = item.get("category", "") or ""
        api_name = item.get("api_name", "") or ""
        detail = item.get("detail", {})
        method = detail.get("请求方式", "") or ""
        path = detail.get("接口地址", "") or ""

        # 对长文本进行换行处理（按列宽）
        name_rows = _wrap_text(api_name, max_name_len - 2)
        path_rows = _wrap_text(path, max_path_len - 2)

        # 取最大行数
        max_rows = max(len(name_rows), len(path_rows), 1)

        # 确保至少有1行
        if not name_rows:
            name_rows = [""]
        if not path_rows:
            path_rows = [""]

        # 补齐行数
        while len(name_rows) < max_rows:
            name_rows.append("")
        while len(path_rows) < max_rows:
            path_rows.append("")

        for row_idx in range(max_rows):
            num_str = f"{i}" if row_idx == 0 else ""
            cat_str = category if row_idx == 0 else ""
            name_str = name_rows[row_idx] if row_idx < len(name_rows) else ""
            method_str = method if row_idx == 0 else ""
            path_str = path_rows[row_idx] if row_idx < len(path_rows) else ""

            lines.append(
                f"│ {_pad_to_width(num_str, COL_NUM - 2)} │ "
                f"{_pad_to_width(cat_str, COL_CATEGORY - 2)} │ "
                f"{_pad_to_width(name_str, max_name_len - 2)} │ "
                f"{_pad_to_width(method_str, COL_METHOD - 2)} │ "
                f"{_pad_to_width(path_str, max_path_len - 2)} │"
            )

    # 表尾
    footer = f"└{'─' * COL_NUM}┴{'─' * COL_CATEGORY}┴{'─' * max_name_len}┴{'─' * COL_METHOD}┴{'─' * max_path_len}┘"
    lines.append(footer)

    # 逐个输出每个API的完整详情
    for i, item in enumerate(details, 1):
        api_id = item.get("api_id", "")
        api_name = item.get("api_name", "")
        category = item.get("category", "")
        detail = item.get("detail", {})

        lines.append("")
        lines.append("─" * 70)
        lines.append(f"【{i}/{len(details)}】{api_name}")
        lines.append(f"API ID: {api_id}")
        lines.append(f"分类:   {category}")
        lines.append("─" * 70)

        # 基本信息
        lines.append("【基本信息】")
        lines.append(f"  接口地址:   {detail.get('接口地址', '')}")
        lines.append(f"  请求协议:   {detail.get('请求协议', 'HTTP')}")
        lines.append(f"  请求方式:   {detail.get('请求方式', '')}")
        lines.append("")

        # 入参列表（完整）
        lines.append("【入参列表】")
        input_params = detail.get("入参列表", [])
        if input_params:
            for p in input_params:
                if isinstance(p, dict):
                    param_name = p.get("参数名", "")
                    param_desc = p.get("参数描述", "")
                    param_type = p.get("参数类型", "")
                    request_type = p.get("请求参数类型", "")
                    lines.append(f"  - {param_name} ({param_type}, {request_type})")
                    if param_desc:
                        lines.append(f"    描述: {param_desc}")
                    # 子参数
                    children = p.get("子参数")
                    if isinstance(children, list) and children:
                        for child in children:
                            if isinstance(child, dict):
                                child_name = child.get("参数名", "")
                                child_desc = child.get("参数描述", "")
                                child_type = child.get("参数类型", "")
                                lines.append(f"    - {child_name} ({child_type})")
                                if child_desc:
                                    lines.append(f"      描述: {child_desc}")
        else:
            lines.append("  (无入参)")
        lines.append("")

        # 出参结构（完整）
        lines.append("【出参结构】")
        output_struct = detail.get("出参结构", {})
        if output_struct:
            for field_name, field_info in output_struct.items():
                if isinstance(field_info, dict):
                    field_desc = field_info.get("描述", "")
                    field_type = field_info.get("类型", "")
                    lines.append(f"  - {field_name} ({field_type})")
                    if field_desc:
                        lines.append(f"    描述: {field_desc}")
                    # 子字段
                    sub_fields = field_info.get("子字段")
                    if isinstance(sub_fields, dict):
                        for sub_name, sub_info in sub_fields.items():
                            if isinstance(sub_info, dict):
                                sub_desc = sub_info.get("描述", "")
                                sub_type = sub_info.get("类型", "")
                                lines.append(f"    - {sub_name} ({sub_type})")
                                if sub_desc:
                                    lines.append(f"      描述: {sub_desc}")
        else:
            lines.append("  (无出参结构)")
        lines.append("")

        # 入参关注点匹配
        matched = detail.get("与关注点匹配的入参")
        if isinstance(matched, list) and matched:
            lines.append("【与入参关注点匹配的参数】")
            for m in matched:
                lines.append(f"  - {m.get('参数名')}: {m.get('参数描述')}")
            lines.append("")

    lines.append("")
    lines.append("=" * 70)
    lines.append("")

    # 输出完整JSON格式详情
    lines.append("=== 完整API详情（JSON格式）===")
    lines.append("")
    lines.append(json.dumps({
        "query_description": description,
        "match_type": match_type,
        "match_count": len(details),
        "apis": details,
    }, ensure_ascii=False, indent=2))

    return "\n".join(lines)


def _safe_substr(s: str, length: int) -> str:
    """安全截取字符串，中文算2个字符宽度"""
    result = []
    count = 0
    for char in s:
        if ord(char) > 127:  # 中文
            if count + 2 <= length:
                result.append(char)
                count += 2
            else:
                result.append("…")
                break
        else:
            if count + 1 <= length:
                result.append(char)
                count += 1
            else:
                result.append("…")
                break
    return "".join(result) if result else ""


def _text_display_width(text: str) -> int:
    """计算文本在终端的显示宽度，中文=2，英文=1"""
    width = 0
    for char in text:
        width += 2 if ord(char) > 0x4E00 and ord(char) <= 0x9FA5 else 1
    return width


def _calc_text_width(text: str) -> int:
    """计算文本宽度（用于表格列宽计算）"""
    return _text_display_width(text)


def _wrap_text(text: str, max_width: int) -> list:
    """智能换行文本，中文按宽度计算，保持完整不截断"""
    if not text:
        return [""]

    lines = []
    current_line = []
    current_width = 0

    for char in text:
        char_width = 2 if (ord(char) > 0x4E00 and ord(char) <= 0x9FA5) else 1

        if current_width + char_width <= max_width:
            current_line.append(char)
            current_width += char_width
        else:
            if current_line:
                lines.append("".join(current_line))
            current_line = [char]
            current_width = char_width

    if current_line:
        lines.append("".join(current_line))

    return lines if lines else [""]


def _pad_to_width(text: str, width: int) -> str:
    """填充文本到指定显示宽度"""
    current_width = _text_display_width(text)
    padding = width - current_width
    if padding > 0:
        return text + " " * padding
    elif padding < 0:
        return text[:width]  # 超长截断
    return text


def query_by_api_id(
    cfg: dict,
    api_id: str,
    interface_input_hints: Optional[str] = None,
    options: Optional[QueryOptions] = None,
) -> QueryResult:
    """
    直接通过 API ID 查询接口详情。

    Args:
        cfg: 配置字典
        api_id: API ID
        interface_input_hints: 入参关注点
        options: 查询选项

    Returns:
        QueryResult 查询结果
    """
    opts = options or QueryOptions()
    start_time = time.time()

    bi = cfg.get("business_interface") or {}
    detail_ok = str(bi.get("detail_success_code", "200"))

    if not api_id:
        return QueryResult(
            success=False,
            description="API ID查询",
            text="错误：请提供 API ID",
            error="缺少 apiId",
            duration_ms=(time.time() - start_time) * 1000,
        )

    # 直接查询详情
    try:
        detail_path = bi.get("detail_path", "").replace("{apiId}", str(api_id))
        detail_resp = _execute_http_request(
            cfg,
            HttpRequest(method="GET", path=detail_path),
            use_cache=opts.use_cache,
        )
    except Exception as e:
        return QueryResult(
            success=False,
            description=f"API ID: {api_id}",
            text=f"查询API详情失败: {e}",
            error=str(e),
            duration_ms=(time.time() - start_time) * 1000,
        )

    detail_node = detail_resp.body
    if not _code_ok(detail_node, detail_ok):
        msg = detail_node.get("message") or detail_node.get("msg") or "未知错误"
        return QueryResult(
            success=False,
            description=f"API ID: {api_id}",
            text=f"查询API详情失败: {msg}",
            error=msg,
            duration_ms=(time.time() - start_time) * 1000,
        )

    detail_data = detail_node.get("data")
    if not isinstance(detail_data, dict):
        return QueryResult(
            success=False,
            description=f"API ID: {api_id}",
            text="查询失败: 详情 data 非对象",
            error="详情格式异常",
            duration_ms=(time.time() - start_time) * 1000,
        )

    # 构建输出
    api_name = detail_data.get("apiName", "")
    category = detail_data.get("category", "")
    friendly = build_ai_friendly_structure(detail_data)

    # 特征组详情查询
    if cfg.get("characteristic") and api_id:
        input_params = friendly.get("入参列表", [])
        if input_params:
            friendly["入参列表"] = enrich_characteristic_fields(input_params, cfg, api_id)

    hints = split_interface_input_hints(interface_input_hints or "")
    if hints:
        friendly = enrich_with_input_hints(friendly, hints)

    # 格式化输出
    out_parts: List[str] = []
    out_parts.append(f"通过 API ID 查询成功:\n")
    out_parts.append(f"- 接口名称: {api_name}\n")
    out_parts.append(f"- API ID: {api_id}\n")
    out_parts.append(f"- 分类: {category}\n")

    if hints:
        out_parts.append("\n=== 从用户话术梳理的入参关注点 ===\n")
        for h in hints:
            out_parts.append(f"- {h}\n")
        out_parts.append("\n")

        matched = friendly.get("与关注点匹配的入参")
        if isinstance(matched, list) and matched:
            out_parts.append("=== 与上述关注点匹配的接口入参 ===\n")
            out_parts.append(json.dumps(matched, ensure_ascii=False, indent=2))
            out_parts.append("\n\n")

    out_parts.append("=== 接口详细信息（AI友好格式）===\n\n")
    out_parts.append(json.dumps(friendly, ensure_ascii=False, indent=2))

    return QueryResult(
        success=True,
        description=f"API ID: {api_id}",
        text="".join(out_parts),
        api_id=str(api_id),
        api_name=api_name,
        duration_ms=(time.time() - start_time) * 1000,
    )


@retry_on_failure(max_attempts=3, delay=0.5, backoff=2.0)
def query_single(
    cfg: dict,
    interface_desc: str,
    *,
    interface_input_hints: Optional[str] = None,
    options: Optional[QueryOptions] = None,
    explicit_api_name: Optional[str] = None,
    explicit_index: Optional[int] = None,
    confirm_mode: bool = False,
) -> QueryResult:
    """
    执行单个接口查询（支持智能匹配）。

    Args:
        cfg: 配置字典
        interface_desc: 接口描述
        interface_input_hints: 入参关注点
        options: 查询选项
        explicit_api_name: 精确指定的 apiName
        explicit_index: 精确指定的索引（1-based，与表格序号一致）
        confirm_mode: 需求确认模式（Y=严格确认，N=交互选择）

    Returns:
        QueryResult 查询结果
    """
    opts = options or QueryOptions()
    start_time = time.time()

    bi = cfg.get("business_interface") or {}
    list_ok = str(bi.get("list_success_code", "200"))
    detail_ok = str(bi.get("detail_success_code", "200"))

    # 1. 查询列表
    try:
        list_resp = _execute_http_request(
            cfg,
            HttpRequest(
                method="POST",
                path=bi.get("list_path", ""),
                body={bi.get("list_body_param_name", "param"): interface_desc},
            ),
            use_cache=opts.use_cache,
        )
    except Exception as e:
        return QueryResult(
            success=False,
            description=interface_desc,
            text=f"查询失败: {e}",
            error=str(e),
            duration_ms=(time.time() - start_time) * 1000,
        )

    list_node = list_resp.body
    if not _code_ok(list_node, list_ok):
        msg = list_node.get("message") or list_node.get("msg") or "未知错误"
        return QueryResult(
            success=False,
            description=interface_desc,
            text=f"查询接口列表失败: {msg}",
            error=msg,
            duration_ms=(time.time() - start_time) * 1000,
        )

    # 2. 解析列表响应
    data_node = list_node.get("data")
    if not isinstance(data_node, list) or len(data_node) == 0:
        return QueryResult(
            success=False,
            description=interface_desc,
            text="未找到匹配的业务接口，请尝试其他描述",
            error="无匹配结果",
            duration_ms=(time.time() - start_time) * 1000,
        )

    # 支持分页：取所有匹配的接口
    if opts.page_size > 0 and len(data_node) > opts.page_size:
        data_node = data_node[:opts.page_size]

    # 3. 智能匹配 API（精确匹配 → 模糊匹配 → 多候选提示）
    selected, match_reason, candidates = smart_match_api(
        data_node,
        interface_desc,
        explicit_api_name=explicit_api_name,
        explicit_index=explicit_index,
        confirm_mode=confirm_mode,
    )

    # 需求确认模式下的多匹配处理
    if confirm_mode and selected is None:
        # 需求确认模式：批量查询所有匹配的API详情
        if match_reason in ("EXACT_MULTI", "FUZZY_MULTI") and candidates:
            all_details = query_all_details(
                cfg, candidates,
                interface_input_hints=interface_input_hints,
                options=opts,
            )
            if all_details:
                formatted_text = format_multi_details(
                    interface_desc,
                    match_reason,
                    all_details,
                    interface_input_hints,
                )
                return QueryResult(
                    success=True,
                    description=interface_desc,
                    text=formatted_text,
                    match_type=match_reason,
                    multi_apis=all_details,
                    duration_ms=(time.time() - start_time) * 1000,
                )
            else:
                return QueryResult(
                    success=False,
                    description=interface_desc,
                    text="批量查询API详情失败",
                    error="QUERY_ALL_FAILED",
                    duration_ms=(time.time() - start_time) * 1000,
                )
        # 无匹配
        elif match_reason == "NONE":
            return QueryResult(
                success=False,
                description=interface_desc,
                text="精确匹配0个，模糊匹配也0个，无法确定目标接口",
                error="NO_MATCH",
                duration_ms=(time.time() - start_time) * 1000,
            )

    # 检查匹配是否成功
    if selected is None:
        # 交互模式：展示所有候选，让用户选择
        candidate_list = collect_all_candidates(data_node, interface_desc)
        if not candidate_list:
            candidate_list = data_node

        # 格式化候选表格
        table_output = format_candidate_table(candidate_list, interface_desc)

        return QueryResult(
            success=False,
            description=interface_desc,
            text=f"匹配失败: {match_reason}\n{table_output}",
            error="NEED_SELECT",
            duration_ms=(time.time() - start_time) * 1000,
            candidates=candidate_list,
        )

    if not isinstance(selected, dict):
        return QueryResult(
            success=False,
            description=interface_desc,
            text="查询失败: 列表项格式异常",
            error="格式异常",
            duration_ms=(time.time() - start_time) * 1000,
        )

    api_id = selected.get("apiId")
    if not api_id:
        return QueryResult(
            success=False,
            description=interface_desc,
            text="查询失败: 列表未返回 apiId",
            error="缺少 apiId",
            duration_ms=(time.time() - start_time) * 1000,
        )

    api_name = selected.get("apiName") or ""
    category = selected.get("category") or ""

    # 4. 查询详情
    try:
        detail_path = bi.get("detail_path", "").replace("{apiId}", str(api_id))
        detail_resp = _execute_http_request(
            cfg,
            HttpRequest(method="GET", path=detail_path),
            use_cache=opts.use_cache,
        )
    except Exception as e:
        return QueryResult(
            success=False,
            description=interface_desc,
            text=f"查询失败: {e}",
            error=str(e),
            duration_ms=(time.time() - start_time) * 1000,
        )

    detail_node = detail_resp.body
    if not _code_ok(detail_node, detail_ok):
        msg = detail_node.get("message") or detail_node.get("msg") or "未知错误"
        return QueryResult(
            success=False,
            description=interface_desc,
            text=f"查询接口详情失败: {msg}",
            error=msg,
            duration_ms=(time.time() - start_time) * 1000,
        )

    detail_data = detail_node.get("data")
    if not isinstance(detail_data, dict):
        return QueryResult(
            success=False,
            description=interface_desc,
            text="查询失败: 详情 data 非对象",
            error="详情格式异常",
            duration_ms=(time.time() - start_time) * 1000,
        )

    # 5. 构建输出
    friendly = build_ai_friendly_structure(detail_data)

    # 6. 特征组详情查询（Step 7）
    # 递归解析 characteristic 类型的字段，查询特征组详情并补充子字段结构
    if cfg.get("characteristic") and api_id:
        input_params = friendly.get("入参列表", [])
        if input_params:
            friendly["入参列表"] = enrich_characteristic_fields(input_params, cfg, api_id)

    hints = split_interface_input_hints(interface_input_hints or "")
    if hints:
        friendly = enrich_with_input_hints(friendly, hints)

    # 7. 格式化输出
    out_parts: List[str] = []
    out_parts.append("找到匹配接口:\n")
    out_parts.append(f"- 接口名称: {api_name}\n")
    out_parts.append(f"- 分类: {category}\n")
    out_parts.append(f"- 匹配方式: {match_reason}\n")

    if hints:
        out_parts.append("=== 从用户话术梳理的入参关注点 ===\n")
        for h in hints:
            out_parts.append(f"- {h}\n")
        out_parts.append("\n")

        matched = friendly.get("与关注点匹配的入参")
        if isinstance(matched, list) and matched:
            out_parts.append("=== 与上述关注点匹配的接口入参（元数据）===\n")
            out_parts.append(json.dumps(matched, ensure_ascii=False, indent=2))
            out_parts.append("\n\n")
        else:
            note = friendly.get("与关注点匹配的入参说明")
            if note:
                out_parts.append(f"{note}\n\n")

    out_parts.append("=== 接口详细信息（AI友好格式）===\n\n")
    out_parts.append(json.dumps(friendly, ensure_ascii=False, indent=2))

    return QueryResult(
        success=True,
        description=interface_desc,
        text="".join(out_parts),
        api_id=str(api_id),
        api_name=api_name,
        duration_ms=(time.time() - start_time) * 1000,
    )


def query_multiple_parallel(
    cfg: dict,
    descriptions: List[str],
    hints: Optional[str] = None,
    options: Optional[QueryOptions] = None,
    *,
    explicit_api_names: Optional[List[Optional[str]]] = None,
    explicit_indices: Optional[List[Optional[int]]] = None,
) -> List[QueryResult]:
    """
    并行执行多个接口查询。

    Args:
        cfg: 配置字典
        descriptions: 接口描述列表
        hints: 入参关注点（共享）
        options: 查询选项
        explicit_api_names: 每个描述对应的精确 apiName 列表
        explicit_indices: 每个描述对应的精确索引列表

    Returns:
        按输入顺序排列的查询结果列表
    """
    opts = options or QueryOptions()
    results: List[Optional[QueryResult]] = [None] * len(descriptions)

    # 确保参数列表长度一致
    api_names = explicit_api_names or [None] * len(descriptions)
    indices = explicit_indices or [None] * len(descriptions)

    with ThreadPoolExecutor(max_workers=opts.max_workers) as executor:
        futures = {
            executor.submit(
                query_single, cfg, desc,
                interface_input_hints=hints,
                options=opts,
                explicit_api_name=api_names[i],
                explicit_index=indices[i],
            ): i
            for i, desc in enumerate(descriptions)
        }

        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = QueryResult(
                    success=False,
                    description=descriptions[idx],
                    text=f"查询失败: {e}",
                    error=str(e),
                )

    return [r for r in results if r is not None]


def has_multiple_candidates(results: List[QueryResult]) -> bool:
    """检查结果中是否存在多候选情况（需要确认的）"""
    return any(r.error == "NEED_CONFIRM" for r in results)


def format_results(results: List[QueryResult]) -> str:
    """格式化多个查询结果为文本输出"""
    chunks: List[str] = []
    success_count = sum(1 for r in results if r.success)

    for i, result in enumerate(results):
        if i > 0:
            chunks.append(
                "\n\n========================================\n========================================\n\n"
            )
        chunks.append(f"【查询描述 {i + 1}/{len(results)}】: {result.description}\n\n")
        chunks.append(result.text)

    chunks.append("\n\n========================================\n")
    chunks.append(f"查询完成: 共 {len(results)} 个描述，成功 {success_count} 个")

    return "".join(chunks)


# =============================================================================
# 主入口
# =============================================================================

def main() -> int:
    require_python_version()

    configure_stdio_utf8()

    ap = argparse.ArgumentParser(description="业务接口查询（对齐 BusinessInterfaceQueryTool）")
    ap.add_argument("--config", default=str(_skill_dir() / "config.yaml"))
    ap.add_argument(
        "--all-interface-desc",
        dest="all_interface_desc",
        default="",
        help="接口描述，多个英文逗号分隔",
    )
    ap.add_argument(
        "--interface-input-hints",
        dest="interface_input_hints",
        default="",
        help="从用户话术抽取的入参关注点，逗号/顿号分隔",
    )
    ap.add_argument(
        "--parallel",
        dest="parallel",
        action="store_true",
        help="启用并行查询",
    )
    ap.add_argument(
        "--max-workers",
        dest="max_workers",
        type=int,
        default=3,
        help="并行查询的最大线程数",
    )
    ap.add_argument(
        "--no-cache",
        dest="no_cache",
        action="store_true",
        help="禁用响应缓存",
    )
    ap.add_argument(
        "--clear-cache",
        dest="clear_cache",
        action="store_true",
        help="清除缓存后退出",
    )
    ap.add_argument(
        "--env-file",
        default=None,
        help="显式指定 .env 路径（默认会加载与 config 同目录下的 .env，与 iuap-c-metadata-info 一致）",
    )
    ap.add_argument(
        "--api-name",
        dest="api_name",
        default="",
        help="精确指定要查询的 apiName（支持多值，与接口描述一一对应，逗号分隔）",
    )
    ap.add_argument(
        "--api-index",
        dest="api_index",
        type=int,
        default=None,
        help="精确指定要选择的接口索引（0-based，从1开始计数）",
    )
    ap.add_argument(
        "--api-id",
        dest="api_id",
        default="",
        help="直接通过 API ID 查询接口详情（跳过列表搜索）",
    )
    ap.add_argument(
        "--confirm",
        dest="confirm",
        default="N",
        choices=["Y", "N"],
        help="需求确认模式: Y=严格确认逻辑（多匹配查所有详情），N=交互选择(默认)",
    )

    args = ap.parse_args()

    # 可选：先加载用户指定的 .env；随后 resolve_config 会再加载 config.yaml 同目录下 .env（若存在则合并）
    if args.env_file:
        env_path = Path(args.env_file).expanduser().resolve()
        load_dotenv(env_path)

    cfg_path = Path(args.config).expanduser().resolve()
    if not cfg_path.exists():
        print(f"错误: 配置文件不存在: {cfg_path}", file=sys.stderr)
        return ExitCode.CONFIG_ERROR

    try:
        cfg = resolve_config(cfg_path)
    except Exception as e:
        print(f"错误: 配置加载失败: {e}", file=sys.stderr)
        return ExitCode.CONFIG_ERROR

    # 清除缓存
    if args.clear_cache:
        _global_cache.clear()
        print("缓存已清除", file=sys.stderr)
        return ExitCode.SUCCESS

    # 如果提供了 --api-id，直接通过 API ID 查询
    api_id_raw = (args.api_id or "").strip()
    if api_id_raw:
        hints_raw = (args.interface_input_hints or "").strip()
        if not hints_raw:
            hints_raw = str((cfg.get("request") or {}).get("interface_input_hints", "")).strip()

        bi = cfg.get("business_interface") or {}
        options = QueryOptions(
            max_workers=1,
            use_cache=not args.no_cache,
            page_size=int(bi.get("page_size", 0)),
        )

        result = query_by_api_id(
            cfg,
            api_id=api_id_raw,
            interface_input_hints=hints_raw,
            options=options,
        )

        print(result.text)

        if result.success:
            return ExitCode.SUCCESS
        else:
            return ExitCode.UNKNOWN_ERROR

    # 解析描述
    desc = (args.all_interface_desc or "").strip()
    if not desc:
        desc = str((cfg.get("request") or {}).get("allInterfaceDesc", "")).strip()

    if not desc:
        print(
            "错误：请提供业务接口描述信息（--all-interface-desc 或 config.request.allInterfaceDesc）",
            file=sys.stderr,
        )
        return ExitCode.VALIDATION_ERROR

    hints_raw = (args.interface_input_hints or "").strip()
    if not hints_raw:
        hints_raw = str((cfg.get("request") or {}).get("interface_input_hints", "")).strip()

    parts = [p.strip() for p in desc.split(",") if p.strip()]

    # 解析 api-name 参数（逗号分隔，与 parts 一一对应）
    api_name_raw = (args.api_name or "").strip()
    api_names = [n.strip() for n in api_name_raw.split(",")] if api_name_raw else [None] * len(parts)

    # 如果只有一个 api-name 且有多个描述，则只用于第一个
    if len(api_names) == 1 and len(parts) > 1:
        api_names = [api_names[0]] + [None] * (len(parts) - 1)
    # 如果 api-name 数量不足，补 None
    elif len(api_names) < len(parts):
        api_names = api_names + [None] * (len(parts) - len(api_names))

    # 解析 api-index 参数（1-based，与表格序号一致）
    explicit_index = args.api_index

    # 解析 confirm 参数
    confirm_mode = args.confirm == "Y"

    # 构建选项（从配置读取 page_size）
    bi = cfg.get("business_interface") or {}
    options = QueryOptions(
        max_workers=args.max_workers if args.parallel else 1,
        use_cache=not args.no_cache,
        page_size=int(bi.get("page_size", 0)),  # 0 表示不限制
        confirm_mode=confirm_mode,
    )

    # 需求确认模式下不启用交互选择
    if confirm_mode:
        # 直接批量查询，不等待用户选择
        results = [
            query_single(
                cfg, p,
                interface_input_hints=hints_raw,
                options=options,
                explicit_api_name=api_names[i] if i < len(api_names) else None,
                explicit_index=explicit_index,
                confirm_mode=confirm_mode,
            )
            for i, p in enumerate(parts)
        ]
    elif args.parallel and len(parts) > 1:
        results = query_multiple_parallel(
            cfg, parts, hints_raw, options,
            explicit_api_names=api_names,
            explicit_indices=[explicit_index] * len(parts) if explicit_index is not None else [None] * len(parts),
        )
    else:
        results = [
            query_single(
                cfg, p,
                interface_input_hints=hints_raw,
                options=options,
                explicit_api_name=api_names[i] if i < len(api_names) else None,
                explicit_index=explicit_index,
                confirm_mode=confirm_mode,
            )
            for i, p in enumerate(parts)
        ]

    # 检查是否存在需要用户选择的候选（NEED_SELECT）
    need_select_results = [r for r in results if r.error == "NEED_SELECT"]
    if need_select_results:
        # 直接展示候选列表，由调用方负责用户交互
        for result in results:
            if result.error == "NEED_SELECT" and result.candidates:
                print(result.text)
                print()
        return ExitCode.USER_CANCEL

    # 输出
    text = format_results(results)
    print(text)

    # 写入文件
    out_cfg = cfg.get("output") or {}
    if out_cfg.get("write_result_json", False):
        out_dir = _skill_dir() / (cfg.get("paths") or {}).get("output_dir", "output")
        out_dir.mkdir(parents=True, exist_ok=True)
        fn = out_cfg.get("result_json_filename", "business_interface_last.json")

        payload = {
            "text_result": text,
            "descriptions": parts,
            "interface_input_hints": split_interface_input_hints(hints_raw),
            "success_count": sum(1 for r in results if r.success),
            "results": [
                {
                    "description": r.description,
                    "success": r.success,
                    "api_id": r.api_id,
                    "api_name": r.api_name,
                    "error": r.error,
                    "duration_ms": round(r.duration_ms, 2),
                }
                for r in results
            ],
        }

        (out_dir / fn).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    return ExitCode.SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
