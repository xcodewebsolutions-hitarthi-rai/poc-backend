"""
旗舰版元数据 HTTP 调用与 JSON 解析，对齐 Java：
- BusinessObjectMetadataParser
- BusinessObjectToolUtil（getBusinessObjectInfo 主流程与 bizTableGroupToMap）
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Windows Git Bash 下强制 UTF-8 输出
if sys.stdout.isatty() or (hasattr(sys.stdout, 'reconfigure') and sys.platform == 'win32'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from iuap_common.bip_auth import http_get_json

logger = logging.getLogger(__name__)

try:
    from metadata_fast_lookup import (
        FastLookupResult,
        MetadataFastLookup,
        get_fast_lookup,
        unique_uris_in_results,
    )
    _FAST_LOOKUP_ENABLED = True
except ImportError:
    _FAST_LOOKUP_ENABLED = False
    unique_uris_in_results = None  # type: ignore

# 与 Java ENTITY_CACHE 类似的简单 TTL 缓存（按 uri）
_uri_cache: Dict[str, Tuple[str, float]] = {}
_uri_lock = threading.Lock()

# 默认缓存 TTL（秒），可通过 cache.ttl_seconds 配置覆盖
_URI_TTL_DEFAULT = 30 * 60

# 默认并发数
_DEFAULT_MAX_WORKERS = 16


def _get_max_workers(cfg: dict) -> int:
    """从配置中获取并发数"""
    concurrency = cfg.get("concurrency") or {}
    return int(concurrency.get("max_workers", _DEFAULT_MAX_WORKERS))


def _get_uri_ttl(cfg: dict) -> float:
    """从配置中获取 URI 缓存 TTL"""
    cache_cfg = cfg.get("cache") or {}
    if cache_cfg.get("enabled", True):
        return float(cache_cfg.get("ttl_seconds", _URI_TTL_DEFAULT))
    return _URI_TTL_DEFAULT


def _cache_get(uri: str) -> Optional[str]:
    """从缓存获取URI响应"""
    with _uri_lock:
        entry = _uri_cache.get(uri)
        if entry is None:
            return None
        body, expire_time = entry
        if time.monotonic() > expire_time:
            del _uri_cache[uri]
            return None
        return body


def _cache_put(uri: str, body: str, ttl: float) -> None:
    """存入URI响应到缓存，指定TTL秒"""
    with _uri_lock:
        expire_time = time.monotonic() + ttl
        _uri_cache[uri] = (body, expire_time)


def _text(node: Any, field: str) -> Optional[str]:
    if not isinstance(node, dict):
        return None
    v = node.get(field)
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _first_non_empty(*values: Optional[str]) -> Optional[str]:
    for v in values:
        if v is not None and str(v).strip():
            return v
    return None


@dataclass
class EnumItem:
    code: Optional[str] = None
    name: Optional[str] = None


@dataclass
class AttributeInfo:
    display_name: Optional[str] = None
    db_column_name: Optional[str] = None
    type: Optional[str] = None
    name: Optional[str] = None
    enums: Optional[List[EnumItem]] = None
    uri: Optional[str] = None
    table_name: Optional[str] = None


@dataclass
class BizTableGroup:
    bill_name: Optional[str] = None
    domain: Optional[str] = None
    table_name: Optional[str] = None
    uri: Optional[str] = None  # 新增：用于判断是否是关联关系表
    attributes: List[AttributeInfo] = field(default_factory=list)


@dataclass
class CandidateInfo:
    """searchByName 多义时的一个候选项"""
    code: Optional[str] = None        # 元数据编码（唯一标识，用于用户选择）
    name: Optional[str] = None        # 名称
    id: Optional[str] = None          # boId（业务对象ID）
    appName: Optional[str] = None     # 应用名称
    appCode: Optional[str] = None     # 应用编码
    # 以下字段保留用于兼容性
    parent_code: Optional[str] = None  # 父级业务对象 code（如 RBSM）
    parent_name: Optional[str] = None  # 父级业务对象名称（如 报账服务）


@dataclass
class BusinessObjectCodeNameResult:
    code: Optional[str] = None
    name: Optional[str] = None
    id: Optional[str] = None          # boId
    appName: Optional[str] = None     # 应用名称
    appCode: Optional[str] = None    # 应用编码
    # 父级业务对象名称，用于判断用户查询的是主表还是子表
    parent_name: Optional[str] = None
    # 多义时返回完整候选列表（包含 code + name + id + appName + appCode）
    candidates: Optional[List[CandidateInfo]] = None
    # URI 查询时，包含从 children[] 收集的所有 URI（主表+子表+孙表）
    all_uris: Optional[List[str]] = None

    def needs_user_selection(self) -> bool:
        return bool(self.candidates)

    def is_main_table_query(self, billname: str) -> bool:
        """判断用户查询的是否为主表（而不是子表）"""
        # 如果 parent_name 等于 billname，说明用户查询的是主表
        # 如果 parent_name 不等于 billname，说明用户查询的是子表
        return bool(self.parent_name and self.parent_name == billname)


    # ========== 新增字段：主表/子表判断 ==========
    # True=主表查询（需要遍历所有 children URI），False=子表查询（只查询精确匹配的 URI）
    is_main_table: bool = False
    # 精确匹配的 URI（子表查询时使用）
    matched_uri: Optional[str] = None
    # 所有子 URI（主表查询时使用，包含主表+子表+孙表）
    all_child_uris: List[str] = field(default_factory=list)

    def needs_user_selection(self) -> bool:
        return bool(self.candidates)

    def is_main_table_query(self, billname: str) -> bool:
        """判断用户查询的是否为主表（而不是子表）"""
        # 优先使用新增的 is_main_table 字段
        if hasattr(self, 'is_main_table') and self.is_main_table:
            return True
        # 兼容旧逻辑：如果 parent_name 等于 billname，说明用户查询的是主表
        return bool(self.parent_name and self.parent_name == billname)



@dataclass
class EntityDetailResult:
    entity_id: Optional[str] = None
    uri: Optional[str] = None
    bo_id: Optional[str] = None
    business_object_code: Optional[str] = None
    parent_child_relationship: Optional[int] = None  # 1=主表, 2=子表, 3=孙表, 4=曾孙表
    parent_uri: Optional[str] = None  # 父实体的 URI
    is_descendant_of_sub: bool = False  # 是否是子表的后代（用于子表查询）


@dataclass
class BusinessPropertySummary:
    name: Optional[str] = None
    display_name: Optional[str] = None
    uri: Optional[str] = None
    table_name: Optional[str] = None


@dataclass
class EntityModelForAI:
    uri: Optional[str] = None
    table_name: Optional[str] = None
    business_object_code: Optional[str] = None
    domain: Optional[str] = None
    schema: Optional[str] = None
    business_properties: List[BusinessPropertySummary] = field(default_factory=list)
    foreign_keys: List[str] = field(default_factory=list)


def _has_term_code(node: Dict[str, Any], code: str) -> bool:
    terms = node.get("terms")
    if not isinstance(terms, list):
        return False
    for t in terms:
        if isinstance(t, dict) and _text(t, "code") == code:
            return True
    return False


def _score_bill_candidate(node: Dict[str, Any]) -> int:
    if not isinstance(node, dict):
        return -1
    tn = _text(node, "tableName")
    if not tn or not str(tn).strip():
        return -1
    score = 0
    if _has_term_code(node, "isMain"):
        score += 20
    if "businessObjectId" in node:
        score += 6
    ca = node.get("codeAttribute")
    if isinstance(ca, dict):
        score += 5
    ka = node.get("keyAttribute")
    if isinstance(ka, dict):
        score += 3
    if _first_non_empty(_text(node, "displayName"), _text(node, "title")):
        score += 2
    if _text(node, "domain"):
        score += 1
    return score


def _find_best_bill_node(node: Any) -> Tuple[Optional[Dict[str, Any]], int]:
    best: Optional[Dict[str, Any]] = None
    best_score = -1

    def walk(n: Any) -> None:
        nonlocal best, best_score
        if isinstance(n, dict):
            sc = _score_bill_candidate(n)
            if sc > best_score:
                best_score = sc
                best = n
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(node)
    return best, best_score


def _find_first_text_by_field_name(node: Any, field_name: str) -> Optional[str]:
    if isinstance(node, dict):
        v = node.get(field_name)
        if isinstance(v, str):
            return v
        for ch in node.values():
            found = _find_first_text_by_field_name(ch, field_name)
            if found:
                return found
    elif isinstance(node, list):
        for ch in node:
            found = _find_first_text_by_field_name(ch, field_name)
            if found:
                return found
    return None


def _find_first_object_by_meta_type_and_uri(
    node: Any, meta_type: str, uri: str
) -> Optional[Dict[str, Any]]:
    if isinstance(node, dict):
        if _text(node, "metaType") == meta_type and _text(node, "uri") == uri:
            return node
        for ch in node.values():
            found = _find_first_object_by_meta_type_and_uri(ch, meta_type, uri)
            if found:
                return found
    elif isinstance(node, list):
        for ch in node:
            found = _find_first_object_by_meta_type_and_uri(ch, meta_type, uri)
            if found:
                return found
    return None


def _parse_enum_items(attribute_node: Dict[str, Any]) -> Optional[List[EnumItem]]:
    items_node = None
    type_node = attribute_node.get("type")
    if isinstance(type_node, dict):
        items_node = type_node.get("items")
    if items_node is None:
        items_node = attribute_node.get("items")
    if not isinstance(items_node, list):
        return None
    out: List[EnumItem] = []
    for item_node in items_node:
        if not isinstance(item_node, dict):
            continue
        code = _text(item_node, "code")
        name = _text(item_node, "name")
        if (not code or not str(code).strip()) and (not name or not str(name).strip()):
            continue
        out.append(EnumItem(code=code, name=name))
    return out or None


def _parse_type_text(attribute_node: Dict[str, Any]) -> Optional[str]:
    tu = _text(attribute_node, "typeUri")
    if tu and str(tu).strip():
        return tu
    type_node = attribute_node.get("type")
    if type_node is None:
        return _text(attribute_node, "type")
    if isinstance(type_node, str):
        return type_node
    if isinstance(type_node, dict):
        n = _text(type_node, "name")
        if n and str(n).strip():
            return n
        tid = _text(type_node, "id")
        if tid and str(tid).strip():
            return tid
    return None


def _parse_attribute_node(
    attribute_node: Optional[Dict[str, Any]],
) -> Optional[AttributeInfo]:
    if not isinstance(attribute_node, dict):
        return None
    assoc = attribute_node.get("association")
    if isinstance(assoc, dict):
        return None
    db_column_name = _first_non_empty(
        _text(attribute_node, "columnName"), _text(attribute_node, "fieldName")
    )
    if not db_column_name or not str(db_column_name).strip():
        return None
    ai = AttributeInfo()
    ai.display_name = _first_non_empty(
        _text(attribute_node, "displayName"), _text(attribute_node, "title")
    )
    ai.db_column_name = db_column_name
    ai.type = _parse_type_text(attribute_node)
    ai.name = _text(attribute_node, "name")
    ai.enums = _parse_enum_items(attribute_node)
    biztype = _text(attribute_node, "biztype")
    dtmt = _text(attribute_node, "dataTypeMetaType")
    type_uri = _text(attribute_node, "typeUri")
    name = _text(attribute_node, "name")
    if biztype == "quote" and type_uri:
        ai.uri = type_uri
    if dtmt == "Class" and name == "headParallel":
        pass  # handled in process_attribute_node
    elif dtmt == "elastic":
        pass
    elif dtmt == "Class" and name != "headParallel":
        ai.type = "参照类型，此处为参照对象id，long类型，需使用toString()"
    return ai


def _add_to_table(
    table_to_attributes: Dict[str, List[AttributeInfo]],
    dedupe: Set[str],
    table_name: str,
    attribute_info: Optional[AttributeInfo],
    add_first: bool,
) -> None:
    if attribute_info is None:
        return
    tn = table_name or ""
    key = f"{tn}|{attribute_info.db_column_name}|{attribute_info.name}"
    if key in dedupe:
        return
    dedupe.add(key)
    lst = table_to_attributes.setdefault(tn, [])
    if add_first:
        lst.insert(0, attribute_info)
    else:
        lst.append(attribute_info)


def _process_attribute_nodes(
    attributes_node: Any,
    table_to_attributes: Dict[str, List[AttributeInfo]],
    dedupe: Set[str],
    main_table_name: str,
    fetch_json_fn,
    parse_fn,
) -> None:
    if not isinstance(attributes_node, list):
        return
    for attribute_node in attributes_node:
        if isinstance(attribute_node, dict):
            _process_attribute_node(
                attribute_node,
                table_to_attributes,
                dedupe,
                main_table_name,
                fetch_json_fn,
                parse_fn,
            )


def _process_attribute_node(
    attribute_node: Dict[str, Any],
    table_to_attributes: Dict[str, List[AttributeInfo]],
    dedupe: Set[str],
    main_table_name: str,
    fetch_json_fn,
    parse_fn,
) -> None:
    data_type_meta_type = _text(attribute_node, "dataTypeMetaType")
    type_uri = _text(attribute_node, "typeUri")
    name = _text(attribute_node, "name")
    biztype = _text(attribute_node, "biztype")

    if data_type_meta_type == "elastic":
        _process_elastic_attribute(
            type_uri,
            attribute_node,
            name,
            main_table_name,
            table_to_attributes,
            dedupe,
            fetch_json_fn,
            parse_fn,
        )
    elif data_type_meta_type == "Class" and name == "headParallel":
        _process_parallel_attribute(
            type_uri,
            name,
            main_table_name,
            table_to_attributes,
            dedupe,
            fetch_json_fn,
            parse_fn,
        )
    else:
        attribute_info = _parse_attribute_node(attribute_node)
        if attribute_info is None:
            return
        if biztype == "quote" and type_uri:
            attribute_info.uri = type_uri
        table_name = _text(attribute_node, "tableName")
        if not table_name or not str(table_name).strip():
            table_name = main_table_name
        _add_to_table(table_to_attributes, dedupe, table_name, attribute_info, False)


def _process_elastic_attribute(
    type_uri: Optional[str],
    attribute_node: Dict[str, Any],
    name: Optional[str],
    main_table_name: str,
    table_to_attributes: Dict[str, List[AttributeInfo]],
    dedupe: Set[str],
    fetch_json_fn,
    parse_fn,
) -> None:
    if not type_uri:
        return
    elastic_json = fetch_json_fn(type_uri)
    if not elastic_json:
        return
    try:
        elastic_groups = parse_fn(elastic_json)
        for elastic_group in elastic_groups:
            elastic_attrs = elastic_group.attributes or []
            prefixed: List[AttributeInfo] = []
            for ea in elastic_attrs:
                na = AttributeInfo()
                na.display_name = ea.display_name
                na.db_column_name = ea.db_column_name
                na.type = ea.type
                na.name = f"{name}.{ea.name}" if name else ea.name
                na.enums = ea.enums
                prefixed.append(na)
            elastic_table_name = elastic_group.table_name or (f"{main_table_name}_{name}")
            table_to_attributes[elastic_table_name] = prefixed
        main_attrs = table_to_attributes.setdefault(main_table_name, [])
        display_name = _text(attribute_node, "displayName")
        column_name = _text(attribute_node, "columnName")
        elastic_group_attr = AttributeInfo()
        elastic_group_attr.display_name = display_name
        elastic_group_attr.db_column_name = column_name
        elastic_group_attr.type = "Long"
        elastic_group_attr.name = name
        main_attrs.append(elastic_group_attr)
    except Exception as e:
        logger.warning("解析特征属性数据失败: %s", e, exc_info=True)


def _process_parallel_attribute(
    type_uri: Optional[str],
    name: Optional[str],
    main_table_name: str,
    table_to_attributes: Dict[str, List[AttributeInfo]],
    dedupe: Set[str],
    fetch_json_fn,
    parse_fn,
) -> None:
    if not type_uri:
        return
    parallel_json = fetch_json_fn(type_uri)
    if not parallel_json:
        return
    try:
        parallel_groups = parse_fn(parallel_json)
        for parallel_group in parallel_groups:
            parallel_attrs = parallel_group.attributes or []
            prefixed: List[AttributeInfo] = []
            for pa in parallel_attrs:
                na = AttributeInfo()
                na.display_name = pa.display_name
                na.db_column_name = pa.db_column_name
                na.type = pa.type
                na.name = f"{name}.{pa.name}" if name else pa.name
                na.enums = pa.enums
                prefixed.append(na)
            parallel_table_name = parallel_group.table_name or (f"{main_table_name}_{name}")
            table_to_attributes[parallel_table_name] = prefixed
    except Exception as e:
        logger.warning("解析平行表数据失败: %s", e, exc_info=True)


def parse_metadata_json(
    json_str: str,
    *,
    fetch_json_fn=None,
    _depth: int = 0,
) -> List[BizTableGroup]:
    """
    解析实体 DB 信息 JSON 为 BizTableGroup 列表。
    fetch_json_fn(uri) 用于 elastic/parallel 子查询；主入口应传入真实 fetch。
    """
    if _depth > 50:
        return []
    if fetch_json_fn is None:
        def fetch_json_fn(_uri: str) -> Optional[str]:  # type: ignore
            return None
    root = json.loads(json_str)
    data_node = root.get("data") if isinstance(root.get("data"), dict) else root
    if not isinstance(data_node, dict):
        data_node = root if isinstance(root, dict) else {}

    bill_node, _ = _find_best_bill_node(data_node)
    if bill_node is None:
        bill_node = data_node

    class_uri = _first_non_empty(
        _text(bill_node.get("codeAttribute") if isinstance(bill_node.get("codeAttribute"), dict) else {}, "classUri"),
        _text(bill_node.get("keyAttribute") if isinstance(bill_node.get("keyAttribute"), dict) else {}, "classUri"),
        _find_first_text_by_field_name(bill_node, "classUri"),
        _find_first_text_by_field_name(data_node, "classUri"),
    )

    class_node = None
    if class_uri:
        class_node = _find_first_object_by_meta_type_and_uri(data_node, "Class", class_uri)
        if class_node is None:
            class_node = _find_first_object_by_meta_type_and_uri(root, "Class", class_uri)

    bill_name = _first_non_empty(
        _text(bill_node, "displayName"),
        _text(bill_node, "title"),
        _text(class_node, "displayName") if class_node else None,
        _text(class_node, "title") if class_node else None,
        _text(data_node, "displayName"),
        _text(data_node, "title"),
    )

    main_table_name = _first_non_empty(
        _text(bill_node, "tableName"),
        _text(class_node, "tableName") if class_node else None,
        _text(data_node, "tableName"),
    )

    domain = _first_non_empty(
        _text(class_node, "domain") if class_node else None,
        _text(bill_node, "domain"),
        _text(data_node, "domain"),
    )

    # 获取实体的 URI，用于判断是否是关联关系表
    entity_uri = _first_non_empty(
        _text(bill_node, "uri"),
        _text(class_node, "uri") if class_node else None,
        _text(data_node, "uri"),
    )

    attributes_node = (
        class_node.get("attributes") if class_node else None
    ) or bill_node.get("attributes")
    association_attributes_node = (
        class_node.get("associationAttributes") if class_node else None
    ) or bill_node.get("associationAttributes")

    table_to_attributes: Dict[str, List[AttributeInfo]] = {}
    dedupe: Set[str] = set()

    def inner_fetch(uri: str) -> Optional[str]:
        j = fetch_json_fn(uri)
        return j

    def inner_parse(js: str) -> List[BizTableGroup]:
        return parse_metadata_json(js, fetch_json_fn=fetch_json_fn, _depth=_depth + 1)

    ka = bill_node.get("keyAttribute") if isinstance(bill_node.get("keyAttribute"), dict) else None
    ca = bill_node.get("codeAttribute") if isinstance(bill_node.get("codeAttribute"), dict) else None
    key_attr = _parse_attribute_node(ka) if ka else None
    code_attr = _parse_attribute_node(ca) if ca else None
    _add_to_table(table_to_attributes, dedupe, main_table_name or "", key_attr, True)
    _add_to_table(table_to_attributes, dedupe, main_table_name or "", code_attr, True)

    _process_attribute_nodes(
        attributes_node,
        table_to_attributes,
        dedupe,
        main_table_name or "",
        inner_fetch,
        inner_parse,
    )
    _process_attribute_nodes(
        association_attributes_node,
        table_to_attributes,
        dedupe,
        main_table_name or "",
        inner_fetch,
        inner_parse,
    )

    groups: List[BizTableGroup] = []
    for tname, attrs in table_to_attributes.items():
        g = BizTableGroup()
        g.bill_name = bill_name
        g.domain = domain
        g.table_name = tname
        g.uri = entity_uri  # 新增：设置 URI 用于判断关联关系表
        g.attributes = attrs
        groups.append(g)
    return groups


def _api_success_code(root: Any) -> bool:
    if not isinstance(root, dict):
        return False
    c = root.get("code")
    if c is None:
        c = root.get("resultCode")
    return str(c).strip() == "200"


def parse_bo_identity_from_entity_db_json(json_str: str) -> Optional[BusinessObjectCodeNameResult]:
    """
    从 queryByUri（实体库表）JSON 中解析 businessObjectCode / businessObjectId，
    用于 isIncludeSub=Y 且侧栏选定 queryUri 时走 byboid 全量实体（含子表）链路。

    提取优先级（适配不同版本的 API 返回格式）：
    1. data.data.businessObjectCode / businessObjectId（顶层）
    2. data.data.suppliers[metaType=Aggregate].businessObjectCode / businessObjectId
    3. 递归搜索兜底（_find_first_text_by_field_name）
    """
    if not json_str:
        return None
    try:
        root = json.loads(json_str)
    except Exception:
        return None
    if not isinstance(root, dict) or not _api_success_code(root):
        return None
    data_node = root.get("data")
    if not isinstance(data_node, dict):
        return None
    inner = data_node.get("data")
    if isinstance(inner, dict):
        data_node = inner

    # 1. 尝试从顶层提取
    code = _text(data_node, "businessObjectCode")
    bo_id = _first_non_empty(
        _text(data_node, "businessObjectId"),
        _text(data_node, "boId"),
    )

    # 2. 如果顶层缺失，尝试从 suppliers[metaType=Aggregate] 提取
    code_s = str(code).strip() if code else ""
    id_s = str(bo_id).strip() if bo_id else ""
    if code_s in ("null",):
        code_s = ""
    if id_s in ("null",):
        id_s = ""

    if not code_s or not id_s:
        suppliers = data_node.get("suppliers", [])
        if isinstance(suppliers, list):
            for item in suppliers:
                if not isinstance(item, dict):
                    continue
                meta_type = str(item.get("metaType") or "").strip()
                if meta_type == "Aggregate":
                    agg_code = _text(item, "businessObjectCode")
                    agg_id = _text(item, "businessObjectId")
                    if not code_s and agg_code and str(agg_code).strip() not in ("null", ""):
                        code_s = str(agg_code).strip()
                    if not id_s and agg_id and str(agg_id).strip() not in ("null", ""):
                        id_s = str(agg_id).strip()
                    break  # 找到第一个 Aggregate 就停止

    # 3. 如果仍然缺失，用递归搜索兜底
    if not code_s or code_s in ("null",):
        code_s = _find_first_text_by_field_name(root, "businessObjectCode") or ""
    if not id_s or id_s in ("null",):
        id_s = _first_non_empty(
            _find_first_text_by_field_name(root, "businessObjectId"),
            _find_first_text_by_field_name(root, "boId"),
        ) or ""

    code_s = code_s.strip()
    id_s = id_s.strip()
    if code_s in ("null",):
        code_s = ""
    if id_s in ("null",):
        id_s = ""
    if not code_s and not id_s:
        return None
    return BusinessObjectCodeNameResult(
        code=code_s or None,
        id=id_s or None,
        name=None,
    )


def parse_business_object_code_name(result: Any, billname: str) -> Optional[BusinessObjectCodeNameResult]:
    """
    解析 searchByName API 响应，返回业务对象编码与 ID。

    优先级规则（根据用户需求）：
    1. 优先使用 BUSINESSOBJECT（如果有值）
    2. BUSINESSOBJECT 为空时才使用 METACLASS

    策略：
    1. 精确匹配（name == search_name）：收集所有匹配项
       - 单一匹配 → 直接返回
       - 多个匹配 → 返回 candidates 列表（needs_user_selection=True）
    2. 模糊匹配（search_name in name）：同上
    3. 无任何匹配 → 返回 None

    支持子表名称直接查询：
    - 用户输入子表名称（如"通用报销单子表"）
    - 从 METACLASS.children 中匹配子表
    - 获取主表的 id（boId）和 code
    """
    if not isinstance(result, dict) or not _api_success_code(result):
        return None
    data_node = result.get("data")
    if not isinstance(data_node, dict):
        return None
    search_name = (billname or "").strip()

    def make_candidate(code: str, name: str, nid: str,
                       app_name: Optional[str] = None,
                       app_code: Optional[str] = None,
                       parent_name: Optional[str] = None) -> CandidateInfo:
        return CandidateInfo(
            code=code, name=name, id=nid,
            appName=app_name, appCode=app_code,
            parent_code=code, parent_name=parent_name
        )

    def _match_and_collect(
        nodes: List[Tuple[Dict[str, Any], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]],
        exact: bool
    ) -> List[CandidateInfo]:
        """从节点列表中匹配并收集候选项"""
        matches: List[CandidateInfo] = []
        for node, parent_code, parent_name, parent_bo_id, app_name, app_code in nodes:
            node_name = _text(node, "name") or ""
            # 精确匹配：name == search_name
            # 模糊匹配：search_name in name
            if exact:
                if node_name == search_name:
                    pass  # 继续处理
                else:
                    continue
            else:
                if search_name and search_name in node_name:
                    pass  # 继续处理
                else:
                    continue

            nid = parent_bo_id  # 用 METACLASS.boId 而非 child 节点的 entity id
            if parent_code and parent_code != "null":
                matches.append(make_candidate(
                    parent_code, node_name or search_name, nid,
                    app_name, app_code, parent_name
                ))
        return matches

    def _build_result(m: CandidateInfo) -> BusinessObjectCodeNameResult:
        """构建返回结果，包含 parent_name"""
        return BusinessObjectCodeNameResult(
            code=m.code,
            name=m.name,
            id=m.id,
            appName=m.appName,
            appCode=m.appCode,
            parent_name=m.parent_name
        )

    # ============================================================
    # 优先使用 BUSINESSOBJECT
    # ============================================================
    bo_arr = data_node.get("BUSINESSOBJECT")
    bo_nodes: List[Tuple[Dict[str, Any], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]] = []
    if isinstance(bo_arr, list) and bo_arr:
        for bo in bo_arr:
            if not isinstance(bo, dict):
                continue
            app_name = _text(bo, "appName")
            app_code = _text(bo, "appCode")
            bo_name = _text(bo, "name")
            children = bo.get("children")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        child_name = _text(child, "name")
                        child_code = _text(child, "code")
                        child_id = _text(child, "id")  # BUSINESSOBJECT.children.id = boId
                        bo_nodes.append((child, child_code, bo_name, child_id, app_name, app_code))

    # 从 BUSINESSOBJECT 中精确匹配
    exact_matches_bo = _match_and_collect(bo_nodes, exact=True)
    if len(exact_matches_bo) == 1:
        return _build_result(exact_matches_bo[0])
    if len(exact_matches_bo) > 1:
        return BusinessObjectCodeNameResult(candidates=exact_matches_bo)

    # 从 BUSINESSOBJECT 中模糊匹配
    fuzzy_matches_bo = _match_and_collect(bo_nodes, exact=False)
    if len(fuzzy_matches_bo) == 1:
        return _build_result(fuzzy_matches_bo[0])
    if len(fuzzy_matches_bo) > 1:
        return BusinessObjectCodeNameResult(candidates=fuzzy_matches_bo)

    # ============================================================
    # BUSINESSOBJECT 为空或不匹配，使用 METACLASS
    # ============================================================
    metaclass_arr = data_node.get("METACLASS")
    mc_nodes: List[Tuple[Dict[str, Any], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]] = []
    if isinstance(metaclass_arr, list) and metaclass_arr:
        for mc in metaclass_arr:
            if not isinstance(mc, dict):
                continue
            parent_code = _text(mc, "code")
            parent_name = _text(mc, "name")
            parent_bo_id = _text(mc, "id")  # METACLASS.id = boId
            app_name = _text(mc, "appName")
            app_code = _text(mc, "appCode")
            children = mc.get("children")
            
            # ========== 新增：主表/子表判断逻辑 ==========
            # 先检查是否是主表查询（METACLASS.name == billname）
            if parent_name and parent_name == search_name:
                # 主表查询：收集所有 children 的 URI
                all_child_uris = []
                if isinstance(children, list):
                    for child in children:
                        if isinstance(child, dict):
                            child_uri = _text(child, "uri")
                            if child_uri:
                                all_child_uris.append(child_uri)
                return BusinessObjectCodeNameResult(
                    code=parent_code,
                    name=parent_name,
                    id=parent_bo_id,
                    appName=app_name,
                    appCode=app_code,
                    is_main_table=True,
                    all_child_uris=all_child_uris
                )
            
            # 子表查询：在 children 中匹配
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        mc_nodes.append((child, parent_code, parent_name, parent_bo_id, app_name, app_code))

    # 从 METACLASS.children 中精确匹配（子表查询）
    exact_matches_mc = _match_and_collect(mc_nodes, exact=True)
    if len(exact_matches_mc) == 1:
        # 子表查询：使用精确匹配的 child URI
        result = _build_result(exact_matches_mc[0])
        if result:
            result.is_main_table = False
            # 找到精确匹配的 child URI
            for node, parent_code, parent_name, parent_bo_id, app_name, app_code in mc_nodes:
                child_name = _text(node, "name")
                if child_name == search_name:
                    child_uri = _text(node, "uri")
                    if child_uri:
                        result.matched_uri = child_uri
                        break
        return result
    if len(exact_matches_mc) > 1:
        return BusinessObjectCodeNameResult(candidates=exact_matches_mc)

    # 从 METACLASS.children 中模糊匹配
    fuzzy_matches_mc = _match_and_collect(mc_nodes, exact=False)
    if len(fuzzy_matches_mc) == 1:
        result = _build_result(fuzzy_matches_mc[0])
        if result:
            result.is_main_table = False
        return result
    if len(fuzzy_matches_mc) > 1:
        return BusinessObjectCodeNameResult(candidates=fuzzy_matches_mc)

    return None


def _collect_entity_details(entities_array: Any, out: List[EntityDetailResult], depth: int = 0, parent_uri: Optional[str] = None) -> None:
    """
    递归收集实体详情，支持子表和孙表层级。

    depth: 当前层级深度
    - depth=0: 主表（root=true 或 parentChildRelationship=1）
    - depth=1: 子表（parentChildRelationship=2）
    - depth=2: 孙表（parentChildRelationship=3 及更深层级）

    parent_uri: 父实体的 URI（用于追踪父子关系）

    收集规则：根据用户需求，收集所有子表和孙表的 URI
    - parentChildRelationship=1: 主表
    - parentChildRelationship=2: 子表
    - parentChildRelationship=3: 孙表
    - 没有 parentChildRelationship 但有 children: 孙表层级继续递归
    """
    if not isinstance(entities_array, list):
        return
    if depth > 10:  # 防止无限递归
        return

    for entity in entities_array:
        if not isinstance(entity, dict):
            continue

        # 获取 parentChildRelationship 判断层级
        # 1=主表, 2=子表, 3=孙表
        pcr = entity.get("parentChildRelationship")
        if pcr is None:
            pcr = 1  # 默认主表

        # 孙表层级（depth>=2 或 parentChildRelationship>=3）也收集
        # 但跳过 root=false 且没有有效 URI 的节点
        eid = _text(entity, "id")
        if eid in (None, "", "null"):
            eid = None
        uri = _text(entity, "uri")

        # 只收集有 URI 的实体（排除关联关系表等无 URI 的节点）
        if uri and uri.strip():
            bo_id = _text(entity, "businessObjectId")
            boc = _text(entity, "businessObjectCode")
            # 获取 parentChildRelationship
            pcr_val = entity.get("parentChildRelationship")
            out.append(
                EntityDetailResult(
                    entity_id=eid, uri=uri, bo_id=bo_id, business_object_code=boc,
                    parent_child_relationship=pcr_val, parent_uri=parent_uri
                )
            )

        # 递归处理 children（子表、孙表、孙孙表...）
        ch = entity.get("children")
        if isinstance(ch, list):
            # 孙表层级继续递归，传递当前实体 uri 作为 parent_uri
            _collect_entity_details(ch, out, depth + 1, parent_uri=uri)


def parse_entity_detail_from_by_bo_id(result: Any) -> Optional[List[EntityDetailResult]]:
    if not isinstance(result, dict) or not _api_success_code(result):
        return None
    data_node = result.get("data")
    if not isinstance(data_node, dict):
        return None
    entities_node = data_node.get("entities")
    if not isinstance(entities_node, list):
        inner = data_node.get("data")
        if isinstance(inner, dict):
            entities_node = inner.get("entities")
    if not isinstance(entities_node, list):
        return None
    out: List[EntityDetailResult] = []
    _collect_entity_details(entities_node, out)
    return out or None


def parse_detail_by_entity_id_response(result: Any) -> Optional[EntityModelForAI]:
    if not isinstance(result, dict) or not _api_success_code(result):
        return None
    data_node = result.get("data")
    if not isinstance(data_node, dict):
        return None
    inner_data = data_node.get("data")
    if isinstance(inner_data, dict):
        data_node = inner_data

    model = EntityModelForAI()
    entity_uri = _text(data_node, "uri")
    model.uri = entity_uri
    model.table_name = _text(data_node, "tableName")
    model.business_object_code = _text(data_node, "businessObjectCode")
    domain = _text(data_node, "domain")
    model.domain = domain
    schema = _text(data_node, "schema")
    if (not schema or not str(schema).strip()) and domain and str(domain).startswith("c-"):
        schema = domain.replace("-", "_") + "_db"
    model.schema = schema

    props: List[BusinessPropertySummary] = []
    bp_array = data_node.get("businessProperties")
    if isinstance(bp_array, list):
        for bp in bp_array:
            if not isinstance(bp, dict):
                continue
            summary = BusinessPropertySummary()
            summary.name = _text(bp, "name")
            summary.display_name = _text(bp, "displayName")
            summary.uri = _text(bp, "uri")
            all_tables = bp.get("allTables")
            if isinstance(all_tables, list) and len(all_tables) > 0:
                ft = all_tables[0]
                summary.table_name = str(ft) if not isinstance(ft, dict) else None
            else:
                summary.table_name = None
            props.append(summary)
            cf_arr = bp.get("characterFields")
            if isinstance(cf_arr, list):
                for cf in cf_arr:
                    if not isinstance(cf, dict):
                        continue
                    cfs = BusinessPropertySummary()
                    cfs.name = _text(cf, "name")
                    cfs.display_name = _text(cf, "displayName")
                    cfs.uri = _text(cf, "uri")
                    cft = cf.get("allTables")
                    if isinstance(cft, list) and len(cft) > 0:
                        cfs.table_name = str(cft[0])
                    props.append(cfs)
    model.business_properties = props

    foreign_keys: List[str] = []
    assoc_attrs = data_node.get("associationAttributes")
    if isinstance(assoc_attrs, list) and entity_uri:
        for attr in assoc_attrs:
            if not isinstance(attr, dict):
                continue
            association_node = attr.get("association")
            if isinstance(association_node, dict):
                type_b_uri = _text(association_node, "typeBUri")
                role_b = _text(association_node, "roleB")
                if entity_uri == type_b_uri and role_b:
                    foreign_keys.append(role_b)
    model.foreign_keys = foreign_keys
    return model


def _fetch_entity_db_info_json_impl(cfg: dict, uri: str) -> Optional[str]:
    meta = cfg.get("metadata_api") or {}
    path = meta.get("query_by_uri_path") or ""
    if not path or not uri:
        return None
    cached = _cache_get(uri)
    if cached is not None:
        return cached
    try:
        payload = http_get_json(cfg, path, {"uri": uri})
        if isinstance(payload, dict):
            body = json.dumps(payload, ensure_ascii=False)
            ttl = _get_uri_ttl(cfg)
            _cache_put(uri, body, ttl)
            return body
    except Exception:
        return None
    return None


def fetch_business_object_by_name(cfg: dict, billname: str) -> Optional[BusinessObjectCodeNameResult]:
    meta = cfg.get("metadata_api") or {}
    path = meta.get("search_by_name_path") or ""
    if not path:
        return None
    try:
        payload = http_get_json(cfg, path, {"key": billname.strip()})
        return parse_business_object_code_name(payload, billname)
    except Exception:
        return None


def fetch_business_object_by_uri(cfg: dict, uri: str) -> Optional[BusinessObjectCodeNameResult]:
    """
    用 URI 查询 searchByName，返回 METACLASS 及其 children[] 中的所有 URI。

    返回的 BusinessObjectCodeNameResult 包含：
    - id: boId
    - code: 元数据编码
    - name: 名称
    - appName: 应用名称
    - appCode: 应用编码
    - all_uris: 从 children[] 递归收集的所有 URI（主表+子表+孙表）
    """
    meta = cfg.get("metadata_api") or {}
    path = meta.get("search_by_name_path") or ""
    if not path:
        return None
    try:
        payload = http_get_json(cfg, path, {"key": uri.strip()})
        return parse_business_object_by_uri_response(payload, uri)
    except Exception:
        return None


def collect_all_uris_from_node(node: Dict[str, Any]) -> List[str]:
    """
    递归收集节点及其所有 children[] 中的 URI（主表+子表+孙表）。
    """
    uris: List[str] = []
    if not isinstance(node, dict):
        return uris

    # 收集当前节点的 URI
    uri = _text(node, "uri")
    if uri and uri.strip():
        uris.append(uri.strip())

    # 递归处理 children
    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                uris.extend(collect_all_uris_from_node(child))

    return uris


def collect_all_uris_from_metaclass(metaclass_node: Dict[str, Any]) -> List[str]:
    """
    从 METACLASS 节点中递归收集所有 URI（主表+子表+孙表）。
    METACLASS.children[].uri = 主表/子表 URI
    """
    if not isinstance(metaclass_node, dict):
        return []
    children = metaclass_node.get("children")
    if not isinstance(children, list):
        return []
    uris: List[str] = []
    for child in children:
        if isinstance(child, dict):
            uris.extend(collect_all_uris_from_node(child))
    return uris


def parse_business_object_by_uri_response(result: Any, key: str) -> Optional[BusinessObjectCodeNameResult]:
    """
    解析用 URI 或 code 查询 searchByName 的响应。

    key 可能是：
    - URI: 如 "cmp.fundpayment.FundPayment"
    - code: 如 "ctm-cmp.cmp_fundpayment"

    返回格式：
    - id: boId
    - code: 元数据编码
    - name: 名称
    - appName: 应用名称
    - appCode: 应用编码
    - all_uris: 从 children[] 递归收集的所有 URI
    """
    if not isinstance(result, dict) or not _api_success_code(result):
        return None

    data_node = result.get("data")
    if not isinstance(data_node, dict):
        return None

    # 判断 key 是 URI 还是 code
    # URI 格式通常是 "domain.entity.EntityName"
    # code 格式通常是 "domain.code" 或纯 code
    is_uri = "." in key and "/" not in key

    # 优先从 METACLASS 查找（URI 查询时）
    metaclass_arr = data_node.get("METACLASS")
    if isinstance(metaclass_arr, list):
        for mc in metaclass_arr:
            if not isinstance(mc, dict):
                continue
            all_uris = collect_all_uris_from_metaclass(mc)
            if key in all_uris:
                # 提取基本信息
                code = _text(mc, "code")
                name = _text(mc, "name")
                bo_id = _text(mc, "id")
                app_name = _text(mc, "appName")
                app_code = _text(mc, "appCode")
                # URI 查询时默认是主表查询
                return BusinessObjectCodeNameResult(
                    code=code, name=name, id=bo_id,
                    appName=app_name, appCode=app_code,
                    is_main_table=True,
                    all_child_uris=all_uris if all_uris else [],
                    all_uris=all_uris if all_uris else None
                )

    # 从 BUSINESSOBJECT 查找（code 查询时）
    # 在 BUSINESSOBJECT 结构中：
    # - BUSINESSOBJECT.id = 业务对象 ID
    # - BUSINESSOBJECT.children[].id = METACLASS ID（boId）
    # - BUSINESSOBJECT.children[].code = 元数据编码
    bo_arr = data_node.get("BUSINESSOBJECT")
    if isinstance(bo_arr, list):
        for bo in bo_arr:
            if not isinstance(bo, dict):
                continue
            children = bo.get("children")
            if not isinstance(children, list):
                continue
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_code = _text(child, "code")
                # code 匹配
                if child_code == key:
                    # children[].id 才是 boId
                    bo_id = _text(child, "id")  # METACLASS id = boId
                    code = child_code
                    name = _text(child, "name")
                    app_name = _text(bo, "appName")
                    app_code = _text(bo, "appCode")
                    return BusinessObjectCodeNameResult(
                        code=code, name=name, id=bo_id,
                        appName=app_name, appCode=app_code,
                        is_main_table=True,  # code 查询时默认是主表查询
                        all_child_uris=[],  # 需要后续调用 getEntityListByBOId 获取
                        all_uris=None
                    )

    # 如果从 METACLASS 找到了，取第一个
    if isinstance(metaclass_arr, list) and metaclass_arr:
        mc = metaclass_arr[0]
        code = _text(mc, "code")
        name = _text(mc, "name")
        bo_id = _text(mc, "id")
        app_name = _text(mc, "appName")
        app_code = _text(mc, "appCode")
        all_uris = collect_all_uris_from_metaclass(mc)
        return BusinessObjectCodeNameResult(
            code=code, name=name, id=bo_id,
            appName=app_name, appCode=app_code,
            is_main_table=True,
            all_child_uris=all_uris if all_uris else [],
            all_uris=all_uris if all_uris else None
        )

    return None


def fetch_entity_detail_by_bo_id(cfg: dict, cn: BusinessObjectCodeNameResult) -> Optional[List[EntityDetailResult]]:
    meta = cfg.get("metadata_api") or {}
    path = meta.get("entity_list_by_bo_id_path") or ""
    if not path or not cn.id:
        return None
    q: Dict[str, str] = {
        "boId": cn.id or "",
        "businessObjectCode": cn.code or "",
    }
    # OpenAPI 示例含 version，默认 2；部分环境缺省可能导致实体树不完整
    ver = meta.get("entity_list_by_bo_id_version")
    q["version"] = str(ver).strip() if ver is not None and str(ver).strip() else "2"
    try:
        payload = http_get_json(cfg, path, q)
        return parse_entity_detail_from_by_bo_id(payload)
    except Exception:
        return None


def fetch_detail_by_entity_id(cfg: dict, ed: EntityDetailResult) -> Optional[EntityModelForAI]:
    meta = cfg.get("metadata_api") or {}
    path = meta.get("entity_info_by_entity_id_path") or ""
    if not path or not ed.entity_id:
        return None
    try:
        payload = http_get_json(
            cfg,
            path,
            {
                "entityId": ed.entity_id or "",
                "uri": ed.uri or "",
                "boId": ed.bo_id or "",
                "businessObjectCode": ed.business_object_code or "",
            },
        )
        return parse_detail_by_entity_id_response(payload)
    except Exception:
        return None


def parse_foreign_keys_from_json(
    json_str: str, _entity_uri: Optional[str] = None
) -> List[Dict[str, Any]]:
    """对齐 Java parseForeignKeysFromJson（quote 类型外键列与 refUri）。"""
    if not json_str:
        return []
    try:
        root = json.loads(json_str)
    except Exception:
        return []
    if not isinstance(root, dict) or not _api_success_code(root):
        return []
    data_node = root.get("data")
    if not isinstance(data_node, dict):
        return []
    inner = data_node.get("data")
    if isinstance(inner, dict):
        data_node = inner
    assoc_attrs = data_node.get("associationAttributes")
    if not isinstance(assoc_attrs, list):
        return []
    out: List[Dict[str, Any]] = []
    for attr in assoc_attrs:
        if not isinstance(attr, dict):
            continue
        biztype = str(attr.get("biztype", "")).replace('"', "")
        if biztype != "quote":
            continue
        column_name = attr.get("columnName")
        type_uri = attr.get("typeUri")
        cn = str(column_name).replace('"', "") if column_name is not None else None
        tu = str(type_uri).replace('"', "") if type_uri is not None else None
        if cn or tu:
            out.append({"columnName": cn, "refUri": tu})
    return out


def _unwrap_entity_db_data_node(root: dict) -> Optional[dict]:
    if not isinstance(root, dict) or not _api_success_code(root):
        return None
    data_node = root.get("data")
    if not isinstance(data_node, dict):
        return None
    inner = data_node.get("data")
    if isinstance(inner, dict):
        return inner
    return data_node


def composition_child_uris_from_entity_db_json(json_str: str) -> List[str]:
    """
    从 queryByUri 返回体中解析组合（composition）子实体 URI。
    与 BusinessObjectToolUtilV2.buildChildEntities 一致：association.type == composition 时取属性 typeUri。
    部分环境子表不在 getEntityListByBOId 的 children 树里，仅出现在父实体的 associationAttributes。

    支持两种来源：
    1. associationAttributes 中的 composition 类型
    2. children 数组中的子实体
    """
    if not json_str:
        return []
    try:
        root = json.loads(json_str)
    except Exception:
        return []
    data_node = _unwrap_entity_db_data_node(root)
    if not isinstance(data_node, dict):
        return []
    out: List[str] = []
    seen: Set[str] = set()

    # 来源1：从 associationAttributes 中解析 composition 子实体
    assoc_attrs = data_node.get("associationAttributes")
    if isinstance(assoc_attrs, list):
        for attr in assoc_attrs:
            if not isinstance(attr, dict):
                continue
            assoc = attr.get("association")
            if not isinstance(assoc, dict):
                continue
            if (_text(assoc, "type") or "").strip().lower() != "composition":
                continue
            tu = _text(attr, "typeUri")
            if tu and str(tu).strip() and tu not in seen:
                seen.add(tu)
                out.append(str(tu).strip())

    # 来源2：从 children 数组中解析子实体 URI
    children = data_node.get("children")
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, dict):
                continue
            # 子实体的 URI 在 uri 字段中
            child_uri = _text(child, "uri")
            if child_uri and str(child_uri).strip() and child_uri not in seen:
                seen.add(child_uri)
                out.append(str(child_uri).strip())

    return out


def get_parent_entity_uri(json_str: str) -> Optional[str]:
    """
    从实体 JSON 中获取父实体（主表）的 URI。
    子表实体的 JSON 中通常包含 parentEntity 或 parentUri 字段引用主表。
    """
    if not json_str:
        return None
    try:
        root = json.loads(json_str)
    except Exception:
        return None
    data_node = _unwrap_entity_db_data_node(root)
    if not isinstance(data_node, dict):
        return None

    # 检查 parentUri 字段
    parent_uri = data_node.get("parentUri")
    if parent_uri and str(parent_uri).strip():
        return str(parent_uri).strip()

    # 检查 parentEntity 字段（可能是 URI 或对象）
    parent_entity = data_node.get("parentEntity")
    if parent_entity:
        if isinstance(parent_entity, str):
            return parent_entity.strip()
        if isinstance(parent_entity, dict):
            # parentEntity 可能是对象，包含 uri 或其他标识字段
            uri = parent_entity.get("uri")
            if uri:
                return str(uri).strip()

    return None


def is_main_table_relation(entity_json: str, main_uri: str) -> bool:
    """
    判断关联关系表是否是"主表关联表"（只能随主表查询返回）。

    判断逻辑：
    1. 解析关联表的 associationAttributes（biztype == quote）
    2. 如果存在非 childid/itemid 的外键引用主表实体（如 mainid -> main_uri），则为"主表关联表"

    例如：资金付款单与第三方关联关系表 有 mainid -> cmp.fundpayment.FundPayment（主表）
          所以它是"主表关联表"，只能随主表查询返回
    """
    if not entity_json or not main_uri:
        return False
    try:
        root = json.loads(entity_json)
    except Exception:
        return False
    data_node = _unwrap_entity_db_data_node(root)
    if not isinstance(data_node, dict):
        return False

    # 检查是否有关联到主表的外键（不是 childid/itemid）
    # childid/itemid 通常是子表的外键，不算"主表关联"的判断依据
    main_uri_lower = main_uri.strip().lower()

    # 检查 associationAttributes 中的 biztype == quote 类型外键
    assoc_attrs = data_node.get("associationAttributes")
    if isinstance(assoc_attrs, list):
        for attr in assoc_attrs:
            if not isinstance(attr, dict):
                continue
            # 检查是否是 quote 类型外键
            biztype = str(attr.get("biztype", "")).strip()
            if biztype != "quote":
                continue
            # 获取外键引用的实体 URI（typeUri）
            type_uri = (attr.get("typeUri") or "").strip().lower()
            if not type_uri:
                continue
            # 跳过 childid 和 itemid 这类子表外键
            column_name = (attr.get("columnName") or "").lower()
            if "childid" in column_name or "itemid" in column_name:
                continue
            # 检查是否引用主表
            if type_uri == main_uri_lower:
                return True

    return False


def _load_scheme_list(cfg: dict) -> List[Dict[str, Any]]:
    paths = cfg.get("paths") or {}
    raw = paths.get("scheme_info_json") or "reference/scheme-info.json"
    base = Path(__file__).resolve().parent.parent
    p = Path(raw)
    if not p.is_absolute():
        p = base / p
    if not p.is_file():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def get_scheme_by_domain(cfg: dict, domain: Optional[str]) -> Optional[str]:
    if not domain or not str(domain).strip():
        return None
    target_key = "mdd_schema." + str(domain).strip()
    for scheme in _load_scheme_list(cfg):
        if not isinstance(scheme, dict):
            continue
        if scheme.get("key") == target_key:
            ev = scheme.get("exclusiveValue")
            if ev and str(ev).strip():
                return str(ev).strip()
            v = scheme.get("value")
            return str(v).strip() if v else None
    return None


def get_refer_scheme(cfg: dict, domain: Optional[str]) -> str:
    s = get_scheme_by_domain(cfg, domain)
    return s if s else "scheme"


def _attr_to_map(
    attr: AttributeInfo,
    group: BizTableGroup,
    entity_result: EntityDetailResult,
    entity_model: Optional[EntityModelForAI],
    property_table_name_map: Dict[str, str],
    uri_to_ref_json: Dict[str, str],
    field_filter: List[str],
    need_ref: bool,
    table_template: Optional[str],
    cfg: dict,
) -> Optional[Dict[str, Any]]:
    if field_filter:
        dn = attr.display_name
        if not dn or dn not in field_filter:
            return None
    am: Dict[str, Any] = {
        "name": attr.name,
        "displayName": attr.display_name,
        "dbColumnName": attr.db_column_name,
        "type": attr.type,
    }
    attr_table = property_table_name_map.get(attr.name or "")
    if attr_table is None and attr.display_name:
        attr_table = property_table_name_map.get(attr.display_name)
    if not attr_table:
        attr_table = group.table_name
    am["tableName"] = attr_table
    attr_uri = attr.uri
    if attr_uri:
        am["uri"] = attr_uri
        if need_ref:
            ref_json = uri_to_ref_json.get(attr_uri)
            if ref_json:
                try:
                    groups = parse_metadata_json(ref_json, fetch_json_fn=lambda u: None)
                    if groups:
                        ref_group = groups[0]
                        ref_attrs: List[Dict[str, Any]] = []
                        for ra in ref_group.attributes or []:
                            an = ra.name
                            adn = ra.display_name
                            if an in ("name", "code") or (
                                table_template and adn and adn in table_template
                            ):
                                ref_attrs.append(
                                    {
                                        "dbColumnName": ra.db_column_name,
                                        "displayName": ra.display_name,
                                    }
                                )
                        domain = ref_group.domain
                        sch = get_refer_scheme(cfg, domain)
                        if not sch and domain and str(domain).startswith("c-"):
                            sch = str(domain).replace("-", "_") + "_db"
                        am["referenceStructure"] = {
                            "billName": ref_group.bill_name,
                            "domain": domain,
                            "tableName": ref_group.table_name,
                            "attributes": ref_attrs,
                            "scheme": sch,
                        }
                except Exception:
                    pass
    if attr.enums:
        am["enums"] = [{"code": e.code, "name": e.name} for e in attr.enums]
    return am


def biz_table_group_to_map(
    cfg: dict,
    group: BizTableGroup,
    entity_result: EntityDetailResult,
    entity_model: Optional[EntityModelForAI],
    foreign_keys: List[Dict[str, Any]],
    doc_fields: Optional[str],
    is_sql: str,
    table_template: Optional[str],
    batch_fetcher,
) -> Optional[Dict[str, Any]]:
    field_filter = [
        x.strip()
        for x in (doc_fields or "").split(",")
        if x and str(x).strip()
    ]
    need_ref = str(is_sql).strip().upper() == "Y"
    property_table_name_map: Dict[str, str] = {}
    if entity_model and entity_model.business_properties:
        for prop in entity_model.business_properties:
            if prop.table_name:
                if prop.name:
                    property_table_name_map[prop.name] = prop.table_name  # type: ignore
                if prop.display_name:
                    property_table_name_map[prop.display_name] = prop.table_name  # type: ignore

    uris_to_fetch: Set[str] = set()
    if need_ref and group.attributes:
        for attr in group.attributes:
            if field_filter and (not attr.display_name or attr.display_name not in field_filter):
                continue
            if attr.uri:
                uris_to_fetch.add(attr.uri)
    uri_to_ref_json: Dict[str, str] = {}
    if need_ref and uris_to_fetch:
        uri_to_ref_json = batch_fetcher(uris_to_fetch)

    attrs_out: List[Dict[str, Any]] = []
    if group.attributes:
        for attr in group.attributes:
            m = _attr_to_map(
                attr,
                group,
                entity_result,
                entity_model,
                property_table_name_map,
                uri_to_ref_json,
                field_filter,
                need_ref,
                table_template,
                cfg,
            )
            if m:
                attrs_out.append(m)

    out: Dict[str, Any] = {
        "tableName": group.table_name,
        "billName": group.bill_name,
        "domain": group.domain,
        "uri": entity_result.uri,
        "businessObjectCode": entity_result.business_object_code,
        "schema": entity_model.schema if entity_model else None,
        "attributes": attrs_out,
    }
    if foreign_keys:
        out["foreignKeys"] = foreign_keys
    return out


def _format_candidates_ambiguous(intro: str, billname: str, candidates: List[CandidateInfo]) -> str:
    """
    格式化多义词候选列表，返回停止提示。
    展示 code + name + appName + appCode + id，帮助用户精确选择。
    """
    if not candidates:
        return intro + f"错误: 单据名称 {billname!r} 命中多个业务对象但无法获取候选信息"
    parts = [
        f"\n停止：{billname!r} 命中 {len(candidates)} 个同名业务对象，请确认使用哪个：",
        "\n候选：\n"
    ]
    for i, c in enumerate(candidates, 1):
        app_info = ""
        if c.appName and c.appCode:
            app_info = f" (应用: {c.appName}/{c.appCode})"
        elif c.appName:
            app_info = f" (应用: {c.appName})"
        parts.append(
            f"  {i}) name: {c.name or ''}  code: {c.code or ''}  id: {c.id or ''}{app_info}\n"
        )
    parts.append("\n请选择对应的 code（如输入 \"ctm-cmp.cmp_fundpayment\"）后重新调用。\n")
    return intro + "".join(parts)


def _format_fast_lookup_ambiguous(
    intro: str, billname: str, fast_results: List["FastLookupResult"]
) -> str:
    """
    本地索引命中同一单据名/键但 URI 不同，避免合并多条元数据；引导用户用 queryUri 重试。
    """
    out: List[str] = [intro, "\n", f"停止：{billname!r} 在本地元数据索引中命中多条不同 URI，必须选定其一，否则结果会混单出错。\n请用户或 Agent 在下次查询的 request 中设置 queryUri 为下面某一条 uri，亦可保留 allbillname 作展示用。\n", "\n候选：\n"]
    seen: Set[str] = set()
    n = 0
    for fr in fast_results:
        u = (fr.uri or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        n += 1
        out.append(
            f"  {n}) uri: {u}\n     metadataName: {fr.metadata_name or ''}  "
            f"bizName: {fr.biz_name or ''}  domain: {fr.domain or ''}  tableName: {fr.table_name or ''}\n"
        )
    out.append(
        "\n（再次调用示例：request 中传 \"queryUri\": \"<所选 uri>\" ）\n"
    )
    return "".join(out)


def get_business_object_info(cfg: dict, params: Dict[str, Any], use_fast_lookup: bool = True) -> str:
    """
    对齐 BusinessObjectToolUtil.getBusinessObjectInfo。
    params: allbillname, queryUri(可选，歧义时用户选定后重试), isIncludeSub, docFields, isDescField, isSQL, tableTemplate(optional), modulename(ignored)
    use_fast_lookup: 是否优先使用快速查找（默认True）
    isIncludeSub=Y：走 byboid 扁平化后的多实体 queryByUri；queryUri 续查时从锚点 uri 解析 boId；索引多 URI 时仍先输出候选。
    isIncludeSub=N：可用本地索引 + 单次 queryByUri（queryUri 直查）。

    输出格式（便于外部解析）:
    - 前缀文本行（可忽略）
    - __METADATA_JSON_START__
    - {JSON数据}
    - __METADATA_JSON_END__
    - footer 说明（可选）
    """
    lines: List[str] = ["\n业务对象属性信息如下:\n"]
    q_raw = params.get("queryUri") or params.get("query_uri")
    query_uri_param = str(q_raw).strip() if q_raw is not None else ""

    allbillname = params.get("allbillname")
    allbillname_str = str(allbillname).strip() if allbillname is not None else ""
    if not query_uri_param and not allbillname_str:
        return lines[0] + "错误: 请提供 allbillname（中文单据/元数据名称）或 queryUri（元数据实体 uri）"

    is_include_sub_obj = params.get("isIncludeSub")
    doc_fields = params.get("docFields")
    if str(params.get("isDescField", "Y")).strip().upper() == "N":
        doc_fields = None
    is_sql = str(params.get("isSQL", "N"))
    table_template = params.get("tableTemplate")

    is_include_sub = bool(
        is_include_sub_obj
        and str(is_include_sub_obj).strip().upper() not in ("", "N")
    )

    def uri_fetch(uri: str) -> Optional[str]:
        return _fetch_entity_db_info_json_impl(cfg, uri)

    def batch_fetch(uris: Set[str]) -> Dict[str, str]:
        """并发拉取多个 URI，max_workers 由配置控制（默认 8）"""
        if not uris:
            return {}
        workers = _get_max_workers(cfg)
        out: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(uri_fetch, u): u for u in uris}
            for fut in as_completed(futures):
                u = futures[fut]
                try:
                    j = fut.result()
                    if j:
                        out[u] = j
                except Exception:
                    pass
        return out

    def append_entities_from_fast_results(
        fr_list: List["FastLookupResult"],
        billname: str,
        *,
        relax_billname_filter: bool,
    ) -> None:
        nonlocal entities_list, fast_lookup_used
        if not fr_list:
            return
        # 同 uri 多行索引只处理一次
        fr_by_uri: Dict[str, "FastLookupResult"] = {}
        for fr in fr_list:
            u = (fr.uri or "").strip()
            if u and u not in fr_by_uri:
                fr_by_uri[u] = fr
        fast_lookup_used = True
        for fr in fr_by_uri.values():
            if not fr.uri:
                continue
            entity_db_json = uri_fetch(fr.uri)
            if not entity_db_json:
                continue

            def fetch_fn(u: str) -> Optional[str]:
                return uri_fetch(u)

            biz_groups = parse_metadata_json(entity_db_json, fetch_json_fn=fetch_fn)
            if not biz_groups:
                continue

            for group in biz_groups:
                if not is_include_sub:
                    gn = group.bill_name
                    if not gn:
                        continue
                    if not relax_billname_filter:
                        # 快速查找模式下，使用 fr 中的 metadata_name/biz_name（来自索引）进行过滤
                        # 因为 API 返回的 displayName 可能与 metadata_lookup.json 中的 metadataName 不一致
                        # 例如：索引中 metadataName="资金付款单"，但 API 返回 displayName="资金付款主表"

                        # 提取 billname 中的核心业务名称（移除参照翻译等额外关键词）
                        billname_keywords_to_remove = ["参照翻译", "id转code", "id_to_code", "id2code", "翻译", "id转换", "id编码", "转code"]
                        core_billname = billname
                        for kw in billname_keywords_to_remove:
                            core_billname = core_billname.replace(kw, "").strip()

                        # 使用核心业务名称进行匹配
                        index_name = (fr.metadata_name or "") or (fr.biz_name or "")
                        index_match = (
                            index_name == core_billname
                            or core_billname in index_name
                            or index_name in core_billname
                        )
                        # API 返回的名称也可能匹配（兜底兼容）
                        api_match = (
                            gn == core_billname
                            or core_billname in gn
                            or gn in core_billname
                        )
                        if not (index_match or api_match):
                            continue

                if fr.domain and not group.domain:
                    group.domain = fr.domain

                fk_json = parse_foreign_keys_from_json(entity_db_json, fr.uri)

                entity_result = EntityDetailResult(
                    uri=fr.uri,
                    entity_id=None,
                    bo_id=None,
                    business_object_code=None
                )

                ent_map = biz_table_group_to_map(
                    cfg,
                    group,
                    entity_result,
                    None,
                    fk_json,
                    str(doc_fields) if doc_fields else None,
                    is_sql,
                    str(table_template) if table_template else None,
                    batch_fetch,
                )
                if ent_map:
                    if fr.schema:
                        ent_map["schema"] = fr.schema
                    if fr.domain:
                        ent_map["domain"] = fr.domain
                    entities_list.append(ent_map)

    entities_list: List[Dict[str, Any]] = []
    fast_lookup_used = False

    billnames = [b.strip() for b in allbillname_str.split(",") if b.strip()]

    def append_from_details(details: List[EntityDetailResult], billname: str, anchor_uri: str = "", anchor_domain: str = "", anchor_json: str = "", anchor_valid_uris: Optional[Set[str]] = None, is_sub_table_query: bool = False, relation_table_uris: Optional[Set[str]] = None, main_uri: str = "", is_relation_table_query: bool = False, is_descendant_only_query: bool = False) -> None:
        """
        处理 entityListByBOId 返回的实体列表。
        anchor_uri: 锚点实体的 URI，用于确保锚点自身始终被处理。
        anchor_domain: 锚点实体的 domain，用于过滤——只保留 domain 匹配的实体，
                      排除共享同一 boId 但 domain 不同的客开扩展实体。
        anchor_json: 锚点实体的 queryByUri 响应 JSON（用于处理父级实体不在 details 中的情况）。
        anchor_valid_uris: 本地索引中有记录的 URI 集合，用于过滤——只保留索引中有记录的实体，
                          排除 API 返回但索引中没有的关联关系表等额外实体。
        is_sub_table_query: 是否为子表查询。如果是子表查询，返回子表和其孙表。
        relation_table_uris: 关联关系表的 URI 集合，用于补充过滤——确保关联表不会被名称过滤误删。
        main_uri: 主表实体的 URI，用于判断关联表是否是"主表关联表"（只能随主表查询返回）。
        is_relation_table_query: 是否为关联关系表查询。如果是，只返回锚点实体本身。
        is_descendant_only_query: 是否为孙表或更深层级查询。如果是，只返回锚点实体本身。
        """
        seen_uris: Set[str] = set()
        # 确保锚点实体总是被处理
        if anchor_uri:
            seen_uris.add(anchor_uri.strip())

        for er in details:
            if er is None:
                continue
            u = (er.uri or "").strip()
            if u:
                seen_uris.add(u)

        def fetch_fn(u: str) -> Optional[str]:
            return uri_fetch(u)

        def _entity_domain_matches(entity_db_json: str, ref_domain: str) -> bool:
            """
            判断实体的 domain 是否与参考 domain 匹配。
            匹配规则：entity_domain == ref_domain（完全相等）。
            用于过滤掉共享同一 boId 但属于不同 domain 的客开扩展实体。
            """
            if not ref_domain:
                return True  # 无参考 domain 时不过滤
            try:
                root = json.loads(entity_db_json)
            except Exception:
                return False
            data_node = _unwrap_entity_db_data_node(root)
            if not isinstance(data_node, dict):
                return False
            entity_domain = str(data_node.get("domain") or "").strip()
            return entity_domain == ref_domain

        def process_one(
            entity_result: EntityDetailResult, entity_db_json: str, is_descendant_entity: bool = False
        ) -> None:
            # 域名校验：entityListByBOId 返回共享同一 boId 的所有实体（含客开扩展）。
            # 用锚点 domain 做过滤，只保留 domain 匹配的实体。
            # 锚点实体自身不受此限制（通过 anchor_uri 判断）。
            is_anchor = anchor_uri and (entity_result.uri or "").strip() == anchor_uri.strip()
            if not is_anchor and anchor_domain and not _entity_domain_matches(entity_db_json, anchor_domain):
                return
            # URI 校验：如果提供了锚点 URI 列表，只处理索引中有记录的 URI
            # 这样可以过滤掉 API 返回的但本地索引中没有的关联关系表等额外实体
            if not is_anchor and anchor_valid_uris is not None:
                entity_uri = (entity_result.uri or "").strip()
                if entity_uri and entity_uri not in anchor_valid_uris:
                    return

            entity_model = None
            if entity_result.entity_id:
                entity_model = fetch_detail_by_entity_id(cfg, entity_result)
            biz_groups = parse_metadata_json(
                entity_db_json, fetch_json_fn=fetch_fn
            )
            if not biz_groups:
                return
            for group in biz_groups:
                gn = group.bill_name
                if not gn:
                    continue

                # 判断是否是关联关系表（通过 URI 判断）
                group_uri = (group.uri or "").strip().lower() if group.uri else ""
                is_relation_table = False
                if relation_table_uris:
                    for rel_uri in relation_table_uris:
                        if rel_uri and rel_uri.strip().lower() in group_uri:
                            is_relation_table = True
                            break

                # 判断是否是"主表关联表"（只能随主表查询返回）
                # 例如：资金付款单与第三方关联关系表 有 mainid -> cmp.fundpayment.FundPayment（主表）
                # 所以它是"主表关联表"，子表查询时应排除
                # 注意：即使实体不在 relation_table_uris 中，也可能通过外键引用主表
                is_main_table_relation_entity = False
                if main_uri and is_sub_table_query:
                    # 检查实体的外键是否引用主表
                    is_main_table_relation_entity = is_main_table_relation(
                        entity_db_json, main_uri
                    )

                # 判断是否是锚点实体（查询对象本身）
                # 锚点实体应该总是被保留，即使名称不匹配或被检测为主表关联表
                is_anchor_entity = False
                if anchor_uri:
                    entity_result_uri = (entity_result.uri or "").strip().lower()
                    anchor_uri_normalized = anchor_uri.strip().lower()
                    if entity_result_uri == anchor_uri_normalized:
                        is_anchor_entity = True

                # 关联关系表查询时，只返回锚点实体（查询对象本身）
                # 主表查询时（is_sub_table_query=False），返回名称包含 billname 的实体
                # 子表查询时（is_sub_table_query=True），返回锚点实体和其 composition 子实体（关联关系表）
                if is_relation_table_query:
                    # 关联关系表查询：只返回锚点实体本身
                    # 通过 URI 精确匹配
                    if not is_anchor_entity:
                        continue
                    # 额外检查：确保 group.uri 匹配锚点 URI
                    group_uri = (group.uri or "").strip().lower()
                    anchor_uri_lower = anchor_uri.strip().lower()
                    if group_uri and group_uri != anchor_uri_lower:
                        continue
                elif is_descendant_only_query:
                    # 孙表或更深层级查询：只返回锚点实体本身
                    if not is_anchor_entity:
                        continue
                elif is_sub_table_query:
                    # 子表查询：返回锚点实体（查询对象本身）或子表的后代实体
                    # is_descendant_of_sub 标记表示该实体是子表的后代
                    if not is_anchor_entity and not is_relation_table and not entity_result.is_descendant_of_sub:
                        continue
                else:
                    # 主表查询：返回名称包含 billname 的实体 或 关联关系表 或 锚点实体
                    # 注意：递归获取的后代实体（孙表、曾孙表等）不应该再进行名称过滤
                    # 因为后代实体的名称可能不包含主表关键词（如"计税结果"不包含"付款单"）
                    if billname not in gn and gn not in billname and not is_relation_table and not is_anchor_entity and not is_descendant_entity:
                        continue
                fk_json = parse_foreign_keys_from_json(
                    entity_db_json, entity_result.uri
                )
                ent_map = biz_table_group_to_map(
                    cfg,
                    group,
                    entity_result,
                    entity_model,
                    fk_json,
                    str(doc_fields) if doc_fields else None,
                    is_sql,
                    str(table_template) if table_template else None,
                    batch_fetch,
                )
                if ent_map:
                    entities_list.append(ent_map)
            if not is_include_sub:
                return
            # 子表查询或孙表查询时不递归获取子表
            if is_sub_table_query or is_descendant_only_query:
                return
            import sys
            child_uris = composition_child_uris_from_entity_db_json(entity_db_json)
            for child_uri in child_uris:
                cu = child_uri.strip()
                if not cu or cu in seen_uris:
                    continue
                seen_uris.add(cu)
                cj = uri_fetch(cu)
                if not cj:
                    continue
                # 域名校验 composition 子实体（避免递归引入客开子实体）
                if anchor_domain and not _entity_domain_matches(cj, anchor_domain):
                    continue
                child_er = EntityDetailResult(
                    uri=cu,
                    entity_id=None,
                    bo_id=entity_result.bo_id,
                    business_object_code=entity_result.business_object_code,
                )
                # 递归调用时传递 is_descendant_entity=True，标记为后代实体
                # 后代实体不需要再进行名称过滤，应该全部包含
                process_one(child_er, cj, is_descendant_entity=True)

        # 并发拉取所有实体的 entity_db_json，再并发 process_one
        uri_to_er: Dict[str, EntityDetailResult] = {}
        for er in details:
            if er is None:
                continue
            u = (er.uri or "").strip()
            if u:
                uri_to_er[u] = er
        workers = _get_max_workers(cfg)
        uri_to_json: Dict[str, str] = {}
        if uri_to_er:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(uri_fetch, u): u for u in uri_to_er}
                for fut in as_completed(futures):
                    u = futures[fut]
                    try:
                        j = fut.result()
                        if j:
                            uri_to_json[u] = j
                    except Exception:
                        pass
        # 并发执行 process_one（其内部递归 composition 子实体）
        # 锚点实体（父级）可能不在 details 列表中，需用 anchor_json 单独处理
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {}
            for u, er in uri_to_er.items():
                json_str = uri_to_json.get(u)
                if not json_str:
                    continue
                # 含子表模式时，后代实体也需要递归获取孙表等，所以传递 is_descendant_entity=True
                futures[ex.submit(process_one, er, json_str, is_descendant_entity=is_include_sub)] = er
            # 如果锚点不在 details 中（父级实体），用 anchor_json 单独处理
            if anchor_uri and anchor_json and anchor_uri.strip() not in uri_to_er:
                anchor_er = EntityDetailResult(
                    uri=anchor_uri.strip(),
                    entity_id=None,
                    bo_id=cn_sel.id if cn_sel else None,
                    business_object_code=cn_sel.code if cn_sel else None,
                )
                futures[ex.submit(process_one, anchor_er, anchor_json)] = anchor_er
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    pass

    fast_lookup = None
    if use_fast_lookup and _FAST_LOOKUP_ENABLED:
        fast_lookup = get_fast_lookup(cfg)

    footer = (
        "\n\n实体模型字段说明: schema为schema，tableName 为表名，billName 为单据名称，domain 为领域；"
        "attributes 为属性列表，每项含 name(属性名)、displayName(显示名)、dbColumnName(数据库列名)、type(数据类型)、enums(枚举值列表)。"
        "referenceStructure 为属性字段的引用元数据结构信息"
    )

    # ============================================================
    # 流程一：使用 queryUri 查询
    # Step1: searchByName(key=queryUri) → 获取 METACLASS（含 boId 和 children[]）
    # Step2: 收集 children[] 中的所有 URI（主表+子表+孙表，递归）
    # Step3: 对每个 URI 调用 queryByUri 获取字段明细
    # ============================================================
    if query_uri_param:
        billname_for_uri = billnames[0] if billnames else (allbillname_str or "")

        # Step1: 用 searchByName 按 URI 查询获取 METACLASS
        cn_from_uri = fetch_business_object_by_uri(cfg, query_uri_param)

        all_uris: List[str] = []
        cn_sel = None
        anchor_json = ""
        anchor_domain = ""  # 提前初始化，避免备用方案中引用未定义变量

        if cn_from_uri and cn_from_uri.id:
            # Step1 成功：获得了 METACLASS 信息
            cn_sel = cn_from_uri
            # Step2: 从 all_uris 获取所有 URI（主表+子表+孙表）
            if cn_from_uri.all_uris:
                all_uris = cn_from_uri.all_uris
            else:
                # 如果 all_uris 为空，尝试递归收集
                # 先获取主表 JSON
                main_json = uri_fetch(query_uri_param)
                if main_json:
                    all_uris = [query_uri_param]
                    # 从主表 JSON 中收集 composition 子实体 URI
                    all_uris.extend(composition_child_uris_from_entity_db_json(main_json))
        else:
            # Step1 失败：URI 查不到 METACLASS，使用备用方案
            # 直接获取主表 JSON
            anchor_json = uri_fetch(query_uri_param)
            if anchor_json:
                cn_anchor = parse_bo_identity_from_entity_db_json(anchor_json)
                if cn_anchor and cn_anchor.id:
                    cn_sel = cn_anchor
                all_uris = [query_uri_param]
                # 从主表 JSON 中收集 composition 子实体 URI
                all_uris.extend(composition_child_uris_from_entity_db_json(anchor_json))

        if not cn_sel or not cn_sel.id:
            # cn_sel.id 为空时，尝试使用备用方案
            # 直接使用 all_uris 和 anchor_json 构建实体
            if all_uris and anchor_json:
                # 使用 append_entities_from_fast_results 处理
                fr_list = []
                for uri in all_uris:
                    u = (uri or "").strip()
                    if not u:
                        continue
                    fr = FastLookupResult(
                        found=True,
                        uri=u,
                        domain=anchor_domain or None,
                        schema=None,
                        metadata_name=billname_for_uri,
                        biz_name=billname_for_uri,
                    )
                    fr_list.append(fr)
                if fr_list:
                    append_entities_from_fast_results(
                        fr_list, billname_for_uri, relax_billname_filter=True
                    )
                    result_map = {"entities": entities_list}
                    lines.append("[已按 queryUri 锚定业务对象并拉取含子表的全量实体]\n")
                    lines.append("__METADATA_JSON_START__\n")
                    lines.append(json.dumps(result_map, ensure_ascii=False))
                    lines.append("\n__METADATA_JSON_END__\n")
                    lines.append(footer)
                    return "".join(lines)
            return lines[0] + "错误: 无法从 queryUri 解析 businessObjectId（boId），请检查 URI 是否正确"

        # 获取锚点 domain 用于过滤
        anchor_domain = ""
        anchor_valid_uris: Optional[Set[str]] = None

        # 尝试从本地索引获取主表和子表的信息，用于过滤关联关系表等额外实体
        if fast_lookup and fast_lookup.is_loaded:
            fast_result = fast_lookup.get_by_uri(query_uri_param)
            if fast_result:
                anchor_valid_uris = {query_uri_param}  # 从主表 URI 开始
                # 获取主表 JSON 以收集子表 URI
                if not anchor_json:
                    anchor_json = uri_fetch(query_uri_param)
                if anchor_json:
                    # 收集 composition 子实体的 URI，但只包含本地索引中有记录的 URI
                    # 这样可以过滤掉关联关系表等额外实体
                    for child_uri in composition_child_uris_from_entity_db_json(anchor_json):
                        cu = child_uri.strip()
                        if cu:
                            # 使用 fast_lookup 验证该 URI 是否在索引中
                            child_in_index = fast_lookup.get_by_uri(cu)
                            if child_in_index:
                                anchor_valid_uris.add(cu)

        if anchor_json and not anchor_domain:
            try:
                anchor_root = json.loads(anchor_json)
                anchor_data = _unwrap_entity_db_data_node(anchor_root)
                if isinstance(anchor_data, dict):
                    anchor_domain = str(anchor_data.get("domain") or "").strip()
            except Exception:
                pass

        # 如果有 boId，调用 getEntityListByBOId 获取实体列表
        # 注意：code 查询时 all_uris 为空，需要先获取实体列表
        details = None
        if cn_sel and cn_sel.id:
            details = fetch_entity_detail_by_bo_id(cfg, cn_sel)
            if details and is_include_sub:
                # 含子表：使用 append_from_details 处理所有实体
                if not anchor_json:
                    anchor_json = uri_fetch(query_uri_param)
                # anchor_valid_uris 应该包含 getEntityListByBOId 返回的所有 URI（包括子表和孙表）
                # 而不是仅仅本地索引中记录的 URI，否则会过滤掉真正的子表
                if anchor_valid_uris is None:
                    anchor_valid_uris = set()
                # 添加锚点 URI（主表）和所有 details 中的 URI
                anchor_valid_uris.add(query_uri_param)
                for d in details:
                    if d and d.uri:
                        anchor_valid_uris.add(d.uri)

                # 收集关联关系表的 URI（composition 子实体），用于补充过滤
                relation_table_uris: Set[str] = set()
                if anchor_json:
                    relation_table_uris = set(composition_child_uris_from_entity_db_json(anchor_json))

                # 判断查询对象的层级（从 details 中获取）
                # 从 details 中查找查询对象的 parent_child_relationship
                is_descendant_only_in_uri = False  # 孙表+
                is_sub_table_level_query = False    # 子表
                query_pcr = None
                if details:
                    for d in details:
                        if d and d.uri == query_uri_param:
                            query_pcr = d.parent_child_relationship
                            if query_pcr is not None:
                                if query_pcr >= 3:
                                    # 孙表或更深层级：只返回查询对象本身
                                    is_descendant_only_in_uri = True
                                elif query_pcr == 2:
                                    # 子表：返回子表 + its children
                                    is_sub_table_level_query = True
                            break

                # 根据层级处理
                if is_descendant_only_in_uri:
                    # 孙表+：只返回查询对象本身
                    append_from_details(details, billname_for_uri, anchor_uri=query_uri_param, anchor_domain=anchor_domain, anchor_json=anchor_json, anchor_valid_uris=anchor_valid_uris, relation_table_uris=set(), is_sub_table_query=False, is_descendant_only_query=True)
                    result_map = {"entities": entities_list}
                    lines.append("[已按 queryUri 锚定业务对象并拉取含子表的全量实体]\n")
                    lines.append("__METADATA_JSON_START__\n")
                    lines.append(json.dumps(result_map, ensure_ascii=False))
                    lines.append("\n__METADATA_JSON_END__\n")
                    lines.append(footer)
                    return "".join(lines)
                elif is_sub_table_level_query:
                    # 子表：返回子表 + its children
                    # relation_table_uris 应该只包含子表的直接 children（孙表+），通过 parent_uri 判断
                    relation_table_uris = set()
                    for d in details:
                        if d and d.uri and d.parent_uri == query_uri_param:
                            # 添加子表的 children (孙表+)
                            relation_table_uris.add(d.uri)
                    append_from_details(details, billname_for_uri, anchor_uri=query_uri_param, anchor_domain=anchor_domain, anchor_json=anchor_json, anchor_valid_uris=anchor_valid_uris, relation_table_uris=relation_table_uris, is_sub_table_query=True)
                    result_map = {"entities": entities_list}
                    lines.append("[已按 queryUri 锚定业务对象并拉取含子表的全量实体]\n")
                    lines.append("__METADATA_JSON_START__\n")
                    lines.append(json.dumps(result_map, ensure_ascii=False))
                    lines.append("\n__METADATA_JSON_END__\n")
                    lines.append(footer)
                    return "".join(lines)

                # 主表查询：返回所有实体（默认行为）
                append_from_details(details, billname_for_uri, anchor_uri=query_uri_param, anchor_domain=anchor_domain, anchor_json=anchor_json, anchor_valid_uris=anchor_valid_uris, relation_table_uris=relation_table_uris)
                result_map = {"entities": entities_list}
                lines.append("[已按 queryUri 锚定业务对象并拉取含子表的全量实体]\n")
                lines.append("__METADATA_JSON_START__\n")
                lines.append(json.dumps(result_map, ensure_ascii=False))
                lines.append("\n__METADATA_JSON_END__\n")
                lines.append(footer)
                return "".join(lines)

        # 不含子表 或 getEntityListByBOId 失败：使用 all_uris 或 queryUri 遍历
        # code 查询时 all_uris 为空，尝试从 details 提取 URI，或使用主表查询
        if not all_uris:
            if details:
                # 从 details 提取第一个 URI
                for d in details:
                    if d and d.uri:
                        all_uris = [d.uri]
                        break
            if not all_uris:
                # 最后尝试直接获取主表 JSON（URI 查询时有，code 查询时可能失败）
                all_uris = [query_uri_param]

        # Step3: 使用 append_entities_from_fast_results 处理 URI 列表
        # 构造 FastLookupResult 列表
        fr_list: List[FastLookupResult] = []
        for uri in all_uris:
            u = (uri or "").strip()
            if not u:
                continue
            # 域名校验
            if anchor_domain:
                uri_json = uri_fetch(u)
                if uri_json and not _entity_domain_matches(uri_json, anchor_domain):
                    continue
            fr = FastLookupResult(
                found=True,
                uri=u,
                domain=anchor_domain or None,
                schema=None,
                metadata_name=billname_for_uri,
                biz_name=billname_for_uri,
            )
            fr_list.append(fr)

        if fr_list:
            append_entities_from_fast_results(
                fr_list, billname_for_uri, relax_billname_filter=True
            )

        result_map = {"entities": entities_list}
        lines.append("[已按 queryUri 拉取元数据]\n")
        lines.append("__METADATA_JSON_START__\n")
        lines.append(json.dumps(result_map, ensure_ascii=False))
        lines.append("\n__METADATA_JSON_END__\n")
        lines.append(footer)
        return "".join(lines)

    # ============================================================
    # 流程二：不使用 queryUri，按名称查询
    # Step1: fetch_business_object_by_name(name) → 获取候选列表
    # Step2: 如果有多义词，停止让用户确认
    # Step3: 用户确认后，用 code 调用 getEntityListByBOId
    # Step4: 对每个 URI 调用 queryByUri 获取字段明细
    # ============================================================
    for billname in billnames:
        # ============================================================
        # 流程二：不使用 queryUri，按名称查询
        # Step1: fetch_business_object_by_name(name) → 获取候选列表
        # Step2: 如果有多义词，停止让用户确认
        # Step3: 用户确认后，用 code 调用 getEntityListByBOId
        # Step4: 对每个 URI 调用 queryByUri 获取字段明细
        # ============================================================

        # 提取核心业务名称（移除参照翻译等额外关键词），用于本地索引匹配和 API 查询
        billname_keywords_to_remove = ["参照翻译", "id转code", "id_to_code", "id2code", "翻译", "id转换", "id编码", "转code"]
        core_billname = billname
        is_ref_translate = False  # 是否为参照翻译查询
        for kw in billname_keywords_to_remove:
            if kw in billname:
                is_ref_translate = True
            core_billname = core_billname.replace(kw, "").strip()

        # 先用本地索引快速查找（使用核心业务名称）
        fast_results: List["FastLookupResult"] = []
        match_type = "none"
        if fast_lookup and fast_lookup.is_loaded:
            # 优先使用核心业务名称进行本地索引查找
            if core_billname:
                fast_results = fast_lookup.lookup(core_billname)
            # 如果核心名称没有命中，尝试使用原始名称
            if not fast_results and core_billname != billname:
                fast_results = fast_lookup.lookup(billname)
            # 获取匹配类型信息
            if fast_lookup.is_loaded and hasattr(fast_lookup, 'lookup_with_match_info'):
                _, match_type, _ = fast_lookup.lookup_with_match_info(core_billname or billname)

        # 输出匹配状态信息
        import sys
        uri_count = len(unique_uris_in_results(fast_results)) if fast_results else 0
        print(f"[索引匹配] 匹配类型: {match_type}, URI数量: {uri_count}", file=sys.stderr)

        # 多候选时停止让用户确认
        if is_include_sub and fast_results and uri_count > 1:
            return _format_fast_lookup_ambiguous(lines[0], billname, fast_results)

        # 本地索引命中1条标准业务实体时，直接使用本地索引，跳过 API 调用
        # 避免 API 返回的多义词候选（关联关系表等）干扰
        if fast_results and len(fast_results) == 1:
            cn_sel = None  # 初始化，避免引用未定义变量
            fr = fast_results[0]
            # 只在有标准 URI 时使用
            if fr.uri and not fr.uri.startswith(("imi.", "check.", "cpu-", "custom.")):
                # 直接使用本地索引的 URI，不再依赖 API 返回的 all_uris
                # 参照翻译查询时，只返回查询对象本身，不包含子表
                if not is_include_sub or is_ref_translate:
                    append_entities_from_fast_results(
                        fast_results, billname, relax_billname_filter=False
                    )
                else:
                    # 含子表模式：直接用本地索引的 URI，获取实体 JSON
                    # 如果使用了 query_uri_param，优先使用它
                    queried_uri = query_uri_param if query_uri_param else fr.uri
                    entity_json = uri_fetch(queried_uri)

                    # 判断是主表还是子表：
                    # - 有 composition 子实体的是主表
                    # - 没有 composition 子实体的是子表（需要查找主表 URI）
                    if entity_json:
                        composition_children = composition_child_uris_from_entity_db_json(entity_json)
                        if composition_children:
                            # 有 composition 子实体，是主表
                            main_uri = queried_uri
                            main_json = entity_json
                        else:
                            # 没有 composition 子实体，是子表，需要查找主表 URI
                            parent_uri = get_parent_entity_uri(entity_json)
                            if parent_uri:
                                main_uri = parent_uri
                            else:
                                main_uri = queried_uri
                            # 获取主表 JSON
                            main_json = uri_fetch(main_uri)
                    else:
                        main_uri = queried_uri
                        main_json = None

                    # 尝试获取 boId，以便调用 getEntityListByBOId 获取完整的实体链
                    if not cn_sel or not cn_sel.id:
                        cn_from_uri = fetch_business_object_by_uri(cfg, main_uri)
                        if cn_from_uri and cn_from_uri.id:
                            cn_sel = cn_from_uri
                            all_uris = cn_from_uri.all_uris or [main_uri]
                        else:
                            all_uris = [main_uri]
                    else:
                        # cn_sel 已设置，使用已有的 all_uris
                        all_uris = cn_sel.all_uris or [main_uri]

                    # 判断是否是子表查询、孙表查询或关联关系表查询
                    is_sub_table_q = False
                    is_descendant_only_q = False  # 孙表或更深层级查询
                    is_relation_table_q = False
                    effective_main_uri = main_uri

                    # 获取查询对象的 JSON，确定其层级
                    import sys
                    print(f"[DEBUG] queried_uri={queried_uri}, main_uri={main_uri}", file=sys.stderr)
                    queried_entity_json = uri_fetch(queried_uri) if queried_uri else None
                    if queried_entity_json:
                        try:
                            root = json.loads(queried_entity_json)
                            data = _unwrap_entity_db_data_node(root)
                            if isinstance(data, dict):
                                is_root = data.get("root")
                                parent_child_relationship = data.get("parentChildRelationship")
                                print(f"[DEBUG] is_root={is_root}, parentChildRelationship={parent_child_relationship}", file=sys.stderr)
                                if not is_root:
                                    if '关联' in billname:
                                        # 关联关系表查询：只返回查询对象本身
                                        is_relation_table_q = True
                                        effective_main_uri = queried_uri
                                    elif parent_child_relationship == 2:
                                        # 子表：返回子表和其孙表
                                        is_sub_table_q = True
                                    elif parent_child_relationship and parent_child_relationship >= 3:
                                        # 孙表或更深层级：只返回查询对象本身
                                        is_descendant_only_q = True
                        except Exception:
                            pass

                    # 如果上述逻辑没有确定类型，回退到旧的 billname 检查
                    if not is_sub_table_q and not is_relation_table_q and not is_descendant_only_q:
                        if billname and '子表' in billname and '关联' not in billname:
                            # 子表查询
                            is_sub_table_q = (queried_uri != main_uri)
                        elif billname and '关联' in billname:
                            # 关联关系表查询：使用查询对象自身，不需要获取主表的 details
                            is_relation_table_q = True
                            effective_main_uri = queried_uri  # 使用查询对象作为主 URI

                    anchor_domain = fr.domain or ""
                    if main_json:
                        try:
                            root = json.loads(main_json)
                            data = _unwrap_entity_db_data_node(root)
                            if isinstance(data, dict):
                                anchor_domain = str(data.get("domain") or "").strip() or anchor_domain
                        except Exception:
                            pass

                    # 对于关联关系表查询，不需要获取主表的 boId 和 details
                    if is_relation_table_q:
                        # 关联关系表查询：只返回查询对象本身
                        details = []
                        entity_result = EntityDetailResult(
                            uri=queried_uri,
                            entity_id=None,
                            bo_id=None,
                            business_object_code=None
                        )
                        process_one(entity_result, entity_json)
                        result_map = {"entities": entities_list}
                        lines.append("[已按本地索引锚定业务对象并拉取含子表的全量实体]\n")
                        lines.append("__METADATA_JSON_START__\n")
                        lines.append(json.dumps(result_map, ensure_ascii=False))
                        lines.append("\n__METADATA_JSON_END__\n")
                        lines.append(footer)
                        return "".join(lines)
                    # 对于孙表或更深层级查询，只返回查询对象本身
                    if is_descendant_only_q:
                        details = []
                        entity_result = EntityDetailResult(
                            uri=queried_uri,
                            entity_id=None,
                            bo_id=None,
                            business_object_code=None
                        )
                        process_one(entity_result, entity_json)
                        result_map = {"entities": entities_list}
                        lines.append("[已按本地索引锚定业务对象并拉取含子表的全量实体]\n")
                        lines.append("__METADATA_JSON_START__\n")
                        lines.append(json.dumps(result_map, ensure_ascii=False))
                        lines.append("\n__METADATA_JSON_END__\n")
                        lines.append(footer)
                        return "".join(lines)
                    elif cn_sel and cn_sel.id:
                        details = fetch_entity_detail_by_bo_id(cfg, cn_sel)
                        if details:
                            # 确保主表 URI 在 details 中（getEntityListByBOId 可能不返回主表本身）
                            main_uri_in_details = any(
                                er and er.uri == main_uri for er in details
                            )
                            if not main_uri_in_details and main_uri:
                                # 主表不在 details 中，需要添加
                                from typing import List
                                main_er = EntityDetailResult(
                                    uri=main_uri,
                                    entity_id=None,
                                    bo_id=cn_sel.id,
                                    business_object_code=cn_sel.code
                                )
                                details = [main_er] + details

                            # 构建 anchor_valid_uris，包含主表 URI、查询对象 URI 和所有 details 中的 URI
                            anchor_valid_uris = set()
                            anchor_valid_uris.add(main_uri)  # 添加主表 URI
                            if is_sub_table_q:
                                anchor_valid_uris.add(queried_uri)  # 添加查询对象 URI（子表）
                            for d in details:
                                if d and d.uri:
                                    anchor_valid_uris.add(d.uri)

                            # 收集关联关系表的 URI（composition 子实体），用于补充过滤
                            relation_table_uris: Set[str] = set()
                            if main_json and not is_relation_table_q:
                                # 关联关系表查询时不清空 relation_table_uris（主表会包含所有子表和关联表）
                                relation_table_uris = set(composition_child_uris_from_entity_db_json(main_json))

                            # 调用 append_from_details
                            # is_sub_table_query=False 时，名称过滤逻辑不会排除子表关联关系表
                            # 但会排除主表关联关系表（通过 is_main_table_relation 检查）
                            append_from_details(details, billname, anchor_uri=queried_uri, anchor_domain=anchor_domain, anchor_json=entity_json or "", anchor_valid_uris=anchor_valid_uris, is_sub_table_query=is_sub_table_q, relation_table_uris=relation_table_uris, main_uri=effective_main_uri, is_relation_table_query=is_relation_table_q)
                            result_map = {"entities": entities_list}
                            lines.append("[已按本地索引锚定业务对象并拉取含子表的全量实体]\n")
                            lines.append("__METADATA_JSON_START__\n")
                            lines.append(json.dumps(result_map, ensure_ascii=False))
                            lines.append("\n__METADATA_JSON_END__\n")
                            lines.append(footer)
                            return "".join(lines)
                        else:
                            # getEntityListByBOId 失败，使用 append_entities_from_fast_results
                            append_entities_from_fast_results(
                                fast_results, billname, relax_billname_filter=False
                            )
                    else:
                        # 无法获取 boId，使用 append_entities_from_fast_results
                        append_entities_from_fast_results(
                            fast_results, billname, relax_billname_filter=False
                        )
                # 处理完成，跳过后面的 API 调用
                continue

        # Step1: 用名称查询 searchByName
        # 提取核心业务名称（移除参照翻译等额外关键词），因为 API 无法匹配包含额外词的查询
        billname_keywords_to_remove = ["参照翻译", "id转code", "id_to_code", "id2code", "翻译", "id转换", "id编码", "转code"]
        core_billname = billname
        for kw in billname_keywords_to_remove:
            core_billname = core_billname.replace(kw, "").strip()

        cn = fetch_business_object_by_name(cfg, core_billname or billname)
        if cn is None:
            # API 调用失败，尝试使用本地索引
            if fast_results:
                # 只查主表模式使用本地索引
                if not is_include_sub:
                    append_entities_from_fast_results(
                        fast_results, billname, relax_billname_filter=False
                    )
                else:
                    # 含子表模式但 API 失败，使用本地索引的 URI 列表
                    for fr in fast_results:
                        if fr.uri:
                            uri_json = uri_fetch(fr.uri)
                            if uri_json:
                                process_one(EntityDetailResult(uri=fr.uri), uri_json)
            continue

        # Step2: 如果有多义词，停止让用户确认
        if cn.needs_user_selection():
            return _format_candidates_ambiguous(lines[0], billname, cn.candidates or [])

        # Step3: 用 code 调用 getEntityListByBOId
        details = fetch_entity_detail_by_bo_id(cfg, cn)
        if not details:
            # getEntityListByBOId 返回为空，尝试使用本地索引
            if fast_results and not is_include_sub:
                append_entities_from_fast_results(
                    fast_results, billname, relax_billname_filter=False
                )
            continue

        # Step4: 判断用户查询的是主表还是子表、孙表或关联关系表
        # 判断逻辑：
        # 1. 主表（root=true）：返回主表和所有子表、孙表、曾孙表
        # 2. 子表（parentChildRelationship=2）：返回子表和其孙表
        # 3. 孙表或更深层级（parentChildRelationship>=3）：只返回查询对象本身
        # 4. 关联关系表（billName 包含"关联"）：只返回查询对象本身
        is_sub_table_query = False
        is_descendant_only_query = False  # 只返回查询对象本身（孙表或更深层级）
        is_relation_table_query = False  # 关联关系表查询
        queried_entity_json = None  # 查询对象的 JSON，用于确定层级

        if is_include_sub and cn and cn.id:
            # 获取查询对象的 JSON，以确定其层级
            # Flow 2 (无 query_uri): 使用 query_uri_param (空)
            # Flow 1 (有 query_uri): 使用 query_uri_param
            queried_uri_for_level = query_uri_param if query_uri_param else ""
            if not queried_uri_for_level:
                # 从 details 中找到匹配 billname 的实体
                for d in details:
                    if d and d.uri:
                        d_json = uri_fetch(d.uri)
                        if d_json:
                            try:
                                root = json.loads(d_json)
                                data = _unwrap_entity_db_data_node(root)
                                if isinstance(data, dict):
                                    bill_node, _ = _find_best_bill_node(data)
                                    bill_name = _text(bill_node, "displayName") or _text(bill_node, "title") or ""
                                    if bill_name == billname:
                                        queried_uri_for_level = d.uri
                                        queried_entity_json = d_json
                                        break
                            except Exception:
                                pass

            # 从 details 中获取查询对象的层级信息（details 来自 getEntityListByBOId，包含完整层级）
            if details and queried_uri_for_level:
                for d in details:
                    if d and d.uri == queried_uri_for_level:
                        pcr = d.parent_child_relationship
                        if pcr is not None:
                            if pcr == 2:
                                # 子表：返回子表和其孙表
                                is_sub_table_query = True
                            elif pcr >= 3:
                                # 孙表或更深层级：只返回查询对象本身
                                is_descendant_only_query = True
                        break
            elif queried_uri_for_level:
                # 回退：从 uri_fetch 获取（但 queryByUri 不包含层级信息）
                queried_entity_json = uri_fetch(queried_uri_for_level)
                if queried_entity_json:
                    try:
                        root = json.loads(queried_entity_json)
                        data = _unwrap_entity_db_data_node(root)
                        if isinstance(data, dict):
                            # 检查是否是主表
                            is_root = data.get("root")
                            # 检查 parentChildRelationship
                            parent_child_relationship = data.get("parentChildRelationship")
                            if is_root:
                                # 是主表，不设置 is_sub_table_query
                                pass
                            elif '关联' in billname:
                                # 关联关系表：只返回查询对象本身
                                is_relation_table_query = True
                            else:
                                # 检查 parentChildRelationship
                                if parent_child_relationship == 2:
                                    # 子表：返回子表和其孙表
                                    is_sub_table_query = True
                                elif parent_child_relationship and parent_child_relationship >= 3:
                                    # 孙表或更深层级：只返回查询对象本身
                                    is_descendant_only_query = True
                    except Exception:
                        pass

            # 如果上述逻辑没有确定类型，回退到旧的 billname 检查
            if not is_sub_table_query and not is_relation_table_query and not is_descendant_only_query:
                if not cn.is_main_table:
                    bill_name = cn.name or ""
                    if '子表' in bill_name and '关联' not in bill_name:
                        is_sub_table_query = True
                    elif '关联' in bill_name:
                        is_relation_table_query = True

        # Step5: 收集关联关系表的 URI（composition 子实体），用于补充过滤
        relation_table_uris: Set[str] = set()
        main_entity_uri = ""
        actual_main_uri = ""
        query_uri = ""  # 查询对象的 URI（用于设置 anchor_uri）
        if is_include_sub and cn and cn.id:
            # 获取主表的 URI（从 details 中获取）
            for d in details:
                if d and d.uri:
                    main_entity_uri = d.uri
                    # 尝试获取实体的 JSON，检查是否有 composition children
                    uri_json = uri_fetch(d.uri)
                    if uri_json:
                        children = composition_child_uris_from_entity_db_json(uri_json)
                        if children:
                            # 有 composition children 的是主表
                            actual_main_uri = d.uri
                            break
            if not actual_main_uri and main_entity_uri:
                actual_main_uri = main_entity_uri

            # 子表查询时，需要找到查询对象（子表）的 URI
            # 方法：使用 hierarchy detection 中已经确定的 queried_uri_for_level
            if is_sub_table_query:
                # 直接使用 hierarchy detection 中确定的 URI
                query_uri = queried_uri_for_level if queried_uri_for_level else ""
            elif is_relation_table_query:
                # 关联关系表查询：找到与用户查询 billname 精确匹配的关联关系表 URI
                for d in details:
                    if d and d.uri:
                        uri_json = uri_fetch(d.uri)
                        if uri_json:
                            try:
                                root = json.loads(uri_json)
                                bill_node, _ = _find_best_bill_node(root)
                                if bill_node:
                                    bill_name = _text(bill_node, "displayName") or _text(bill_node, "title") or ""
                                    # 精确匹配用户的 billname
                                    if bill_name == billname:
                                        query_uri = d.uri
                                        break
                            except Exception:
                                pass
            else:
                # 主表查询：查询对象就是主表
                query_uri = actual_main_uri

            if main_entity_uri:
                main_json = uri_fetch(main_entity_uri)
                if main_json:
                    relation_table_uris = set(composition_child_uris_from_entity_db_json(main_json))

            # 子表查询时，还需要包含查询对象（子表）的子实体
            # 使用 details 中的 parent_uri 来确定子表的孩子
            if is_sub_table_query and query_uri and details:
                # 清空主表的 composition children
                relation_table_uris = set()
                # 从 details 中找到子表的所有后代实体（孙表+）
                for d in details:
                    if d and d.uri:
                        if d.parent_uri == query_uri:
                            # 直接子实体（孙表）
                            relation_table_uris.add(d.uri)
                            d.is_descendant_of_sub = True
                        elif d.parent_uri and d.parent_uri != query_uri:
                            # 检查是否是子表后代的后代（曾孙表+）
                            # 通过追溯 parent_uri 链来判断
                            parent_uri = d.parent_uri
                            visited = set()
                            while parent_uri:
                                if parent_uri in visited:
                                    break  # 防止循环
                                visited.add(parent_uri)
                                # 查找 parent_uri 对应的实体
                                for parent_d in details:
                                    if parent_d and parent_d.uri == parent_uri and parent_d.parent_uri == query_uri:
                                        d.is_descendant_of_sub = True
                                        relation_table_uris.add(d.uri)
                                        break
                                # 继续向上追溯
                                next_parent = None
                                for parent_d in details:
                                    if parent_d and parent_d.uri == parent_uri:
                                        next_parent = parent_d.parent_uri
                                        break
                                parent_uri = next_parent

            # 孙表或更深层级查询时，只返回查询对象本身
            if is_descendant_only_query:
                # 清空 relation_table_uris，只返回查询对象本身
                relation_table_uris = set()
                # 设置 query_uri 为查询对象
                query_uri = queried_uri_for_level

            # 关联关系表查询时，只返回查询对象本身，不返回其 composition children
            # （因为关联关系表的 composition children 可能是其父表，不是真正的子表）
            if is_relation_table_query:
                # 清空 relation_table_uris，只保留查询对象本身
                relation_table_uris = set()
                # 同时清空 actual_main_uri，避免主表被包含
                actual_main_uri = ""

        # Step6: 使用 append_from_details 处理所有实体
        append_from_details(details, billname, anchor_uri=query_uri, is_sub_table_query=is_sub_table_query, relation_table_uris=relation_table_uris, main_uri=actual_main_uri, is_relation_table_query=is_relation_table_query, is_descendant_only_query=is_descendant_only_query)

    result_map = {"entities": entities_list}
    if fast_lookup_used:
        lines.append("[使用快速查找优化]\n")
    lines.append("__METADATA_JSON_START__\n")
    lines.append(json.dumps(result_map, ensure_ascii=False))
    lines.append("\n__METADATA_JSON_END__\n")
    lines.append(footer)
    return "".join(lines)
