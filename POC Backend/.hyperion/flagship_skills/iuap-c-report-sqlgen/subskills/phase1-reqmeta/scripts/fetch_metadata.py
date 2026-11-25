#!/usr/bin/env python3
"""
拉取 BIP 业务对象元数据 - 对齐 ReportSQLGenTool / BusinessObjectToolUtil
支持并行处理、URI 缓存、进度显示、配置验证

Database helpers: run scripts/db_query.py separately, or use --run-db-check.
"""

from __future__ import annotations

import argparse
import json
import hashlib
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# 确保 scripts 目录在 sys.path 中（支持直接执行脚本）
_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from iuap_common.utils import (
    ConfigValidationError,
    ExitCode,
    _text,
    _first_non_empty,
    get_progress_bar,
    load_dotenv,
    load_yaml,
    parse_doc_fields,
    resolve_config,
    safe_filename,
    validate_api_config,
)
from iuap_common.logging_config import get_logger, setup_logging

import iuap_common.bip_auth as bip_auth
from iuap_common.console_utf8 import configure_stdio_utf8
from metadata_parse import AttributeInfo, BizTableGroup, parse
from metadata_fast_lookup import MetadataFastLookup, get_fast_lookup
from iuap_common.paths_util import (
    SKILL_DIR,
    resolve_skill_path,
    resolve_workspace_path,
    workspace_base,
)

from parse_excel import parse_excel
from redis_cache import (
    init_redis_cache,
    is_redis_available,
    get_cached_metadata,
    get_cached_metadata_batch,
    set_cached_metadata,
    delete_cached_metadata,
    parse_cached_metadata,
    get_cache_stats,
    get_cached_uri_by_billname,
    set_cached_uri_by_billname,
)

# 【v6.0 新增】Entities 索引引擎
try:
    from entities_indexer import EntitiesIndex, get_entities_index, query_entity
    _ENTITIES_INDEX_AVAILABLE = True
except ImportError:
    EntitiesIndex = None
    get_entities_index = None
    query_entity = None
    _ENTITIES_INDEX_AVAILABLE = False

# 【v8.4 新增】Session Cache 索引引擎（SQLite + LRU 内存缓存）
try:
    from session_cache_indexer import SessionCacheIndex, get_session_cache_index, load_session_cache_to_index
    _SESSION_CACHE_INDEX_AVAILABLE = True
except ImportError:
    SessionCacheIndex = None
    get_session_cache_index = None
    load_session_cache_to_index = None
    _SESSION_CACHE_INDEX_AVAILABLE = False

# 全局日志记录器（须在快速查找 try 之前，避免 ImportError 分支引用未定义 logger）
logger = get_logger("fetch_metadata")

# 全局 FastLookup 实例（进程内单例，避免重复加载 JSON）
_fast_lookup_instance: Optional[MetadataFastLookup] = None

# 【v6.0 新增】全局 entities 索引实例（进程内单例，延迟初始化）
_entities_index_instance: Optional[Any] = None

# ============================================================
# 【P0 优化】Session 级跨进程实体缓存
# 解决：AI Agent 多次调用时，每次 fetch_metadata.py 都是独立进程，
#       进程内缓存无法跨进程共享，导致相同业务对象被重复拉取。
#
# 策略：
#   1. 每次 run_all_bills 完成后，将 billname → [entities] 写入 session_cache.json
#   2. 每次 run_all_bills 启动时，先加载 session_cache.json
#   3. 对于已有缓存的 billname，直接从缓存加载，跳过所有 API 调用
#   4. 新增的 billname 才走 API，最终结果合并到 session_cache
# ============================================================

_session_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None
_session_cache_dir: Optional[Path] = None
_session_cache_lock = threading.Lock()


# 【v39.x 已移除】_store_raw_query_uri_to_session_cache - 不再需要，因为 entity_info 条目已包含 businessProperties


# 【v16.x 新增】bill_info 辅助函数
def _get_bill_info_item(bill_info: List[Dict[str, str]], bill_name: str, occurrence: int = 0) -> Optional[Dict[str, str]]:
    """从 bill_info 列表中查找匹配的 billName（忽略大小写）

    Args:
        bill_info: bill_info 列表
        bill_name: 要查找的 billName
        occurrence: 返回第几个匹配（0=第一个, 1=第二个, ...）

    Returns:
        匹配的 item，未找到返回 None
    """
    bn_lower = bill_name.lower().strip()
    count = 0
    for item in bill_info:
        if item.get("billName", "").lower().strip() == bn_lower:
            if count == occurrence:
                return item
            count += 1
    return None


def _get_bill_info_index(bill_info: List[Dict[str, str]], bill_name: str, occurrence: int = 0) -> int:
    """获取 bill_info 中匹配项的索引位置

    Args:
        bill_info: bill_info 列表
        bill_name: 要查找的 billName
        occurrence: 返回第几个匹配（0=第一个, 1=第二个, ...）

    Returns:
        匹配的索引，未找到返回 -1
    """
    bn_lower = bill_name.lower().strip()
    count = 0
    for idx, item in enumerate(bill_info):
        if item.get("billName", "").lower().strip() == bn_lower:
            if count == occurrence:
                return idx
            count += 1
    return -1


def _update_bill_info(bill_info: List[Dict[str, str]], bill_name: str, uri: str = "", schema: str = "", parent_hint: str = "", occurrence: int = 0) -> None:
    """更新 bill_info 中指定 billName 的 uri 和 schema

    Args:
        bill_info: bill_info 列表
        bill_name: 要更新的 billName
        uri: 要设置的 URI
        schema: 要设置的 schema
        parent_hint: 要设置的 parent_hint
        occurrence: 更新第几个匹配（0=第一个, 1=第二个, ...）
    """
    item = _get_bill_info_item(bill_info, bill_name, occurrence)
    if item:
        if uri:
            item["uri"] = uri
        if schema:
            item["schema"] = schema
        if parent_hint:
            item["parent_hint"] = parent_hint


# 【v16.x 新增】实体信息缓存 key 构建
def _build_entity_info_cache_key(cfg: dict, uri: str) -> str:
    """构建实体信息缓存的唯一 key

    格式: entity_info:{base_url}:{tenant_id}:getEntityInfoByBOIdAndEntityId:{uri}
    """
    api = cfg.get("api") or {}
    base_url = api.get("base_url", "").rstrip("/")
    tenant_id = _get_tenant_id(cfg) or ""
    return f"entity_info:{base_url}:{tenant_id}:getEntityInfoByBOIdAndEntityId:{uri}"


# 【v40.x 新增】queryByUri 缓存 key 构建
def _build_query_uri_cache_key(cfg: dict, uri: str) -> str:
    """构建 queryByUri 缓存的唯一 key

    格式: raw_query_uri:{base_url}:{tenant_id}:queryByUri:{uri}

    【v40.x 修复】与 entity_info 保持一致的 key 格式，存储 queryByUri 原始响应
    """
    api = cfg.get("api") or {}
    base_url = api.get("base_url", "").rstrip("/")
    tenant_id = _get_tenant_id(cfg) or ""
    return f"raw_query_uri:{base_url}:{tenant_id}:queryByUri:{uri}"


# 【v40.x 新增】将 queryByUri 原始响应存入 session_cache
def _store_query_uri_to_session_cache(cfg: dict, uri: str, raw_obj: Any) -> None:
    """
    将 queryByUri 原始响应存入 session_cache。

    【v40.x 新增】key 格式与 entity_info 一致：
      raw_query_uri:{base_url}:{tenant_id}:queryByUri:{uri}

    Args:
        cfg: 配置字典
        uri: 实体 URI
        raw_obj: queryByUri API 返回的原始 JSON 对象
    """
    global _session_cache
    if not _session_cache or not raw_obj:
        return

    cache_key = _build_query_uri_cache_key(cfg, uri)
    if cache_key not in _session_cache:
        _session_cache[cache_key] = raw_obj
        logger.debug(f"[SESSION] 存储 raw_query_uri: {uri}")


def _extract_schema_from_raw_ent(raw_ent: Any) -> Optional[str]:
    """从 getEntityInfoByBOIdAndEntityId 返回的 raw_ent 中提取 schema"""
    if not isinstance(raw_ent, dict):
        return None
    ent_data = raw_ent.get("data")
    if not isinstance(ent_data, dict):
        return None
    # 尝试直接获取 schema
    schema = ent_data.get("schema")
    if schema:
        return schema
    # 嵌套结构：data.data.schema
    inner = ent_data.get("data")
    if isinstance(inner, dict):
        schema = inner.get("schema")
        if schema:
            return schema
        # 从 groups 中查找 schema
        groups = inner.get("groups")
        if isinstance(groups, list):
            for group in groups:
                if isinstance(group, dict) and group.get("schema"):
                    return group.get("schema")
    return None


def _reset_entities_index() -> None:
    """【v8.5】重置全局 entities 索引实例，用于参照实体查询后重新加载"""
    global _entities_index_instance
    _entities_index_instance = None
    logger.info("已重置 entities 索引实例")


def _get_entities_index(cfg: dict) -> Optional[Any]:
    """
    获取或初始化全局 entities 索引实例

    索引在首次访问时从 output/entities.json 加载，
    后续所有 URI 查询均走 SQLite O(1) 索引，绕过文件 IO。
    """
    global _entities_index_instance
    if _entities_index_instance is None and _ENTITIES_INDEX_AVAILABLE:
        paths = cfg.get("paths") or {}
        out_dir = resolve_workspace_path(paths.get("output_dir", "output"), cfg)
        entities_json = out_dir / "entities.json"
        if entities_json.exists():
            _entities_index_instance = get_entities_index(str(entities_json))
            count = _entities_index_instance.load_from_json(str(entities_json))
            logger.info(f"Entities 索引已加载: {count} 个实体 ({entities_json})")
    return _entities_index_instance


def _get_session_cache_dir(cfg: dict) -> Path:
    """
    获取 session 缓存目录。

    优先级：
      0. SCRIPT_ORIGINAL_CWD 环境变量（pip_install.sh 传入的用户原始 cwd）【v8.3 新增】
      1. YONBIP_REPORT_SQL_WORKSPACE 环境变量（显式指定工作空间）
      2. paths.workspace_root 配置项
      3. 向上搜索 SKILL_DIR 父路径，找已存在的 session_cache.json（健壮性保障）
      4. workspace_base(cfg) 推断结果
    """
    # 0. 【v8.3 新增】pip_install.sh 传入的原始 cwd
    original_cwd = os.environ.get("SCRIPT_ORIGINAL_CWD", "").strip()
    if original_cwd:
        p = Path(original_cwd).resolve()
        for candidate in [p, *p.parents]:
            if (candidate / ".hyperion").is_dir() or (candidate / ".git").is_dir():
                session_dir = candidate / "output" / "01_metadata"
                session_dir.mkdir(parents=True, exist_ok=True)
                return session_dir
        session_dir = p / "output" / "01_metadata"
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    # 1. 显式环境变量
    env_workspace = os.environ.get("YONBIP_REPORT_SQL_WORKSPACE", "").strip()
    if env_workspace:
        p = Path(env_workspace).expanduser()
        if not p.is_absolute():
            p = SKILL_DIR / p
        session_dir = p / "output" / "01_metadata"
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    paths = cfg.get("paths") or {}
    out_dir_str = paths.get("workspace_root") or ""
    if out_dir_str:
        p = Path(out_dir_str).expanduser()
        if not p.is_absolute():
            p = SKILL_DIR / p
    else:
        found = _find_session_cache_by_walking_up()
        if found:
            return found
        p = workspace_base(cfg)

    session_dir = p / "output" / "01_metadata"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _find_session_cache_by_walking_up() -> Optional[Path]:
    """
    从 SKILL_DIR 向上搜索，找到包含 session_cache.json 的目录。
    """
    candidate = SKILL_DIR
    for _ in range(10):
        session_json = candidate / "output" / "01_metadata" / "session_cache.json"
        if session_json.exists():
            logger.debug(f"通过向上搜索找到 session_cache: {session_json}")
            return session_json.parent
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return None


def _load_session_cache(cfg: dict) -> Dict[str, List[Dict[str, Any]]]:
    """
    加载 session 缓存（跨进程持久化的实体映射）。

    【v8.4 优化】优先使用 SQLite 索引引擎，回退到 JSON 文件加载。
    """
    global _session_cache, _session_cache_dir
    with _session_cache_lock:
        if _session_cache is not None:
            return _session_cache

        _session_cache = {}
        _session_cache_dir = _get_session_cache_dir(cfg)
        cache_file = _session_cache_dir / "session_cache.json"

        if _SESSION_CACHE_INDEX_AVAILABLE:
            try:
                idx = get_session_cache_index(cfg, cache_dir=str(_session_cache_dir))

                if cache_file.exists():
                    count = idx.load_from_json(str(cache_file))
                    if count > 0:
                        logger.info(
                            f"【SESSION CACHE】已从 JSON 迁移到 SQLite 索引: "
                            f"{count} 个业务对象 ({cache_file})"
                        )

                stats = idx.get_stats()
                billname_count = stats.get("billname_count", 0)
                entity_count = stats.get("total_entities", 0)
                if billname_count > 0:
                    logger.info(
                        f"【SESSION CACHE】SQLite 索引已加载: "
                        f"{billname_count} 个业务对象, {entity_count} 个实体 "
                        f"(查询走内存 <0.1ms)"
                    )
                    return _session_cache

            except Exception as e:
                logger.warning(f"Session Cache SQLite 加载失败，回退到 JSON: {e}")

        if not cache_file.exists():
            logger.info(f"Session 缓存不存在，将从 {cache_file} 创建新缓存")
            return _session_cache

        try:
            with cache_file.open("r", encoding="utf-8") as f:
                _session_cache = json.load(f)
            total_entities = sum(len(v) for v in _session_cache.values())
            logger.info(
                f"【SESSION CACHE】已加载 session 缓存: "
                f"{len(_session_cache)} 个业务对象, {total_entities} 个实体 "
                f"({cache_file})"
            )
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Session 缓存加载失败，将创建新缓存: {e}")
            _session_cache = {}

    return _session_cache


def _save_session_cache(cfg: dict, cache: Dict[str, List[Dict[str, Any]]]) -> None:
    """
    保存 session 缓存到磁盘。
    """
    global _session_cache_dir
    if _session_cache_dir is None:
        _session_cache_dir = _get_session_cache_dir(cfg)

    cache_file = _session_cache_dir / "session_cache.json"

    if _SESSION_CACHE_INDEX_AVAILABLE:
        try:
            idx = get_session_cache_index(cfg, cache_dir=str(_session_cache_dir))

            # 分离 entity-list 条目和 entity_info 条目
            entity_list_entries = {}
            entity_info_entries = {}
            for k, v in cache.items():
                if k.startswith("entity_info:"):
                    entity_info_entries[k] = v
                else:
                    entity_list_entries[k] = v

            # entity-list 条目写入 SQLite
            for billname_lower, entities in entity_list_entries.items():
                idx.put_by_billname(billname_lower, entities, async_write=True)

            # entity_info 条目不写入 SQLite（只用 JSON 备份）
            if entity_info_entries:
                logger.debug(f"【v16.x】{len(entity_info_entries)} 个 entity_info 条目跳过 SQLite，仅保存 JSON")
                # 有 entity_info 条目时也回退到 JSON，确保这些条目被持久化
                raise Exception("有 entity_info 条目需保存到 JSON")

            logger.debug(f"Session 缓存已提交到 SQLite（异步写入中）")
            return
        except Exception as e:
            logger.warning(f"Session Cache SQLite 写入失败，回退到 JSON: {e}")

    tmp_file = cache_file.with_suffix(".json.tmp")
    try:
        tmp_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_file.replace(cache_file)
        logger.debug(f"Session 缓存已保存: {cache_file} ({len(cache)} 个业务对象)")
    except IOError as e:
        logger.warning(f"Session 缓存写入失败: {e}")


# ============================================================
# 【性能优化 v6.0】进程内 JSON 对象缓存层
# 解决同一 URI 的 JSON 数据被多次 json.loads() 解析的问题
# 同时存储 JSON 字符串（用于 Redis/磁盘缓存）和已解析对象（避免重复解析）
# ============================================================

class _JsonObjectCache:
    """
    进程内 JSON 对象缓存 — 同时缓存 JSON 字符串和已解析对象。

    缓存策略：
    - 同一 URI 的数据只解析一次
    - 同时存储原始 JSON 字符串（用于返回给调用方）和已解析对象（用于内部处理）
    - 使用 LRU（最近最少使用）策略限制内存占用
    """

    def __init__(self, max_size: int = 5000):
        from collections import OrderedDict
        import threading
        self._lock = threading.Lock()
        # 使用 OrderedDict 实现 LRU：最新访问的放在末尾
        # {uri: {"json": str, "obj": Any}}
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._max_size = max_size
        self._hit_count = 0
        self._miss_count = 0

    def _touch(self, uri: str) -> None:
        """
        将 URI 标记为最近使用（移动到 OrderedDict 末尾）
        注意：调用方必须持有锁
        """
        if uri in self._cache:
            self._cache.move_to_end(uri)

    def get_json(self, uri: str) -> Optional[str]:
        """获取 JSON 字符串（已有则直接返回，无则返回 None）"""
        with self._lock:
            entry = self._cache.get(uri)
            if entry is not None:
                j = entry.get("json")
                if j is not None:
                    self._hit_count += 1
                    self._touch(uri)  # 标记为最近使用
                    return j
            self._miss_count += 1
            return None

    def get_obj(self, uri: str) -> Optional[Any]:
        """获取已解析的 JSON 对象"""
        with self._lock:
            entry = self._cache.get(uri)
            if entry is not None:
                obj = entry.get("obj")
                if obj is not None:
                    self._hit_count += 1
                    self._touch(uri)  # 标记为最近使用
                    return obj
            self._miss_count += 1
            return None

    def put_json(self, uri: str, json_str: str) -> None:
        """存入 JSON 字符串（已有对象则复用）"""
        with self._lock:
            entry = self._cache.get(uri)
            if entry is not None:
                # 已有对象，保持不变，只更新 json
                entry["json"] = json_str
            else:
                self._cache[uri] = {"json": json_str}
            self._touch(uri)  # 标记为最近使用
            self._maybe_evict()

    def put_obj(self, uri: str, obj: Any) -> None:
        """存入已解析的对象（已有 JSON 字符串则复用）"""
        with self._lock:
            entry = self._cache.get(uri)
            if entry is not None:
                # 已有 JSON，保持不变，只更新 obj
                entry["obj"] = obj
            else:
                self._cache[uri] = {"obj": obj}
            self._touch(uri)  # 标记为最近使用
            self._maybe_evict()

    def put_both(self, uri: str, json_str: str, obj: Any) -> None:
        """同时存入 JSON 字符串和已解析对象"""
        with self._lock:
            self._cache[uri] = {"json": json_str, "obj": obj}
            self._touch(uri)  # 标记为最近使用
            self._maybe_evict()

    def _maybe_evict(self) -> None:
        """容量超限时驱逐最旧的条目（真正的 LRU 策略）"""
        while len(self._cache) > self._max_size:
            # popitem(last=False) 移除并返回第一个（最旧的）条目
            self._cache.popitem(last=False)

    def get_stats(self) -> Dict[str, int]:
        """获取缓存统计"""
        with self._lock:
            total = self._hit_count + self._miss_count
            hit_rate = self._hit_count / total if total > 0 else 0.0
            return {
                "size": len(self._cache),
                "hit_count": self._hit_count,
                "miss_count": self._miss_count,
                "hit_rate": f"{hit_rate:.1%}",
            }


# 全局进程内对象缓存实例（URI 级别）
_uri_object_cache = _JsonObjectCache(max_size=5000)

# 【v15.x 新增】URI 级 schema 解析缓存：同一 URI 只调用一次 Three APIs
_schema_three_apis_cache: Dict[str, Optional[str]] = {}


def _get_fast_lookup(cfg: dict) -> MetadataFastLookup:
    """
    获取 FastLookup 实例（带配置感知的单例，自动尝试加载 JSON）。

    单例保护策略：
      - 首次加载失败后，若后续 config 变更（传入不同的 cfg），会重试加载
      - 仅当 _lookup_data 已成功加载时才复用（避免返回空实例）
      - 【v7.0 新增】索引过期时自动触发后台重建
    """
    global _fast_lookup_instance
    # 如果之前加载失败（_lookup_data 为 None），允许重试
    needs_reinit = (
        _fast_lookup_instance is None
        or (_fast_lookup_instance is not None and not _fast_lookup_instance.is_loaded)
    )
    if needs_reinit:
        _fast_lookup_instance = MetadataFastLookup()
        paths = (cfg.get("paths") or {}) if cfg else {}
        json_path = paths.get("metadata_lookup_json")
        loaded = False
        if json_path:
            # 相对路径解析策略：
            # 1. 先尝试相对于 SKILL_DIR（标准用法）
            # 2. 如果不存在，再尝试相对于 workspace_base（兼容项目根目录路径）
            json_p = Path(json_path).expanduser()
            if not json_p.is_absolute():
                # 尝试 SKILL_DIR 基准
                p1 = (SKILL_DIR / json_p).resolve()
                # 尝试 workspace 基准
                from iuap_common.paths_util import workspace_base
                cfg_for_workspace = cfg or {}
                p2 = (workspace_base(cfg_for_workspace) / json_p).resolve()
                # 选择存在的那个
                if p1.exists():
                    p = p1
                elif p2.exists():
                    p = p2
                else:
                    p = p1  # 用 SKILL_DIR 的路径作为默认值（后续会警告）
            else:
                p = json_p.resolve()
            if p.exists():
                loaded = _fast_lookup_instance.load_lookup_json(str(p))
            else:
                logger.warning(
                    f"FastLookup JSON 不存在: {p}，将降级到慢速 API 搜索"
                )
        if not loaded:
            # 尝试默认路径
            skill_ref = SKILL_DIR / "reference" / "metadata_lookup.json"
            if skill_ref.exists():
                loaded = _fast_lookup_instance.load_lookup_json(str(skill_ref))
            if not loaded:
                logger.warning(
                    f"FastLookup JSON 加载失败，降级到慢速 API 搜索 "
                    f"(建议检查 reference/metadata_lookup.json 是否存在)"
                )
    else:
        # 【v7.0 新增】索引已加载，检查是否过期并自动触发后台重建
        if _fast_lookup_instance._is_index_expired():
            logger.info("FastLookup 索引已过期，触发后台异步重建...")
            # 传入重建函数（从默认路径重新加载）
            def _rebuild_func():
                skill_ref = SKILL_DIR / "reference" / "metadata_lookup.json"
                if skill_ref.exists():
                    return _fast_lookup_instance.load_lookup_json(str(skill_ref))
                return False

            _fast_lookup_instance.check_and_auto_rebuild(_rebuild_func)

    return _fast_lookup_instance


# 批量查询默认上限（可被 config.performance 覆盖）
MAX_CONCURRENT_REQUESTS_DEFAULT = 16  # queryByUri 并行度默认
MAX_BATCH_SIZE = 80  # 单批 URI 数量（仅分批，不降低并发）
MAX_REFERENCE_FIELDS_DEFAULT = 30  # 参照展开条数上限默认


def _perf(cfg: dict) -> dict:
    return cfg.get("performance") or {}


def _max_concurrent_uri(cfg: dict) -> int:
    return max(
        1, int(_perf(cfg).get("max_concurrent_query_by_uri", MAX_CONCURRENT_REQUESTS_DEFAULT))
    )


def _max_concurrent_bills(cfg: dict) -> int:
    return max(1, int(_perf(cfg).get("max_concurrent_bills", 6)))


def _max_concurrent_entities(cfg: dict) -> int:
    return max(1, int(_perf(cfg).get("max_concurrent_entities", 8)))


def _max_reference_fields(cfg: dict) -> int:
    v = _perf(cfg).get("max_reference_fields_expand")
    if v is None:
        return MAX_REFERENCE_FIELDS_DEFAULT
    return max(1, int(v))


# ============================================================
# 磁盘持久化缓存配置（从 config 读取后初始化）
# ============================================================

_disk_cache_enabled: bool = False
_disk_cache_dir: Path = SKILL_DIR / ".cache" / "fetch-metadata"
_disk_cache_ttl: int = 86400  # 默认 1 天过期（秒）
_disk_cache_max_entries: int = 2000  # 默认最大缓存条目

# 【v7.0 新增】Redis 批量写入缓冲（用于单条查询场景）
_redis_write_buffer: List[Tuple[str, str]] = []
_redis_write_buffer_lock = threading.Lock()
_redis_write_buffer_max_size: int = 50  # 默认值，将被 config 覆盖


def _init_performance_config(cfg: dict) -> None:
    """
    【v7.0 新增】从 config.yaml 初始化性能配置

    统一并发配置管理：所有并发参数集中到 config.yaml 的 performance 段
    """
    global _redis_write_buffer_max_size
    
    perf = _perf(cfg)
    _redis_write_buffer_max_size = max(10, int(perf.get("redis_write_buffer_size", 50)))
    
    # 更新 FastLookup 索引配置
    from metadata_fast_lookup import MetadataFastLookup
    MetadataFastLookup._INDEX_TTL_SECONDS = int(perf.get("fastlookup_index_ttl_seconds", 86400))
    MetadataFastLookup._MIN_REBUILD_INTERVAL = int(perf.get("fastlookup_min_rebuild_interval", 3600))
    
    logger.info(f"[CONFIG] 性能配置已加载: "
                f"redis_buffer={_redis_write_buffer_max_size}, "
                f"fastlookup_ttl={MetadataFastLookup._INDEX_TTL_SECONDS}s")


def _flush_redis_write_buffer() -> None:
    """
    【v7.0 新增】刷新 Redis 写入缓冲区（批量写入）

    线程安全：函数内部自行处理锁，无需调用方持有锁
    """
    global _redis_write_buffer
    
    # 先获取锁，复制并清空缓冲区，然后在锁外执行写入
    with _redis_write_buffer_lock:
        if not _redis_write_buffer:
            return
        # 复制数据并清空缓冲区（在锁内完成）
        buffer_copy = list(_redis_write_buffer)
        _redis_write_buffer = []
        count = len(buffer_copy)
    
    # 在锁外执行 Redis 写入，避免长时间持有锁
    try:
        from redis_cache import set_cached_metadata_batch
        set_cached_metadata_batch(buffer_copy)
        logger.debug(f"Redis 缓冲区已刷新: {count} 条")
    except Exception as e:
        logger.warning(f"Redis 缓冲区刷新失败: {e}")


def _buffer_redis_write(uri: str, json_str: str) -> None:
    """
    【v7.0 新增】缓冲 Redis 写入请求，达到阈值时批量刷新

    Args:
        uri: 元数据 URI
        json_str: JSON 字符串
    """
    with _redis_write_buffer_lock:
        _redis_write_buffer.append((uri, json_str))
        # 缓冲区满时自动刷新
        if len(_redis_write_buffer) >= _redis_write_buffer_max_size:
            # 注意：不要在持有锁时调用 _flush_redis_write_buffer()
            # 因为 _flush_redis_write_buffer() 会尝试获取同一把锁
            # 这里记录需要刷新，然后在锁外执行
            needs_flush = True
        else:
            needs_flush = False
    
    # 在锁外执行刷新，避免死锁
    if needs_flush:
        _flush_redis_write_buffer()


def _uri_to_cache_filename(uri: str) -> str:
    """将 URI 转换为安全的缓存文件名（SHA256 哈希前 16 字符）"""
    h = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:16]
    return f"{h}.json"


def _get_disk_cache_path(uri: str) -> Path:
    """获取磁盘缓存文件路径"""
    return _disk_cache_dir / _uri_to_cache_filename(uri)


def _get_cached_uri_disk(uri: str) -> Optional[str]:
    """从磁盘缓存获取 URI 数据（检查 TTL）"""
    if not _disk_cache_enabled:
        return None
    p = _get_disk_cache_path(uri)
    if not p.exists():
        return None
    # 检查过期
    mtime = p.stat().st_mtime
    if time.time() - mtime > _disk_cache_ttl:
        logger.debug(f"Disk cache expired: {uri}")
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:
        logger.debug(f"Failed to read disk cache: {e}")
        return None


def _set_cached_uri_disk(uri: str, data: str) -> None:
    """
    写入 URI 数据到磁盘缓存。

    【优化】实际执行 max_entries 限制：
      - 超出容量时删除最老的 20% 条目（按 mtime 排序）
      - 避免磁盘缓存无限增长撑满磁盘
    """
    if not _disk_cache_enabled:
        return
    p = _get_disk_cache_path(uri)
    try:
        _disk_cache_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(data, encoding="utf-8")
        logger.debug(f"Wrote disk cache: {uri}")

        # 【优化】磁盘缓存容量限制：超出 max_entries 时删除最老的 20%
        if _disk_cache_max_entries > 0:
            all_files = sorted(
                _disk_cache_dir.glob("*.json"),
                key=lambda f: f.stat().st_mtime,
            )
            if len(all_files) > _disk_cache_max_entries:
                evict_count = max(1, _disk_cache_max_entries // 5)
                for f in all_files[:evict_count]:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                logger.debug(f"Evicted {evict_count} old disk cache entries")
    except Exception as e:
        logger.debug(f"Failed to write disk cache: {e}")


def _init_disk_cache(cfg: dict) -> None:
    """从配置初始化磁盘缓存"""
    global _disk_cache_enabled, _disk_cache_dir, _disk_cache_ttl, _disk_cache_max_entries
    cache_cfg = cfg.get("cache", {})
    _disk_cache_enabled = bool(cache_cfg.get("enabled", True))

    # 缓存目录：配置项 → 默认 SKILL_DIR/.cache/fetch-metadata
    cache_dir = str(cache_cfg.get("dir", ".cache/fetch-metadata")).strip()
    if cache_dir:
        p = Path(cache_dir)
        if not p.is_absolute():
            p = SKILL_DIR / p
        _disk_cache_dir = p.resolve()

    _disk_cache_ttl = int(cache_cfg.get("ttl_seconds", 86400))
    _disk_cache_max_entries = int(cache_cfg.get("max_entries", 2000))

    logger.debug(
        f"Disk cache initialized: enabled={_disk_cache_enabled}, dir={_disk_cache_dir}"
    )


def http_get_json(
    cfg: dict, path: str, extra_params: Optional[Dict[str, str]] = None
) -> Any:
    """见 bip_auth.http_get_json：先取 token（缓存+过期刷新），再带 access_token 请求。"""
    return bip_auth.http_get_json(cfg, path, extra_params)


def _byname_cache_key(billname: str) -> str:
    """【v15.x】生成 byname 缓存 key：去掉括号后缀，统一真实业务对象名称"""
    import re
    return re.sub(r"[（(][^）)]*?[）)]$", "", billname).strip()


def _get_cached_byname(billname: str) -> Optional[str]:
    """从缓存获取 searchByName 结果（仅 Redis）"""
    key = _byname_cache_key(billname)
    return get_cached_metadata(f"byname:{key}")


def _set_cached_byname(billname: str, data: str) -> None:
    """缓存 searchByName 结果到 Redis"""
    key = _byname_cache_key(billname)
    set_cached_metadata(f"byname:{key}", data)


def _del_cached_byname(billname: str) -> None:
    """删除 searchByName 缓存（用于解析失败时清除陈旧缓存）"""
    key = _byname_cache_key(billname)
    delete_cached_metadata(f"byname:{key}")


def _get_cached_byboid(boid: str) -> Optional[str]:
    """从缓存获取 getEntityListByBOId 结果（仅 Redis）"""
    return get_cached_metadata(f"byboid:{boid}")


def _set_cached_byboid(boid: str, data: str) -> None:
    """缓存 getEntityListByBOId 结果到 Redis"""
    set_cached_metadata(f"byboid:{boid}", data)


def _get_entityid_disk_cache_path(entity_id: str) -> Path:
    """获取 entityid 磁盘缓存文件路径"""
    h = hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:16]
    return _disk_cache_dir / f"entityid_{h}.json"


def _get_cached_entityid_disk(entity_id: str) -> Optional[str]:
    """从磁盘缓存获取 entityid 数据（检查 TTL）"""
    if not _disk_cache_enabled:
        return None
    p = _get_entityid_disk_cache_path(entity_id)
    if not p.exists():
        return None
    mtime = p.stat().st_mtime
    if time.time() - mtime > _disk_cache_ttl:
        logger.debug(f"[DISK] entityid 缓存过期: {entity_id}")
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception as e:
        logger.debug(f"[DISK] 读取 entityid 缓存失败: {e}")
        return None


def _set_cached_entityid_disk(entity_id: str, data: str) -> None:
    """写入 entityid 数据到磁盘缓存"""
    if not _disk_cache_enabled:
        return
    p = _get_entityid_disk_cache_path(entity_id)
    try:
        _disk_cache_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(data, encoding="utf-8")
        logger.debug(f"[DISK] entityid 缓存写入: {entity_id}")

        if _disk_cache_max_entries > 0:
            all_files = sorted(
                _disk_cache_dir.glob("entityid_*.json"),
                key=lambda f: f.stat().st_mtime,
            )
            if len(all_files) > _disk_cache_max_entries:
                evict_count = max(1, _disk_cache_max_entries // 5)
                for f in all_files[:evict_count]:
                    try:
                        f.unlink()
                    except OSError:
                        pass
    except Exception as e:
        logger.debug(f"[DISK] 写入 entityid 缓存失败: {e}")


def _get_cached_entityid(entity_id: str) -> Optional[str]:
    """
    从缓存获取 getEntityInfoByBOIdAndEntityId 结果。

    缓存层级（v11.0 新增磁盘缓存）：
      1. Redis 缓存 → 跨进程共享
      2. 磁盘缓存 → 本地持久化兜底（Redis 不可用时）

    【v11.0 优化】当 Redis 不可用时，从磁盘缓存回退，避免重复请求 API。
    """
    # 1. 先查 Redis
    cached = get_cached_metadata(f"entityid:{entity_id}")
    if cached:
        logger.debug(f"[CACHE] entityid Redis命中: {entity_id}")
        return cached

    # 2. Redis 未命中，查磁盘缓存（兜底）
    disk_cached = _get_cached_entityid_disk(entity_id)
    if disk_cached:
        logger.info(f"[DISK] entityid 磁盘缓存命中: {entity_id}")
        # 回填 Redis（如果可用）
        try:
            set_cached_metadata(f"entityid:{entity_id}", disk_cached)
        except Exception:
            pass
        return disk_cached

    return None


def _set_cached_entityid(entity_id: str, data: str) -> None:
    """
    缓存 getEntityInfoByBOIdAndEntityId 结果。

    【v11.0 优化】Redis + 磁盘双写，确保 Redis 不可用时数据不丢失。
    """
    # 1. 写 Redis
    try:
        set_cached_metadata(f"entityid:{entity_id}", data)
    except Exception as e:
        logger.debug(f"[CACHE] entityid Redis写入失败: {e}")

    # 2. 写磁盘（确保持久化）
    _set_cached_entityid_disk(entity_id, data)


def _extract_business_properties_from_raw(raw_ent: Any) -> Optional[List[Dict[str, Any]]]:
    """
    从 getEntityInfoByBOIdAndEntityId 返回的原始 JSON 中提取 businessProperties。

    Args:
        raw_ent: getEntityInfoByBOIdAndEntityId 返回的原始 JSON（可以是字符串或已解析的 dict）

    Returns:
        businessProperties 列表，如果不存在则返回 None
    """
    try:
        # 支持字符串和 dict 两种输入
        if isinstance(raw_ent, str):
            db_obj = json.loads(raw_ent)
        else:
            db_obj = raw_ent
        data_node = db_obj.get("data", {})
        if isinstance(data_node, dict):
            inner = data_node.get("data", {})
            if isinstance(inner, dict):
                biz_props = inner.get("businessProperties", [])
                if isinstance(biz_props, list) and biz_props:
                    return biz_props
    except Exception as e:
        logger.debug(f"[ENRICH] 解析 businessProperties 失败: {e}")
    return None


def _supplement_custom_fields_to_attributes(entities: List[Dict[str, Any]]) -> None:
    """
    【v33.x 新增】将自定义项字段添加到主表 attributes。

    从 businessProperties 中提取 isCharacterRef=True 的自定义项字段，
    添加到对应主表的 attributes 列表中。

    Args:
        entities: 实体列表（原地修改）
    """
    for ent in entities:
        if not isinstance(ent, dict):
            continue

        table_name = ent.get("tableName", "")
        biz_props = ent.get("businessProperties", [])
        if not biz_props:
            continue

        attrs = ent.setdefault("attributes", [])
        existing_cols = {a.get("dbColumnName") for a in attrs if isinstance(a, dict)}

        for bp in biz_props:
            if not isinstance(bp, dict):
                continue

            # 只处理 isCharacterRef=True 的自定义项
            if not bp.get("isCharacterRef"):
                continue

            # 使用 fieldName 作为列名
            field_name = bp.get("fieldName")
            if not field_name or field_name in existing_cols:
                continue

            # 获取显示名
            display_name = bp.get("displayName") or "自定义项"

            # 获取参照 URI
            data_type = bp.get("dataType", {})
            uri = data_type.get("uri") if isinstance(data_type, dict) else None

            # 构建属性
            attr = {
                "displayName": display_name,
                "dbColumnName": field_name,
                "type": "参照类型，此处为参照对象id，long类型，需使用toString()",
                "tableName": table_name,
                "isCharacterField": True,
            }

            if uri:
                attr["uri"] = uri

            attrs.append(attr)
            existing_cols.add(field_name)
            logger.debug(f"[ENRICH] 自定义项字段已添加: {table_name}.{field_name} ({display_name})")

    logger.debug(f"[ENRICH] 自定义项字段补充完成")


def _enrich_business_properties(entities: List[Dict[str, Any]], cfg: dict = None) -> None:
    """
    【v11.0 新增】补充实体的 businessProperties。

    从 getEntityInfoByBOIdAndEntityId 的磁盘缓存中提取 businessProperties，
    解决：主表的 businessProperties 在 queryByUri 中为空，导致特征表 allTables 解析问题。

    【v33.x 修复】增加从 session 缓存通过 URI 查找的逻辑，
    因为 queryUri 返回的实体没有 entityId。

    【v38.x 修复】同时更新 attributes：将 businessProperties 转换为 attributes 格式，
    解决：主表的 BusinessData.attributes 只有 6 个系统字段，90 个业务字段在 businessProperties 中。

    【修复】过滤 businessProperties，只保留与 Excel 表头匹配的属性

    Args:
        entities: 实體列表（原地修改）
        cfg: 配置字典（用于构建 session 缓存 key）
    """
    enriched_count = 0
    skipped_no_entity_id = 0
    skipped_has_bp = 0
    skipped_no_cache = 0
    skipped_no_uri = 0

    # 【v44.x 修复】获取过滤字段集合，优先使用 _column_field_names（业务字段名），fallback 到 _effective_fields（表头描述）
    filter_fields: Set[str] = set()
    if cfg:
        # 优先使用 columnFieldNames（业务字段名，如"单据日期"），与元数据 displayName 匹配
        column_field_names = cfg.get("_column_field_names", [])
        if column_field_names:
            filter_fields = {f.lower() for f in column_field_names}
        else:
            # fallback 到 effectiveFields（表头描述，如"资产购置申请单时间"）
            effective_fields = cfg.get("_effective_fields", [])
            filter_fields = {f.lower() for f in effective_fields}
        # 内联默认必需字段（与 _fetch_entity_schema_and_filter_attrs 中的 _DEFAULT_REQUIRED_FIELDS 保持一致）
        # 【v46.x 修复】添加"单据日期"、"单据编号"等核心业务字段，避免被过滤丢失
        # 【v48.x 修复】添加更多核心业务字段，避免申请单特有字段被过滤
        filter_fields.update({
            "id", "审批状态", "verifystate", "审批", "审核状态",
            "结算状态", "isettlestatus", "结算",
            "审批日期", "audit_date", "auditdate",
            "单据状态", "借款金额", "预付金额", "nloanmny",
            "核销金额", "已核销金额", "ncavmny",
            "ytenant_id", "租户", "tenant_id",
            # 【v46.x 新增】单据日期/单据编号等核心字段
            "单据日期", "billdate", "单据时间", "制单日期", "制单时间",
            "单据编号", "billno", "编码", "code", "单号",
            "创建人", "创建时间", "修改人", "修改时间",
            "申请人", "applyuser", "申请人名称",
            "组织", "org", "组织名称", "部门", "dept", "部门名称",
            "费用承担组织", "费用承担部门",
            # 【v48.x 新增】通用申请单特有字段
            "vouchdate", "业务日期", "业务时间",
            "申请名称", "applyname", "申请事由", "applyreason", "vreason",
            "申请金额", "applyamount", "napplymny",
            "资产类型", "预算内外", "资产购置",
        })

    # 【v33.x 新增】使用全局 session 缓存
    global _session_cache

    for ent in entities:
        # 已有 businessProperties 的跳过
        if ent.get("businessProperties"):
            skipped_has_bp += 1
            continue

        # 优先尝试通过 URI 从 session 缓存查找（queryUri 返回的实体没有 entityId）
        uri = ent.get("uri")
        bp = None
        if uri:
            # 【v39.x 优化】使用 entity_info:{uri} 查找（包含完整 businessProperties）
            cache_key = _build_entity_info_cache_key(cfg, uri)
            if _session_cache and cache_key in _session_cache:
                raw_ent = _session_cache[cache_key]
                bp = _extract_business_properties_from_raw(raw_ent)
                if bp:
                    logger.info(f"[ENRICH] 从 session_cache entity_info 命中 businessProperties: {uri}")
            # 【v33.x 新增】尝试从磁盘缓存读取（entityid 格式）
            if not bp:
                entity_id = ent.get("entityId")
                if entity_id:
                    raw_json = _get_cached_entityid_disk(entity_id)
                    if raw_json:
                        bp = _extract_business_properties_from_raw(raw_json)
                        if bp:
                            logger.info(f"[ENRICH] 从磁盘缓存 entityid 命中 businessProperties: {uri}")

            # 【v42.x 新增】URI 查找和 entityId 磁盘缓存都失败时，调用 Three APIs 获取
            if not bp and uri:
                logger.info(f"[ENRICH] 尝试通过 Three APIs 获取 businessProperties: {uri}")
                schema = _fetch_schema_via_three_apis(cfg, uri)
                if schema:
                    # Three APIs should have stored entity_info in session_cache
                    cache_key = _build_entity_info_cache_key(cfg, uri)
                    if _session_cache and cache_key in _session_cache:
                        raw_ent = _session_cache[cache_key]
                        bp = _extract_business_properties_from_raw(raw_ent)
                        if bp:
                            logger.info(f"[ENRICH] 从 Three APIs 获取到 businessProperties: {uri} ({len(bp)} fields)")

        if bp:
            ent["businessProperties"] = bp
            enriched_count += 1
            # 【v50.x 修复】按 filter_fields 过滤，只保留报表需要的字段
            # 用 displayName 匹配 _column_field_names（从 _columnFieldMappings 提取的字段）
            bp_attrs = _convert_business_properties_to_attributes(bp)
            if bp_attrs:
                existing = ent.get("attributes", [])
                existing_cols = {a.get("dbColumnName") for a in existing if isinstance(a, dict)}
                for attr in bp_attrs:
                    col = attr.get("dbColumnName")
                    if not col or col in existing_cols:
                        continue
                    dn = (attr.get("displayName") or "").lower()
                    dbn = col.lower()
                    # 匹配 displayName 或 dbColumnName
                    if filter_fields and dn not in filter_fields and dbn not in filter_fields:
                        continue
                    existing.append(attr)
                    existing_cols.add(col)
                ent["attributes"] = existing
                logger.debug(f"[ENRICH] 实体 {uri} 补充了 {len(bp)} 个 businessProperties，attributes 更新为 {len(existing)} 个")
            continue

        # 原有逻辑：通过 entityId 查找
        entity_id = ent.get("entityId")
        if not entity_id:
            skipped_no_entity_id += 1
            skipped_no_uri += 1 if uri else 0
            continue

        # 从磁盘缓存读取
        raw_json = _get_cached_entityid_disk(entity_id)
        if not raw_json:
            skipped_no_cache += 1
            continue

        # 提取 businessProperties
        bp = _extract_business_properties_from_raw(raw_json)
        if bp:
            ent["businessProperties"] = bp
            enriched_count += 1
            # 【v38.x 新增】将 businessProperties 转换为 attributes 并追加到现有 attributes
            bp_attrs = _convert_business_properties_to_attributes(bp)
            if bp_attrs:
                existing = ent.get("attributes", [])
                existing_cols = {a.get("dbColumnName") for a in existing if isinstance(a, dict)}
                for attr in bp_attrs:
                    col = attr.get("dbColumnName")
                    if col and col not in existing_cols:
                        existing.append(attr)
                        existing_cols.add(col)
                ent["attributes"] = existing
                logger.debug(f"[ENRICH] 实体 {ent.get('uri')} 补充了 {len(bp)} 个 businessProperties，attributes 更新为 {len(existing)} 个")

    logger.info(
        f"[ENRICH] businessProperties 补充完成: "
        f"补充 {enriched_count} 个, 已有 {skipped_has_bp} 个, "
        f"无entityId {skipped_no_entity_id} 个, 无缓存 {skipped_no_cache} 个"
    )


def _convert_business_properties_to_attributes(business_properties: List[Any], entity_table_name: str = "") -> List[Dict[str, Any]]:
    """
    【v38.x 新增】将 businessProperties 转换为 attributes 格式。
    【v41.x 优化】保留 characterFields 用于多选参照展开

    businessProperties 格式：
      - fieldName: 字段名（驼峰）
      - displayName: 显示名
      - biztype: 业务类型（long/text/decimal/quote/UserDefine）
      - dataType.uri: 数据类型 URI（Long/String/Decimal）
      - refUri: 参照 URI（仅 quote 类型）
      - tableName: 表名（在 allTables 中）
      - isCharacterRef: 是否为自定义项参照
      - characterFields: 自定义项子字段（多选参照）

    Args:
        business_properties: businessProperties 列表

    Returns:
        attributes 格式的列表
    """
    if not isinstance(business_properties, list):
        return []

    attrs = []
    for bp in business_properties:
        if not isinstance(bp, dict):
            continue

        # 【v48.x 修复】fieldName 可能为 None，此时使用 name 作为 dbColumnName
        field_name = bp.get("fieldName") or bp.get("name")
        if not field_name:
            continue

        display_name = bp.get("displayName") or field_name
        biztype = bp.get("biztype", "")

        # 获取数据类型
        data_type = bp.get("dataType")
        type_uri = ""
        if isinstance(data_type, dict):
            type_uri = data_type.get("uri", "") or data_type.get("name", "")

        # 参照类型
        # 【v47.x 修复】refUri 可能不在 bp 中，而是 dataType.uri
        ref_uri = bp.get("refUri")
        if not ref_uri and biztype == "quote" and type_uri:
            ref_uri = type_uri

        if biztype == "quote" and ref_uri:
            type_str = ref_uri  # 【v47.x 修复】type 应该是参照 URI
        elif type_uri:
            type_str = type_uri
        else:
            type_str = biztype or "String"

        # 获取表名：优先 tableName 字段，否则取 allTables[0]
        all_tables = bp.get("allTables")
        table_name = bp.get("tableName") or ""
        if not table_name and isinstance(all_tables, list) and all_tables:
            table_name = all_tables[0]

        # 【v47.x 修复】对于参照字段，如果 allTables 与当前实体不符，使用当前实体的 tableName
        # 避免变更单等实体的 businessProperties 返回主表的 allTables
        if biztype == "quote" and entity_table_name and table_name:
            if entity_table_name not in (all_tables or []):
                logger.info(f"[REF TABLE] 参照字段 {display_name} 的 allTables={all_tables} 与实体表 {entity_table_name} 不符，使用实体表名")
                table_name = entity_table_name

        attr: Dict[str, Any] = {
            "displayName": display_name,
            "dbColumnName": field_name,
            "type": type_str,
            "tableName": table_name,
            "biztype": biztype,
        }

        if ref_uri:
            attr["refUri"] = ref_uri

        # 【v41.x 新增】保留 isCharacterRef 标识
        if bp.get("isCharacterRef"):
            attr["isCharacterRef"] = True

        # 【v41.x 新增】保留 characterFields（多选参照子字段）
        char_fields = bp.get("characterFields")
        if char_fields:
            attr["characterFields"] = char_fields

        attrs.append(attr)

    return attrs


# 【v47.x 新增】为参照字段补充 referenceStructure
def _supplement_reference_structure(attr: Dict[str, Any], cfg: dict = None) -> None:
    """
    为参照字段补充 referenceStructure。

    处理逻辑：
    1. 如果是特征字段（isCharacterField=True），跳过展开
    2. 如果没有 refUri，跳过展开
    3. 只有标准参照才展开 referenceStructure

    Args:
        attr: 属性字典，会原地修改添加 referenceStructure
        cfg: 配置字典（用于获取缓存和调用 API）
    """
    # 【修改】优先检查特征字段，特征字段不展开 referenceStructure
    is_char_field = attr.get("isCharacterField")
    if is_char_field:
        logger.info(f"[REF] 跳过特征字段展开: {attr.get('displayName')} (isCharacterField=True)")
        return

    ref_uri = attr.get("refUri")
    if not ref_uri:
        logger.info(f"[REF] 跳过参照展开：无 refUri, displayName={attr.get('displayName')}")
        return

    logger.info(f"[REF] 开始处理参照字段: displayName={attr.get('displayName')}, refUri={ref_uri}")

    try:
        # 1. 获取 schema（优先使用 fast_lookup，其次调用 API）
        schema = None
        fast_lookup = _get_fast_lookup(cfg) if cfg else None
        if fast_lookup and fast_lookup.is_loaded:
            hit = fast_lookup.get_by_uri(ref_uri)
            if hit and hit.schema:
                schema = hit.schema
                logger.info(f"[REF] FastLookup schema 命中: {ref_uri} → {schema}")

        if not schema and ref_uri:
            schema = _fetch_schema_via_three_apis(cfg, ref_uri)
            if schema:
                logger.info(f"[REF] 通过 API 获取 schema: {ref_uri} → {schema}")

        if not schema or schema == "scheme":
            logger.warning(f"[REF] 无法获取参照实体的 schema: {ref_uri}，跳过 referenceStructure")
            return

        # 2. 查询参照实体的 attributes 和 tableName（主要是 name, code 等）
        ref_result = _query_reference_attributes(ref_uri, cfg)
        ref_attrs = ref_result.get("attributes", [])
        ref_table_name = ref_result.get("tableName", "")
        logger.info(f"[REF] 获取到 {len(ref_attrs)} 个参照属性: {ref_uri}, tableName={ref_table_name}")

        # 3. 构建 referenceStructure
        bill_name = _extract_bill_name_from_uri(ref_uri)
        domain = ref_uri.split(".")[0] if "." in ref_uri else ""

        ref_struct = {
            "billName": bill_name or ref_uri,
            "domain": domain,
            "attributes": ref_attrs,
            "scheme": schema,
        }
        # 【v48.x 修复】将参照实体的 tableName 加入 referenceStructure
        if ref_table_name:
            ref_struct["tableName"] = ref_table_name
            logger.info(f"[REF TABLE] referenceStructure.tableName = {ref_table_name}")

        attr["referenceStructure"] = ref_struct

        # 【v48.x 修复】同时更新 attr 的 tableName 为参照实体的 tableName
        if ref_table_name:
            attr["tableName"] = ref_table_name
            logger.info(f"[REF TABLE] attr.tableName 更新为参照实体的 tableName: {ref_table_name}")

        logger.info(f"[REF] 补充 referenceStructure 完成: {attr.get('displayName')} → schema={schema}, tableName={ref_table_name}")
    except Exception as e:
        logger.warning(f"[REF] 补充 referenceStructure 失败: {e}")


def _query_reference_attributes(ref_uri: str, cfg: dict = None) -> Dict[str, Any]:
    """
    查询参照实体的 attributes 和 tableName。

    Args:
        ref_uri: 参照实体的 URI
        cfg: 配置字典

    Returns:
        dict: {"attributes": [...], "tableName": "org_orgs"}
    """
    result = {"attributes": [], "tableName": ""}
    try:
        attrs = []
        ref_table_name = ""

        # 【修改】直接从 entity_info API 获取 businessProperties（不再从 queryByUri 获取）
        # 1. 先调用 _fetch_schema_via_three_apis 获取 schema，同时结果存入 _session_cache
        schema = _fetch_schema_via_three_apis(cfg, ref_uri)
        if not schema:
            logger.warning(f"[REF] 无法从 entity_info 获取 schema: {ref_uri}")
            return result

        # 2. 从 _session_cache 中获取 entity_info 结果
        cache_key = _build_entity_info_cache_key(cfg, ref_uri)
        if _session_cache and cache_key in _session_cache:
            raw_ent = _session_cache[cache_key]
            ent_data = raw_ent.get("data", {}) if isinstance(raw_ent, dict) else {}
            inner_data = ent_data.get("data", {}) if isinstance(ent_data, dict) else {}

            # 获取 tableName
            ref_table_name = inner_data.get("tableName", "")
            if not ref_table_name:
                ref_table_name = ent_data.get("tableName", "")
            if ref_table_name:
                logger.info(f"[REF TABLE] 从 entity_info 获取 tableName: {ref_uri} → {ref_table_name}")

            # 获取 businessProperties 中的 attributes
            bp = inner_data.get("businessProperties", [])
            for p in bp:
                name = p.get("name") or p.get("fieldName")
                if name in ("name", "code", "id"):
                    attrs.append({
                        "displayName": p.get("displayName") or name,
                        "dbColumnName": name,
                        "primarykey": "id" if name == "id" else None,
                    })
            logger.info(f"[REF] 从 entity_info 获取 {len(attrs)} 个属性: {ref_uri}")

        # 【修改】所有信息都来自 _fetch_schema_via_three_apis，不再需要默认属性 fallback

        # 返回 attributes 和 tableName
        result["attributes"] = attrs[:5] if attrs else []
        result["tableName"] = ref_table_name if ref_table_name else result.get("tableName", "")
        return result

    except Exception as e:
        logger.debug(f"[REF] 查询参照属性失败: {ref_uri}, {e}")
        return result


def _query_entity_info_cached(cfg: dict, uri: str) -> Optional[str]:
    """查询 entity_info（带缓存）"""
    if not cfg:
        return None

    try:
        cache_key = _build_entity_info_cache_key(cfg, uri)
        if _session_cache and cache_key in _session_cache:
            raw_ent = _session_cache[cache_key]
            return json.dumps(raw_ent, ensure_ascii=False)
    except Exception:
        pass
    return None


def _extract_bill_name_from_uri(uri: str) -> str:
    """从 URI 提取 billName（简化逻辑）"""
    if not uri:
        return ""

    # 常见的映射
    uri_to_name = {
        "org.func.BaseOrg": "组织",
        "bd.adminOrg.AdminOrgVO": "部门",
        "base.user.User": "用户",
        "base.user.BipUser": "用户",
        "bd.project.ProjectVO": "项目",
        "bd.costcenter.CostCenter": "成本中心",
        "bd.expenseitem.ExpenseItem": "费用项目",
        "bd.expenseitem.ExpenseItemType": "费用类型",
        "bd.currencytenant.CurrencyTenantVO": "币种",
        "bd.region.BaseRegionVO": "地区",
        "aa.vendor.Vendor": "供应商",
        "aa.merchant.Merchant": "客户",
    }

    if uri in uri_to_name:
        return uri_to_name[uri]

    # 从 URI 解析：bd.adminOrg.AdminOrgVO → 部门
    parts = uri.split(".")
    if len(parts) >= 3:
        # 取最后一部分，去掉 VO 后缀
        name_part = parts[-1].replace("VO", "").replace("VO", "")
        # 尝试转换为中文
        name_mapping = {
            "Org": "组织",
            "AdminOrg": "部门",
            "User": "用户",
            "Project": "项目",
            "CostCenter": "成本中心",
            "ExpenseItem": "费用项目",
            "Currency": "币种",
            "Region": "地区",
            "Vendor": "供应商",
            "Merchant": "客户",
        }
        for eng, cn in name_mapping.items():
            if eng.lower() in name_part.lower():
                return cn

    return uri


# 【v41.x 新增】展开 characterFields 到实体属性
def _expand_character_fields(entity: Dict[str, Any]) -> None:
    """
    展开 characterFields 到实体 attributes 中。
    将多选参照字段添加到 attributes 列表，便于后续处理。

    例如：
      financialProductCharacterDef.characterFields = [
        {"name": "aicoding_test", "biztype": "referenceList", ...},
        {"name": "id", "biztype": "text", ...},
        {"name": "pubts", "biztype": "dateTime_Timestamp", ...}
      ]

    展开后：
      attributes 中新增（跳过系统字段 id, pubts, ytenant）：
        {"name": "aicoding_test", "tableName": "..._99", "biztype": "referenceList", "isCharacterField": true, ...}
    """
    attrs = entity.get("attributes")
    if not isinstance(attrs, list):
        return

    # 系统字段列表（不展开）
    SYSTEM_FIELDS = {"id", "pubts", "ytenant", "ytenant_id", "creator", "modifier", "create_time", "modify_time"}

    for attr in attrs:
        char_fields = attr.get("characterFields")
        if not isinstance(char_fields, list):
            continue

        parent_field = attr.get("name", "")
        parent_table = attr.get("tableName", "")

        for cf in char_fields:
            if not isinstance(cf, dict):
                continue

            cf_name = cf.get("name", "")
            # 跳过系统字段
            if cf_name in SYSTEM_FIELDS:
                continue

            # 获取子字段的表名
            cf_all_tables = cf.get("allTables")
            cf_table = ""
            if isinstance(cf_all_tables, list) and cf_all_tables:
                cf_table = cf_all_tables[0]

            # 获取数据类型
            cf_data_type = cf.get("dataType")
            cf_type_uri = ""
            if isinstance(cf_data_type, dict):
                cf_type_uri = cf_data_type.get("uri", "") or cf_data_type.get("name", "")

            cf_biztype = cf.get("biztype", "")
            cf_display = cf.get("displayName") or cf_name
            cf_ref_type = cf.get("refType")
            # 【v43.x 修复】特征字段的 dbColumnName 应该是 fieldName（数据库列名），而不是 name（字段定义名）
            cf_field_name = cf.get("fieldName") or cf_name

            # 创建展开后的属性
            expanded_attr: Dict[str, Any] = {
                "displayName": cf_display,
                "dbColumnName": cf_field_name,
                "type": cf_type_uri or cf_biztype or "String",
                "tableName": cf_table,
                "biztype": cf_biztype,
                # 【v41.x 标记】这是多选参照字段
                "isCharacterField": True,
                "parentField": parent_field,
                "parentTable": parent_table,
            }

            # 多选参照标识
            if cf_biztype == "referenceList":
                expanded_attr["isMultiSelect"] = True

            # 参照类型
            if cf_ref_type:
                expanded_attr["refType"] = cf_ref_type

            # refUri
            if cf_type_uri:
                expanded_attr["refUri"] = cf_type_uri

            # 检查是否已存在（避免重复）
            existing_names = {a.get("name") for a in attrs}
            existing_cols = {a.get("dbColumnName") for a in attrs}
            if cf_field_name not in existing_cols:
                attrs.append(expanded_attr)
                logger.debug(f"[EXPAND] 展开多选参照字段: {cf_name} -> dbColumnName={cf_field_name} (parent={parent_field})")


# 【v41.x 新增】将 businessProperties 整合到 attributes 并移除 businessProperties
def _merge_business_properties_into_attributes(entities: List[Dict[str, Any]], cfg: dict = None) -> List[Dict[str, Any]]:
    """
    将 businessProperties 整合到 attributes，移除 businessProperties。
    保留 characterFields 用于后续多选参照展开。

    【v41.x 优化】统一 entities.json 输出格式，只保留 attributes
    【v44.x 修复】过滤 businessProperties，只保留与 Excel 表头匹配的属性
    """
    # 【v44.x 修复】获取过滤字段集合，优先使用 _column_field_names（业务字段名），fallback 到 _effective_fields（表头描述）
    filter_fields: Set[str] = set()
    if cfg:
        # 优先使用 columnFieldNames（业务字段名，如"单据日期"），与元数据 displayName 匹配
        column_field_names = cfg.get("_column_field_names", [])
        if column_field_names:
            filter_fields = {f.lower() for f in column_field_names}
        else:
            # fallback 到 effectiveFields（表头描述，如"资产购置申请单时间"）
            effective_fields = cfg.get("_effective_fields", [])
            filter_fields = {f.lower() for f in effective_fields}
        # 内联默认必需字段（与 _fetch_entity_schema_and_filter_attrs 中的 _DEFAULT_REQUIRED_FIELDS 保持一致）
        # 【v46.x 修复】添加"单据日期"、"单据编号"等核心业务字段，避免被过滤丢失
        filter_fields.update({
            "id", "审批状态", "verifystate", "审批", "审核状态",
            "结算状态", "isettlestatus", "结算",
            "审批日期", "audit_date", "auditdate",
            "单据状态", "借款金额", "预付金额", "nloanmny",
            "核销金额", "已核销金额", "ncavmny",
            "ytenant_id", "租户", "tenant_id",
            # 【v45.x 新增】子表关联主表的外键字段（SQL JOIN 必需）
            "mainid", "主表id", "imainid",
            # 【v46.x 新增】单据日期/单据编号等核心字段
            "单据日期", "billdate", "单据时间", "制单日期", "制单时间",
            "单据编号", "billno", "编码", "code", "单号",
            "创建人", "创建时间", "修改人", "修改时间",
            "申请人", "applyuser", "申请人名称",
            "组织", "org", "组织名称", "部门", "dept", "部门名称",
            "费用承担组织", "费用承担部门",
        })

    for ent in entities:
        bp = ent.pop("businessProperties", None)
        if bp and isinstance(bp, list):
            # 转换为 attributes 并追加
            # 【v47.x 修复】传入实体的 tableName，用于修正参照字段的 tableName
            entity_table_name = ent.get("tableName", "")
            bp_attrs = _convert_business_properties_to_attributes(bp, entity_table_name)
            existing = ent.get("attributes", [])
            existing_cols = {a.get("dbColumnName") for a in existing if isinstance(a, dict)}

            # 【v50.x 修复】按 filter_fields 过滤，只保留报表需要的字段
            # 用 displayName 匹配 _column_field_names（从 _columnFieldMappings 提取的字段）
            # 【v52.x 修复】如果有 characterFields 且子字段在 filter_fields 中，应该保留父字段
            for attr in bp_attrs:
                col = attr.get("dbColumnName")
                if not col or col in existing_cols:
                    continue
                dn = (attr.get("displayName") or "").lower()
                dbn = col.lower()
                # 检查是否应该保留
                keep = False
                if filter_fields:
                    # 直接匹配
                    if dn in filter_fields or dbn in filter_fields:
                        keep = True
                    # 检查 characterFields 子字段是否在 filter_fields 中
                    char_fields = attr.get("characterFields")
                    if not keep and char_fields and isinstance(char_fields, list):
                        for cf in char_fields:
                            cf_dn = (cf.get("displayName") or "").lower()
                            if cf_dn in filter_fields:
                                keep = True
                                logger.info(f"[CHAR] 父字段 {dn} 因子字段 {cf_dn} 在 filter_fields 中而被保留")
                                break
                else:
                    keep = True

                if not keep:
                    continue

                # 【v47.x 新增】为参照字段补充 referenceStructure
                if attr.get("biztype") == "quote" and attr.get("refUri"):
                    _supplement_reference_structure(attr, cfg)
                existing.append(attr)
                existing_cols.add(col)

            ent["attributes"] = existing

        # 【v47.x 新增】为已有的参照字段补充 referenceStructure（原 API 返回的属性）
        existing = ent.get("attributes", [])
        for attr in existing:
            if isinstance(attr, dict) and attr.get("biztype") == "quote" and attr.get("refUri"):
                if "referenceStructure" not in attr:
                    _supplement_reference_structure(attr, cfg)

    return entities


def _query_by_uri_cached(cfg: dict, uri: str) -> str:
    """
    queryByUri 统一入口：进程内缓存 → Redis + 磁盘持久缓存 → 避免重复请求相同 URI。
    HTTP 层超时由 api.http_timeout_seconds 控制（见 bip_auth）。

    缓存层级（v7.0 优化）：
      1. 进程内对象缓存（_uri_object_cache）→ 零解析开销，快速路径
      2. Redis 缓存 → 跨进程共享（仅当进程内未命中时检查）
      3. 磁盘缓存 → 本地持久化
      4. 网络请求 → 存入 Redis + 磁盘 + 进程内缓存

    【v7.0 优化】进程内缓存命中时直接返回，跳过 Redis/磁盘检查，减少 ~15ms 开销。
    """
    # 【优化 v7.0】快速路径：进程内缓存命中时直接返回，跳过 Redis/磁盘检查
    cached_obj = _uri_object_cache.get_obj(uri)
    if cached_obj is not None:
        logger.debug(f"进程内对象缓存命中（快速路径）: {uri}")
        cached_json = _uri_object_cache.get_json(uri)
        if cached_json is not None:
            return cached_json
        return json.dumps(cached_obj, ensure_ascii=False)

    cached_json_str = _uri_object_cache.get_json(uri)
    if cached_json_str is not None:
        logger.debug(f"进程内 JSON 缓存命中（快速路径）: {uri}")
        try:
            obj = json.loads(cached_json_str)
            _uri_object_cache.put_both(uri, cached_json_str, obj)
        except json.JSONDecodeError:
            pass
        return cached_json_str

    # 【慢速路径】进程内未命中，继续检查 Redis/磁盘
    # 1. 检查 Redis 缓存（跨进程共享）
    redis_cached = get_cached_metadata(uri)
    if redis_cached:
        logger.info(f"[METADATA] Redis缓存命中: {uri}")
        try:
            obj = json.loads(redis_cached)
            _uri_object_cache.put_both(uri, redis_cached, obj)
            # 【v40.x 新增】存入 session_cache
            _store_query_uri_to_session_cache(cfg, uri, obj)
        except json.JSONDecodeError:
            _uri_object_cache.put_json(uri, redis_cached)
        return redis_cached

    # 2. 检查磁盘缓存
    cached_disk = _get_cached_uri_disk(uri)
    if cached_disk:
        logger.info(f"[METADATA] 磁盘缓存命中: {uri}")
        set_cached_metadata(uri, cached_disk)
        try:
            obj = json.loads(cached_disk)
            _uri_object_cache.put_both(uri, cached_disk, obj)
            # 【v40.x 新增】存入 session_cache
            _store_query_uri_to_session_cache(cfg, uri, obj)
        except json.JSONDecodeError:
            _uri_object_cache.put_json(uri, cached_disk)
        return cached_disk

    # 3. 网络请求
    api = cfg.get("api") or {}
    meta_uri_path = api.get("metadata_uri", "")
    logger.info(f"[METADATA] API请求: {uri}")
    j = http_get_json(cfg, meta_uri_path, {"uri": uri})
    s = json.dumps(j, ensure_ascii=False)

    # 存入缓存（磁盘 + 进程内 + 【v7.0 优化】缓冲写入 Redis）
    _set_cached_uri_disk(uri, s)
    _uri_object_cache.put_both(uri, s, j)
    _buffer_redis_write(uri, s)  # 【v7.0 优化】使用缓冲写入，减少 Redis 网络 RTT

    # 【v40.x 新增】存入 session_cache（raw_query_uri key 格式与 entity_info 一致）
    _store_query_uri_to_session_cache(cfg, uri, j)

    return s


def _query_by_uri_cached_parsed(cfg: dict, uri: str, schema: str = "") -> Any:
    """
    queryByUri 并返回已解析的对象（v6.0 优化：复用进程内对象缓存）。

    注意：优先返回进程内缓存的对象，避免 json.loads() 重复解析。

    【v16.x 新增】如果传入 schema，会将 schema 注入到返回的对象和缓存中。
    【v26.x 修复】schema 需要注入到 data 节点，因为 parse 函数从 data_node 获取 schema。
    """
    # 【优化 v6.0】直接返回进程内缓存的对象（零解析）
    cached_obj = _uri_object_cache.get_obj(uri)
    if cached_obj is not None:
        # 【v26.x 修复】如果传入了 schema 且缓存对象的 data 节点没有 schema，注入 schema
        if schema:
            data_node = cached_obj.get("data") if isinstance(cached_obj, dict) else None
            if data_node and not data_node.get("schema"):
                data_node["schema"] = schema
        return cached_obj

    # 兜底：走完整缓存链
    s = _query_by_uri_cached(cfg, uri)
    try:
        obj = json.loads(s)
        # 【v26.x 修复】将 schema 注入到 data 节点（parse 函数从 data_node 获取 schema）
        if schema:
            data_node = obj.get("data") if isinstance(obj, dict) else None
            if data_node and not data_node.get("schema"):
                data_node["schema"] = schema
                # 更新缓存中的 JSON 字符串
                s = json.dumps(obj, ensure_ascii=False)
                _uri_object_cache.put_json(uri, s)
        _uri_object_cache.put_obj(uri, obj)
        return obj
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse URI {uri}: {e}")
        raise


def _fetch_schema_via_three_apis(cfg: dict, uri: str) -> Optional[str]:
    """
    【v12.x 新增】通过 searchByName → getEntityListByBOId → getEntityInfoByBOIdAndEntityId 三级调用获取 schema。

    入参：
        uri: BIP 业务对象 URI，如 "aa.merchant.Merchant"

    返回：
        schema 字符串，或 None（获取失败时）

    逻辑：
        1. 直接使用 URI 作为 searchByName 的搜索 key
        2. searchByName 返回多个结果时，严格用 URI 精确匹配
        3. 获取 id 后，调用 getEntityListByBOId → getEntityInfoByBOIdAndEntityId
        4. 返回 schema
    """
    import re

    # 【v15.x 优化】URI 级缓存命中检查
    # 【v42.x 修复】即使 schema 缓存命中，也检查 session_cache 中是否有 entity_info
    global _session_cache
    if uri in _schema_three_apis_cache:
        cached = _schema_three_apis_cache[uri]
        cache_key = _build_entity_info_cache_key(cfg, uri)
        # 检查 session_cache 中是否已有 entity_info（包含 businessProperties）
        if _session_cache and cache_key in _session_cache:
            logger.info(f"[Three APIs] URI 缓存命中且 entity_info 已存在: {uri}")
        else:
            logger.info(f"[Three APIs] URI 缓存命中但 entity_info 不存在，将调用 getEntityInfo: {uri}")
            # 需要重新调用 getEntityInfoByBOIdAndEntityId 以获取 businessProperties
            # 跳过 schema 缓存，直接走后续流程
        if cached is not None and (_session_cache and cache_key in _session_cache):
            logger.info(f"[Three APIs] URI 缓存命中（直接返回）: {uri} → {cached}")
            return cached
        # 否则继续执行，调用 getEntityInfoByBOIdAndEntityId

    api = cfg.get("api") or {}
    byname_path = api.get("metadata_byname", "")
    byboid_path = api.get("metadata_byboid", "")
    ent_path = api.get("metadata_entityid", "")

    # 1. 直接使用 URI 作为 searchByName 的搜索 key
    if not uri:
        logger.warning(f"[Three APIs] URI 为空")
        _schema_three_apis_cache[uri] = None  # v15.x cache write
        return None

    # 2. 调用 searchByName
    logger.info(f"[Three APIs] searchByName 请求: {uri}")
    try:
        raw = http_get_json(cfg, byname_path, {"key": uri})
    except Exception as e:
        logger.warning(f"[Three APIs] searchByName 请求失败: {e}")
        _schema_three_apis_cache[uri] = None  # v15.x cache write
        return None

    # 3. 解析 searchByName 结果，找精确匹配 URI 的项
    data_node = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data_node, dict):
        logger.warning(f"[Three APIs] searchByName 返回数据格式异常: {raw}")
        _schema_three_apis_cache[uri] = None  # v15.x cache write
        return None

    # searchByName 返回结构可能是 {bill: [...]} 或 {BUSINESSOBJECT: [...], METACLASS: [...]} 或 {data: [{entitys: [{uri: ...}]}]}
    # 需要同时搜索顶层 bill 和嵌套的 BUSINESSOBJECT/METACLASS 的 children 数组，以及 entitys 数组
    bill_list = data_node.get("bill") or []

    # 严格 URI 匹配
    matched = None
    matched_boid = None  # 如果 URI 在 children 中找到，需要用 parent 的 id
    matched_code = None

    # 0. 【v43.x 新增】搜索 data.entitys 数组（部分 API 返回结构）
    if not matched:
        data_list = data_node.get("data") or []
        if isinstance(data_list, list):
            for parent in data_list:
                if not isinstance(parent, dict):
                    continue
                entitys = parent.get("entitys") or []
                parent_id = parent.get("id") or parent.get("boId")
                parent_code = parent.get("code") or ""
                for entity in entitys:
                    entity_uri = entity.get("uri") or ""
                    if entity_uri == uri:
                        matched = entity
                        matched_boid = parent_id  # 用 parent 的 id
                        matched_code = parent_code  # 用 parent 的 code
                        logger.info(f"[Three APIs] searchByName URI 精确匹配（data.entitys）: {uri}")
                        break
                if matched:
                    break

    # 1. 先搜索顶层 bill
    if not matched:
        for item in bill_list:
            item_uri = item.get("uri") or ""
            if item_uri == uri:
                matched = item
                matched_boid = matched.get("id") or matched.get("boId")
                matched_code = matched.get("code") or ""
                logger.info(f"[Three APIs] searchByName URI 精确匹配（顶层）: {uri}")
                break

    # 2. 如果顶层没找到，搜索 BUSINESSOBJECT/METACLASS 的 children
    if not matched:
        for key in ("BUSINESSOBJECT", "METACLASS"):
            for category in data_node.get(key, []) or []:
                if not isinstance(category, dict):
                    continue
                parent_id = category.get("id") or category.get("boId")
                parent_code = category.get("code") or ""
                children = category.get("children") or []
                for child in children:
                    child_uri = child.get("uri") or ""
                    if child_uri == uri:
                        matched = child
                        matched_boid = parent_id  # 用 parent 的 id
                        matched_code = parent_code  # 用 parent 的 code
                        logger.info(f"[Three APIs] searchByName URI 精确匹配（{key} children）: {uri}")
                        break
                if matched:
                    break
            if matched:
                break

    if not matched:
        # 没找到精确匹配，取第一个
        if bill_list:
            matched = bill_list[0]
            matched_boid = matched.get("id") or matched.get("boId")
            matched_code = matched.get("code") or ""
        else:
            # 尝试从 BUSINESSOBJECT/METACLASS 取第一个
            # 【v35.x 修复】优先选择不带 "IBpmStep" 后缀的实体（主表），避免取到审批变体
            for key in ("BUSINESSOBJECT", "METACLASS"):
                categories = data_node.get(key, []) or []
                for cat in categories:
                    if not isinstance(cat, dict):
                        continue
                    children = cat.get("children") or []
                    if not children or not isinstance(children[0], dict):
                        continue
                    # 优先找主表（不带 IBpmStep 后缀）
                    main_entity = None
                    for child in children:
                        child_uri = child.get("uri", "")
                        # 排除 IBpmStep 变体
                        if child_uri and not child_uri.endswith("IBpmStep"):
                            main_entity = child
                            break
                    # 如果没找到主表，用第一个（兼容旧逻辑）
                    if main_entity is None:
                        main_entity = children[0]
                    matched = main_entity
                    matched_boid = cat.get("id") or cat.get("boId")
                    matched_code = cat.get("code") or ""
                    logger.info(f"[Three APIs] searchByName 选中主表（排除IBpmStep）: {matched.get('uri')}")
                    break
                if matched:
                    break
        logger.warning(f"[Three APIs] searchByName 未找到 URI={uri}，使用: {matched.get('uri') if matched else 'None'}")

    if not matched:
        logger.warning(f"[Three APIs] searchByName 未能找到有效的业务对象: {uri}")
        _schema_three_apis_cache[uri] = None  # v15.x cache write
        return None

    # 判断 parent id 是否有效（parent id 为空时使用 child id）
    parent_id_valid = bool(matched_boid and matched_boid != matched.get("id"))
    use_child_id = not parent_id_valid and bool(matched.get("id"))

    if not matched_boid and not use_child_id:
        logger.warning(f"[Three APIs] searchByName 未能找到有效的业务对象: {uri}")
        _schema_three_apis_cache[uri] = None  # v15.x cache write
        return None

    boid = matched_boid if matched_boid else ""
    code = matched_code
    bill_uri = matched.get("uri") or ""

    entity_id = None
    if use_child_id:
        # parent id 为空，直接使用 child id 作为 entity_id，跳过 getEntityListByBOId
        entity_id = matched.get("id")
        ent_boid = entity_id
        ent_code = ""
        logger.info(f"[Three APIs] 跳过 getEntityListByBOId，直接使用 child id: {entity_id}")
        # parent id 为空，直接使用 child id 作为 entity_id，跳过 getEntityListByBOId
        entity_id = matched.get("id")
        ent_boid = entity_id
        ent_code = ""
        logger.info(f"[Three APIs] 跳过 getEntityListByBOId，直接使用 child id: {entity_id}")
    else:
        # 正常流程：先调用 getEntityListByBOId
        logger.info(f"[Three APIs] getEntityListByBOId 请求: boId={boid}, code={code}")
        try:
            raw_boid = http_get_json(cfg, byboid_path, {"boId": boid, "businessObjectCode": code})
        except Exception as e:
            logger.warning(f"[Three APIs] getEntityListByBOId 请求失败: {e}")
            _schema_three_apis_cache[uri] = None  # v15.x cache write
            return None

        entity_list = raw_boid.get("data") or {}
        if isinstance(entity_list, dict):
            inner = entity_list.get("data")
            if isinstance(inner, dict):
                entity_list = inner.get("entities") or []
            else:
                entity_list = entity_list.get("entities") or []
        if not entity_list:
            logger.warning(f"[Three APIs] getEntityListByBOId 返回空: boid={boid}")
            _schema_three_apis_cache[uri] = None  # v15.x cache write
            return None

        if isinstance(entity_list, list) and len(entity_list) > 0:
            first_entity = entity_list[0]
        else:
            first_entity = entity_list
        entity_id = _text(first_entity, "id") if isinstance(first_entity, dict) else None
        ent_boid = _text(first_entity, "businessObjectId") if isinstance(first_entity, dict) else None
        ent_code = _text(first_entity, "businessObjectCode") if isinstance(first_entity, dict) else None
        if not entity_id:
            logger.warning(f"[Three APIs] getEntityListByBOId 结果缺少 entity id: {first_entity}")
            _schema_three_apis_cache[uri] = None  # v15.x cache write
            return None

    # 5. 调用 getEntityInfoByBOIdAndEntityId
    # 【v16.x 新增】先检查 session 缓存
    cache_key = _build_entity_info_cache_key(cfg, uri)
    if _session_cache and cache_key in _session_cache:
        logger.info(f"[Three APIs] session 缓存命中: {cache_key}")
        raw_ent = _session_cache[cache_key]
    else:
        logger.info(f"[Three APIs] getEntityInfoByBOIdAndEntityId 请求: entityId={entity_id}, boId={ent_boid or boid}")
        try:
            raw_ent = http_get_json(cfg, ent_path, {
                "entityId": entity_id,
                "uri": bill_uri or "",
                "boId": ent_boid or boid or "",
                "businessObjectCode": ent_code or code or "",
            })
            # 【v16.x 新增】结果存入 session 缓存
            if _session_cache is not None:
                _session_cache[cache_key] = raw_ent
                logger.info(f"[Three APIs] 结果已写入 session 缓存: {cache_key}")
        except Exception as e:
            logger.warning(f"[Three APIs] getEntityInfoByBOIdAndEntityId 请求失败: {e}")
            _schema_three_apis_cache[uri] = None  # v15.x cache write
            return None

    # 6. 从返回结果中提取 schema
    ent_data = raw_ent.get("data") if isinstance(raw_ent, dict) else None
    if isinstance(ent_data, dict):
        # 尝试直接获取 schema
        schema = ent_data.get("schema")
        if schema:
            logger.info(f"[Three APIs] 获取到 schema: {uri} → {schema}")
            _schema_three_apis_cache[uri] = schema  # v15.x cache write
            return schema
        # 嵌套结构：data.data.schema
        inner = ent_data.get("data")
        if isinstance(inner, dict):
            schema = inner.get("schema")
            if schema:
                logger.info(f"[Three APIs] 从 data.data 获取 schema: {uri} → {schema}")
                _schema_three_apis_cache[uri] = schema  # v15.x cache write
                return schema
        # 兜底：从 groups 中查找 schema
        groups = inner.get("groups") if isinstance(inner, dict) else None
        if isinstance(groups, list):
            for group in groups:
                if isinstance(group, dict) and group.get("schema"):
                    logger.info(f"[Three APIs] 从 groups 获取 schema: {uri} → {group.get('schema')}")
                    _schema_three_apis_cache[uri] = schema  # v15.x cache write
                    return group.get("schema")

    logger.warning(f"[Three APIs] 未能从 API 响应中提取 schema: {uri}")
    _schema_three_apis_cache[uri] = None  # v15.x cache write
    return None


def _query_by_uri_batch(cfg: dict, uris: List[str]) -> Dict[str, Any]:
    """
    【v7.0 优化】批量查询 URI 并返回已解析的对象字典。

    性能优化：
    - 先批量检查 Redis mget，一次性获取多个 URI
    - 返回 {uri: obj} 字典，调用方直接使用无需 json.loads()
    - 自动处理缓存未命中的 URI
    - 【v7.0 新增】收集所有新获取的 URI，使用 Pipeline 批量写入 Redis
    """
    from redis_cache import set_cached_metadata_batch

    result: Dict[str, Any] = {}
    # 【v7.0 新增】收集需要写入 Redis 的 (uri, json_str) 对
    redis_write_buffer: List[Tuple[str, str]] = []

    # 1. 过滤出需要查询的 URI（不在进程内缓存的）
    uris_to_fetch: List[str] = []
    for uri in uris:
        cached_obj = _uri_object_cache.get_obj(uri)
        if cached_obj is not None:
            result[uri] = cached_obj
        else:
            uris_to_fetch.append(uri)

    if not uris_to_fetch:
        logger.debug(f"_query_by_uri_batch: 全部 {len(uris)} 个 URI 命中进程内缓存")
        return result

    # 2. 批量检查 Redis
    redis_batch = get_cached_metadata_batch(uris_to_fetch)
    for uri, json_str in redis_batch.items():
        try:
            obj = json.loads(json_str)
            result[uri] = obj
            _uri_object_cache.put_both(uri, json_str, obj)
            # 【v40.x 新增】存入 session_cache
            _store_query_uri_to_session_cache(cfg, uri, obj)
            uris_to_fetch.remove(uri)
        except json.JSONDecodeError:
            _uri_object_cache.put_json(uri, json_str)

    if not uris_to_fetch:
        logger.info(f"[METADATA] 批量查询: Redis命中 {len(redis_batch)}/{len(uris)} 个 URI")
        return result

    # 3. 【优化 v7.0】未命中的 URI 使用 ThreadPoolExecutor 并发请求
    # 替代原有串行循环，充分利用网关并发能力
    if uris_to_fetch:
        max_workers = _max_concurrent_uri(cfg)
        logger.info(f"[METADATA] 并发查询 {len(uris_to_fetch)} 个 URI (并发度={max_workers})")

        # 【v7.0 新增】线程安全的写入缓冲区
        buffer_lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_uri = {
                executor.submit(_query_by_uri_cached, cfg, uri): uri
                for uri in uris_to_fetch
            }

            for future in as_completed(future_to_uri):
                uri = future_to_uri[future]
                try:
                    s = future.result()
                    try:
                        obj = json.loads(s)
                        result[uri] = obj
                        # 【v7.0 新增】收集到写入缓冲区
                        with buffer_lock:
                            redis_write_buffer.append((uri, s))
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse URI {uri}: {e}")
                except Exception as e:
                    logger.error(f"URI query failed: {uri} - {e}")

    # 4. 【v7.0 新增】批量写入 Redis（使用 Pipeline）
    if redis_write_buffer:
        logger.info(f"[METADATA] 批量写入 Redis: {len(redis_write_buffer)} 条")
        try:
            set_cached_metadata_batch(redis_write_buffer)
        except Exception as e:
            logger.warning(f"Redis 批量写入失败: {e}")

    return result


def fetch_entity_db_info_with_timeout(cfg: dict, uri: str) -> Optional[str]:
    """
    批量路径使用的 queryByUri（失败返回 None）
    超时由 api.http_timeout_seconds 控制。
    """
    try:
        return _query_by_uri_cached(cfg, uri)
    except Exception as e:
        logger.error(f"获取实体信息失败 [uri={uri}]: {e}")
        return None


def fetch_entity_db_info_batch(
    cfg: dict, uris: Set[str]
) -> Dict[str, Any]:
    """
    【v6.0 优化】批量获取实体数据库信息，返回已解析的对象字典。

    性能优化：
    - 使用 _query_by_uri_batch 直接返回 {uri: obj} 字典
    - 调用方直接使用对象，无需 json.loads()
    - 自动处理 Redis mget 批量查询
    """
    if not uris:
        return {}

    # 【v6.0 优化】直接使用批量查询接口，返回已解析的对象
    result = _query_by_uri_batch(cfg, list(uris))

    logger.info(f"批量查询完成: 获取 {len(result)}/{len(uris)} 个实体对象")
    return result


def fetch_entity_db_info_with_timeout(cfg: dict, uri: str) -> Optional[Any]:
    """
    【保留】单个 URI 查询，返回已解析的对象。
    优先使用 _query_by_uri_cached_parsed 或 _query_by_uri_batch。
    """
    return _query_by_uri_cached_parsed(cfg, uri)


@dataclass
class CodeNameResult:
    """业务对象编码和名称解析结果"""
    code: Optional[str] = None
    name: Optional[str] = None
    id: Optional[str] = None
    uri: Optional[str] = None  # 【v9.0 新增】URI 字段
    available_names: Optional[List[str]] = None
    available_items: Optional[List[Dict[str, Optional[str]]]] = None
    not_found: bool = False
    not_found_reason: Optional[str] = None

    def needs_selection(self) -> bool:
        return bool(self.available_items and len(self.available_items) > 1)


def parse_business_object_code_name(result: Any, billname: str, cfg: dict = None) -> Optional[CodeNameResult]:
    """解析业务对象响应，提取编码和名称"""
    import re
    if not isinstance(result, dict):
        return None
    code_val = _text(result, "code") or _text(result, "resultCode")
    if code_val != "200":
        return None
    data_node = result.get("data")
    if not isinstance(data_node, dict):
        return None
    target_nodes: List[dict] = []
    inner_data_array = data_node.get("data")
    if isinstance(inner_data_array, list):
        for item in inner_data_array:
            if isinstance(item, dict):
                target_nodes.append(item)
    nodes_with_parent: List[Tuple[dict, Optional[str], Optional[str]]] = []  # (child, parent_code, parent_name)
    if not target_nodes:
        bo_array = data_node.get("METACLASS")
        if isinstance(bo_array, list):
            for bo in bo_array:
                if not isinstance(bo, dict):
                    continue
                parent_code = _text(bo, "code")
                parent_name = _text(bo, "name")
                children = bo.get("children")
                if isinstance(children, list):
                    for child in children:
                        if isinstance(child, dict):
                            nodes_with_parent.append((child, parent_code, parent_name))

    # 【v35.x 调试】打印 API 返回的原始数据
    logger.info(f"[v35.x DEBUG] parse_business_object_code_name 输入: billname={billname}")
    logger.info(f"  target_nodes: {len(target_nodes)} 个, nodes_with_parent: {len(nodes_with_parent)} 个")

    search_name = (billname or "").strip()
    # 【v15.x 修复】去掉括号后缀，转换为真实业务对象名称用于 METACLASS 匹配
    search_name = re.sub(r"[（(][^）)]*?[）)]$", "", search_name).strip()

    # 【v11.x 新增】检测 code=xxx 格式，使用预选的 item 直接返回，跳过 API 搜索
    # 格式：原名（code=xxx）由自动选择逻辑生成
    code_match = re.search(r'[（(]code=([^）)]+)[）)]', search_name)
    if code_match:
        direct_code = code_match.group(1).strip()
        # 从请求上下文的 _pre_selected_items 中查找对应 code 的 item
        req = (cfg or {}).get("request", {}) if cfg else {}
        pre_selected = req.get("_pre_selected_items") or []
        matched_item = None
        for item in pre_selected:
            if item.get("code") == direct_code:
                matched_item = item
                break
        if matched_item:
            logger.info(f"[自动选择 v11.x] 匹配到预选 item: code={direct_code}, name={matched_item.get('name')}")
            return CodeNameResult(
                code=matched_item.get("code"),
                name=matched_item.get("name"),
                id=matched_item.get("id"),
                uri=matched_item.get("uri")
            )
        else:
            logger.warning(f"[自动选择 v11.x] 未在 _pre_selected_items 中找到 code={direct_code}")

    def _dedupe_by_code(
        items: List[Dict[str, Optional[str]]]
    ) -> List[Dict[str, Optional[str]]]:
        seen: Set[str] = set()
        out: List[Dict[str, Optional[str]]] = []
        for it in items:
            c = (it.get("code") or "").strip()
            if not c or c in seen:
                continue
            seen.add(c)
            out.append(it)
        return out

    exact_tn: List[Dict[str, Optional[str]]] = []
    for node in target_nodes:
        name = _text(node, "name")
        if name == search_name:
            code = _text(node, "code")
            id_ = _text(node, "id")
            uri_ = _text(node, "uri")  # 【v9.0 新增】提取 URI
            if code and code != "null":
                exact_tn.append({"code": code, "name": name, "id": id_, "uri": uri_})
    exact_tn = _dedupe_by_code(exact_tn)
    if len(exact_tn) == 1:
        o = exact_tn[0]
        return CodeNameResult(code=o["code"], name=o["name"], id=o.get("id"), uri=o.get("uri"))
    if len(exact_tn) > 1:
        names = [f"{x.get('name') or ''}（{x.get('code') or ''}）" for x in exact_tn]
        return CodeNameResult(available_names=names, available_items=exact_tn)

    # 检查是否为空数据（查无此单据）
    if not target_nodes and not nodes_with_parent:
        logger.warning(
            f"searchByName 返回空数据，未找到业务对象: billname={search_name}，"
            f"请检查单据名称是否正确，或该单据是否已发布到 BIP 平台"
        )
        # 【v11.1 新增】FastLookup 回退：当 API 返回空时，尝试从 metadata_lookup.json 查找
        try:
            fast_lookup = _get_fast_lookup(cfg)
            if fast_lookup.is_loaded:
                hits = fast_lookup.strict_lookup(search_name)
                if hits:
                    first = hits[0]
                    logger.info(f"[FastLookup 回退] searchByName 空数据，通过 FastLookup 找到: {first.uri}, schema={first.schema}")
                    if first.uri:
                        # 构造一个有效的 CodeNameResult，通过 queryUri 路径继续处理
                        r = CodeNameResult(
                            code=first.metadata_name or first.biz_name or search_name,
                            name=first.biz_name or search_name,
                            id=None,  # FastLookup 没有 id，需要后续通过 queryUri 获取
                            uri=first.uri
                        )
                        r._fastlookup_schema = first.schema  # 传递 schema 信息
                        r._fastlookup_match = True
                        return r
        except Exception as e:
            logger.warning(f"[FastLookup 回退] 查找失败: {e}")
        # FastLookup 也找不到，返回 not_found
        r = CodeNameResult()
        r.not_found = True
        r.not_found_reason = (
            f"searchByName 返回空数据，未找到名为 [{search_name}] 的业务对象。"
            f"请确认：（1）单据名称正确；（2）该单据已在 BIP 平台发布"
        )
        return r

    exact_wp: List[Dict[str, Optional[str]]] = []
    for node, parent_code, parent_name in nodes_with_parent:
        name = _text(node, "name")
        if name == search_name and parent_code and parent_code != "null":
            uri_ = _text(node, "uri")  # 【v9.0 新增】提取 URI
            exact_wp.append(
                {"code": parent_code, "name": name, "id": _text(node, "id"), "uri": uri_, "_parent_name": parent_name or ""}
            )
    exact_wp = _dedupe_by_code(exact_wp)

    # 【v35.x 调试】打印 exact_wp 收集结果
    if exact_wp:
        logger.info(f"[v35.x DEBUG] exact_wp 收集结果: {len(exact_wp)} 个, search_name={search_name}")
        for i, item in enumerate(exact_wp):
            logger.info(f"  [{i}] uri={item.get('uri')}, name={item.get('name')}, parent={item.get('_parent_name')}")

    # 【v17.x 重构】多匹配时使用 parent_hint 自动过滤
    # 支持两种格式：
    # 1. "授信信息（票据登记的子表）" → "票据登记"
    # 2. "授信信息（票据信息）" → "票据信息"
    if len(exact_wp) > 1:
        m = re.search(r'[（(]([^）)]+)[）)]', (billname or "").strip())
        parent_hint = m.group(1).replace("的", "").strip() if m else ""
        if parent_hint:
            # 先尝试精确匹配 parent_hint
            filtered = [x for x in exact_wp if parent_hint == (x.get("_parent_name") or "")]
            if not filtered:
                # 再尝试包含匹配
                filtered = [x for x in exact_wp if parent_hint in (x.get("_parent_name") or "")]
            if filtered:
                exact_wp = filtered
                logger.info(f"[METACLASS 多匹配 v17.x] parent_hint={parent_hint!r} 过滤后剩余 {len(exact_wp)} 个")

    # 【v35.x 修复】exact_wp 阶段也过滤 IBpmStep 后缀
    if len(exact_wp) == 1:
        o = exact_wp[0]
        # 如果唯一匹配是 IBpmStep 变体，检查是否有多于1个候选（需要重新收集）
        if o.get("uri", "").endswith("IBpmStep"):
            # 尝试找主表
            main_candidates = [item for item in exact_wp if not item.get("uri", "").endswith("IBpmStep")]
            if main_candidates:
                o = main_candidates[0]
                logger.info(f"[v35.x] exact_wp 过滤 IBpmStep，选择主表: {o.get('uri')}")
            else:
                # 只有 IBpmStep，直接返回（避免无结果）
                logger.info(f"[v35.x] exact_wp 仅剩 IBpmStep 变体: {o.get('uri')}")
        return CodeNameResult(code=o["code"], name=o["name"], id=o.get("id"), uri=o.get("uri"))
    if len(exact_wp) > 1:
        # 【v35.x 修复】多匹配时过滤 IBpmStep
        main_items = [x for x in exact_wp if not x.get("uri", "").endswith("IBpmStep")]
        if main_items:
            exact_wp = main_items
            logger.info(f"[v35.x] exact_wp 过滤 IBpmStep 变体后剩余 {len(exact_wp)} 个")
        if len(exact_wp) == 1:
            o = exact_wp[0]
            return CodeNameResult(code=o["code"], name=o["name"], id=o.get("id"), uri=o.get("uri"))
        names = [f"{x.get('name') or ''}（{x.get('code') or ''}）" for x in exact_wp]
        return CodeNameResult(available_names=names, available_items=exact_wp)

    sub_tn: List[Dict[str, Optional[str]]] = []
    for node in target_nodes:
        name = _text(node, "name")
        if name and search_name in name:
            code = _text(node, "code")
            id_ = _text(node, "id")
            uri_ = _text(node, "uri")  # 【v9.0 新增】提取 URI
            if code and code != "null":
                sub_tn.append({"code": code, "name": name, "id": id_, "uri": uri_})
    sub_tn = _dedupe_by_code(sub_tn)

    # 【v35.x 调试】打印 sub_tn 收集结果
    logger.info(f"[v35.x DEBUG] sub_tn 收集结果: {len(sub_tn)} 个, search_name={search_name}")
    for i, item in enumerate(sub_tn):
        logger.info(f"  [{i}] uri={item.get('uri')}, name={item.get('name')}")

    if len(sub_tn) == 1:
        o = sub_tn[0]
        return CodeNameResult(code=o["code"], name=o["name"], id=o.get("id"), uri=o.get("uri"))
    if len(sub_tn) > 1:
        names = [f"{x.get('name') or ''}（{x.get('code') or ''}）" for x in sub_tn]
        return CodeNameResult(available_names=names, available_items=sub_tn)

    sub_wp: List[Dict[str, Optional[str]]] = []
    # 【v35.x 修复】先尝试精确匹配，再尝试子串匹配
    exact_matches: List[Dict[str, Optional[str]]] = []
    partial_matches: List[Dict[str, Optional[str]]] = []
    for node, parent_code, parent_name in nodes_with_parent:
        name = _text(node, "name")
        if name and parent_code and parent_code != "null":
            uri_ = _text(node, "uri")
            item = {"code": parent_code, "name": name, "id": _text(node, "id"), "uri": uri_, "_parent_name": parent_name or ""}
            # 精确匹配
            if name == search_name:
                exact_matches.append(item)
            # 子串匹配（排除已经精确匹配的）
            elif search_name in name:
                partial_matches.append(item)
    sub_wp = _dedupe_by_code(exact_matches) or _dedupe_by_code(partial_matches) or []

    # 【v35.x 调试】打印 sub_wp 收集结果
    logger.info(f"[v35.x DEBUG] sub_wp 收集结果: {len(sub_wp)} 个, search_name={search_name}")
    for i, item in enumerate(sub_wp):
        logger.info(f"  [{i}] uri={item.get('uri')}, name={item.get('name')}, parent={item.get('_parent_name')}")

    # 【v15.x 新增】多匹配时使用 parent_hint 自动过滤
    if len(sub_wp) > 1:
        m = re.search(r'[（(]([^）)]*?)的?子表[）)]', (billname or "").strip())
        parent_hint = m.group(1).replace("的", "").strip() if m else ""
        if parent_hint:
            filtered = [x for x in sub_wp if parent_hint in (x.get("_parent_name") or "")]
            if filtered:
                sub_wp = filtered
                logger.info(f"[sub_wp 多匹配 v15.x] parent_hint={parent_hint!r} 过滤后剩余 {len(sub_wp)} 个")

    # 【v35.x 修复】过滤 IBpmStep 变体
    if len(sub_wp) >= 1:
        logger.info(f"[v35.x DEBUG] sub_wp 过滤前: {len(sub_wp)} 个, search_name={search_name}")
        for i, x in enumerate(sub_wp):
            logger.info(f"  [{i}] full_item={json.dumps(x, ensure_ascii=False)}")
        # 过滤掉包含 IBpmStep/审批 的项
        filtered = [x for x in sub_wp if not any(kw in ((x.get("uri") or "") + (x.get("name") or "")) for kw in ["IBpmStep", "审批"])]
        if filtered:
            sub_wp = filtered
            logger.info(f"[v35.x] sub_wp 过滤 IBpmStep/审批 变体后剩余 {len(sub_wp)} 个")
        else:
            logger.info(f"[v35.x] sub_wp 过滤后为空，需要返回 parent 信息")
            sub_wp = []  # 【v35.x 修复】必须显式清空，否则保持原值
            logger.info(f"[v35.x] 准备进入 if not sub_wp: 检查")

    # 【v35.x 修复】如果过滤后为空，返回 METACLASS parent 信息
    logger.info(f"[v35.x] 检查 if not sub_wp: sub_wp={sub_wp}, len={len(sub_wp)}")
    if not sub_wp:
        logger.info(f"[v35.x DEBUG] 进入 parent 查找，search_name={search_name}")
        bo_array = data_node.get("METACLASS") if isinstance(data_node, dict) else None
        logger.info(f"[v35.x DEBUG] METACLASS: {len(bo_array) if isinstance(bo_array, list) else 'None'}")
        if isinstance(bo_array, list):
            for bo in bo_array:
                if not isinstance(bo, dict):
                    continue
                parent_name = _text(bo, "name")
                logger.info(f"[v35.x DEBUG] 检查 METACLASS parent: name={parent_name}, search_name={search_name}")
                if parent_name == search_name:
                    parent_id = _text(bo, "id")
                    parent_code = _text(bo, "code")
                    if parent_id or parent_code:
                        logger.info(f"[v35.x] sub_wp 过滤后为空，返回 METACLASS parent: id={parent_id}, code={parent_code}")
                        return CodeNameResult(
                            code=parent_code,
                            name=parent_name,
                            id=parent_id,
                            uri=None
                        )

    if len(sub_wp) == 1:
        o = sub_wp[0]
        return CodeNameResult(code=o["code"], name=o["name"], id=o.get("id"), uri=o.get("uri"))
    if len(sub_wp) > 1:
        names = [f"{x.get('name') or ''}（{x.get('code') or ''}）" for x in sub_wp]
        return CodeNameResult(available_names=names, available_items=sub_wp)

    # 【v11.x 新增】无业务对象实体 fallback：当正常的 METACLASS（有 parent_code）没有找到任何实体时，
    # 检查 "无业务对象实体" 类型 METACLASS（parent_code 为 null，但 children 有有效 id）
    # 类似 _fetch_schema_via_three_apis 中的 use_child_id 逻辑
    no_parent_nodes: List[Dict[str, Optional[str]]] = []
    for node, parent_code, parent_name in nodes_with_parent:
        if parent_code is None or parent_code == "null":
            name = _text(node, "name")
            if name == search_name:
                node_id = _text(node, "id")
                uri_ = _text(node, "uri")
                if node_id and node_id != "null":
                    # use_child_id 逻辑：parent id 无效但 child id 有效，跳过 getEntityListByBOId
                    # 注意：这里 code 为空串，不使用 _dedupe_by_code（它会过滤掉空 code）
                    no_parent_nodes.append({"code": "", "name": name, "id": node_id, "uri": uri_})
    if len(no_parent_nodes) == 1:
        o = no_parent_nodes[0]
        logger.info(f"[无业务对象实体 fallback] 匹配到: name={o['name']}, id={o['id']}, uri={o['uri']}")
        return CodeNameResult(code=o["code"], name=o["name"], id=o.get("id"), uri=o.get("uri"))
    if len(no_parent_nodes) > 1:
        names = [f"{x.get('name') or ''}（id={x.get('id') or ''}）" for x in no_parent_nodes]
        return CodeNameResult(available_names=names, available_items=no_parent_nodes)

    # 【v35.x 新增】当 children 中没有精确匹配时，返回 METACLASS parent 信息
    # 让调用方通过 getEntityListByBOId 获取主表
    bo_array = data_node.get("METACLASS") if isinstance(data_node, dict) else None
    if isinstance(bo_array, list):
        for bo in bo_array:
            if not isinstance(bo, dict):
                continue
            parent_name = _text(bo, "name")
            if parent_name == search_name:
                parent_id = _text(bo, "id")
                parent_code = _text(bo, "code")
                if parent_id or parent_code:
                    logger.info(f"[v35.x] children 中无精确匹配，返回 METACLASS parent: id={parent_id}, code={parent_code}, name={parent_name}")
                    return CodeNameResult(
                        code=parent_code,
                        name=parent_name,
                        id=parent_id,
                        uri=None  # URI 未知，需要通过 getEntityListByBOId 获取
                    )

    # 收集所有候选业务对象及其完整信息（code、name、id、uri）
    all_items: List[Dict[str, Optional[str]]] = []
    seen_codes: Set[str] = set()
    for node in target_nodes:
        name = _text(node, "name")
        code = _text(node, "code")
        uri_ = _text(node, "uri")  # 【v9.0 新增】提取 URI
        if name and code and code != "null" and code not in seen_codes:
            all_items.append({"code": code, "name": name, "id": _text(node, "id"), "uri": uri_})
            seen_codes.add(code)
    for node, parent_code, parent_name in nodes_with_parent:
        name = _text(node, "name")
        uri_ = _text(node, "uri")  # 【v9.0 新增】提取 URI
        if name and parent_code and parent_code != "null" and parent_code not in seen_codes:
            all_items.append({"code": parent_code, "name": name, "id": _text(node, "id"), "uri": uri_})
            seen_codes.add(parent_code)

    # 【v35.x 调试】打印 all_items 收集结果
    logger.info(f"[v35.x DEBUG] all_items 收集结果: {len(all_items)} 个, search_name={search_name}")
    for i, item in enumerate(all_items):
        logger.info(f"  [{i}] uri={item.get('uri')}, name={item.get('name')}")

    # 【v35.x 修复】过滤 IBpmStep/审批/自定义项，优先选择主表
    # 当 search_name 不在 all_items 中时，需要过滤变体
    if all_items:
        search_lower = search_name.lower()
        # 检查 search_name 是否在 all_items 中
        name_matches = [item for item in all_items if item.get("name", "").lower() == search_lower]

        if not name_matches:
            # search_name 不在候选中，需要过滤变体
            # 排除包含 IBpmStep、审批、自定义项 的项
            filtered_items = []
            for item in all_items:
                name = item.get("name", "")
                uri = item.get("uri", "")
                name_lower = name.lower()
                # 排除变体
                if any(keyword in name_lower for keyword in ["ibpmstep", "审批", "自定义项", "dcs"]):
                    continue
                # 优先选择主表（包含 "主表" 的）
                if "主表" in name:
                    filtered_items.insert(0, item)  # 插入到开头
                else:
                    filtered_items.append(item)

            if filtered_items:
                all_items = filtered_items
                logger.info(f"[v35.x] 过滤变体后选择主表: {all_items[0].get('uri')}, name={all_items[0].get('name')}")

    # 【v35.x 修复】过滤 IBpmStep 后缀，优先选择主表
    # 当有多个候选时，排除 URI 包含 "IBpmStep" 的变体
    if len(all_items) > 1:
        main_items = [item for item in all_items if item.get("uri") and not item.get("uri", "").endswith("IBpmStep")]
        if main_items:
            all_items = main_items
            logger.info(f"[v35.x] 过滤 IBpmStep 变体后剩余 {len(all_items)} 个主表实体")

    if len(all_items) == 1:
        # 【v12.x 修复】只有一个候选时，检查 name 是否匹配搜索词
        # 如果 name 不匹配（票据工作台→票据动作），检查 code 是否匹配搜索词
        item = all_items[0]
        item_name = item.get("name") or ""
        item_code = item.get("code") or ""
        # 精确匹配 或 搜索词是候选名的子串/父串 → 直接使用
        if item_name == search_name or search_name in item_name or item_name in search_name:
            return CodeNameResult(code=item["code"], name=item["name"], id=item.get("id"), uri=item.get("uri"))
        # 【v12.x 新增】name 不匹配但 code 匹配搜索词 → 直接使用（用户显式指定了 code）
        # 场景："票据工作台" 匹配到 name=票据动作, code=票据工作台
        if item_code == search_name:
            return CodeNameResult(code=item["code"], name=item["name"], id=item.get("id"), uri=item.get("uri"))
        # name 和 code 都不匹配 → 改为选择列表，让用户确认
        all_names = [item["name"] for item in all_items]
        return CodeNameResult(available_names=all_names, available_items=all_items)
    elif len(all_items) > 1:
        # 多个候选，返回完整信息供选择
        all_names = [item["name"] for item in all_items]
        return CodeNameResult(available_names=all_names, available_items=all_items)
    return None


def collect_entity_details(result: Any) -> List[Dict[str, Optional[str]]]:
    """从业务对象响应中收集实体详情列表"""
    if not isinstance(result, dict):
        return []
    code_val = _text(result, "code") or _text(result, "resultCode")
    if code_val != "200":
        return []
    data_node = result.get("data")
    if not isinstance(data_node, dict):
        return []
    entities_node = data_node.get("entities")
    if not isinstance(entities_node, list):
        inner = data_node.get("data")
        if isinstance(inner, dict):
            entities_node = inner.get("entities")
    if not isinstance(entities_node, list):
        return []

    out: List[Dict[str, Optional[str]]] = []

    def walk(arr: List[Any], parent_name: str = "") -> None:
        for entity in arr:
            if not isinstance(entity, dict):
                continue
            eid = _text(entity, "id")
            if eid == "null":
                eid = None
            # 提取 billName（可能在 name 或 displayName 字段）
            # 优先使用 displayName（中文名），其次是 name（英文名）
            bill_name = _text(entity, "name") or ""
            display_name = _text(entity, "displayName") or ""
            # 如果有中文名，优先使用
            if display_name:
                bill_name = display_name
            out.append(
                {
                    "entityId": eid,
                    "uri": _text(entity, "uri"),
                    "boId": _text(entity, "businessObjectId"),
                    "businessObjectCode": _text(entity, "businessObjectCode"),
                    "billName": bill_name,
                    "displayName": display_name,  # 保留原始中文名
                    "name": _text(entity, "name"),  # 保留原始英文名
                    "parentName": parent_name,
                }
            )
            ch = entity.get("children")
            if isinstance(ch, list):
                # 递归时传递当前实体名作为父节点名（使用中文名）
                parent_for_child = display_name if display_name else bill_name
                walk(ch, parent_for_child)

    walk(entities_node)

    # 【v35.x 调试】打印 entities 结构
    if out:
        logger.info(f"[v35.x DEBUG] collect_entity_details 返回 {len(out)} 个实体")
        for i, e in enumerate(out[:5]):
            # 打印所有字段
            logger.info(f"  [{i}] {json.dumps(e, ensure_ascii=False)}")

    return out


def _semantic_match_billname(billname: str, uri: str) -> bool:
    """判断 URI 是否与 billname 语义匹配

    例如：
      billname="授信信息", uri="drft.drftnoteinformation.DrftNoteInformationAgreement"
      -> 匹配（URI 包含 Agreement）

      billname="授信释放信息", uri="lcm.payregister.ReleaseCredit_b"
      -> 匹配（URI 包含 Release 和 Credit）

      billname="授信信息", uri="cam.agreement.Agreement"
      -> 不匹配
    """
    if not billname or not uri:
        return False

    # 移除 .VO 后缀
    uri_clean = uri.replace(".VO", "")

    # 提取 URI 的最后一段（通常是实体名）
    parts = uri_clean.split(".")
    entity_name = parts[-1] if parts else ""
    entity_lower = entity_name.lower()
    uri_lower = uri_clean.lower()

    # 【v17.x】中英文关键词映射表
    # key: 中文关键词, value: URI 中可能的英文关键词列表
    keyword_map = {
        "授信": ["credit", "agreement"],
        "信息": ["information", "info"],
        "执行": ["execution", "exec"],
        "兑付": ["expire", "cash"],
        "办理": ["register"],
        "付款": ["pay", "payment"],
        "释放": ["release"],
    }

    # 收集 billname 中包含的中文关键词
    matched_keywords = set()
    for cn_kw, en_kws in keyword_map.items():
        if cn_kw in billname:
            matched_keywords.add(cn_kw)

    if not matched_keywords:
        return False

    # 检查 URI 中是否包含至少一个匹配关键词对应的英文
    matched_en = set()
    for cn_kw in matched_keywords:
        en_kws = keyword_map.get(cn_kw, [])
        for en_kw in en_kws:
            if en_kw in uri_lower:
                matched_en.add(cn_kw)
                break

    # 如果有足够的匹配（billname 中有一个关键词在 URI 中找到对应英文，就认为是匹配的）
    # 但如果是"授信"+"释放"组合，两个都需要匹配
    if "授信" in matched_keywords and "释放" in matched_keywords:
        # 需要同时匹配授信和释放
        has_credit = any(kw in uri_lower for kw in keyword_map["授信"])
        has_release = "release" in uri_lower
        return has_credit and has_release
    elif "授信" in matched_keywords and "信息" in matched_keywords:
        # 需要同时匹配授信和信息
        has_credit = any(kw in uri_lower for kw in keyword_map["授信"])
        has_info = any(kw in uri_lower for kw in keyword_map["信息"])
        return has_credit and has_info
    else:
        # 至少匹配一个关键词
        return len(matched_en) > 0


def parse_entity_model_for_ai(result: Any) -> Optional[Dict[str, Any]]:
    """解析实体模型供 AI 使用"""
    if not isinstance(result, dict):
        return None
    code_val = _text(result, "code") or _text(result, "resultCode")
    if code_val != "200":
        return None
    data_node = result.get("data")
    if not isinstance(data_node, dict):
        return None
    inner = data_node.get("data")
    if isinstance(inner, dict):
        data_node = inner
    domain = _text(data_node, "domain")
    schema = _text(data_node, "schema")
    if (not schema or not str(schema).strip()) and domain and domain.startswith("c-"):
        schema = domain.replace("-", "_") + "_db"
    model: Dict[str, Any] = {
        "uri": _text(data_node, "uri"),
        "tableName": _text(data_node, "tableName"),
        "businessObjectCode": _text(data_node, "businessObjectCode"),
        "domain": domain,
        "schema": schema,
        "businessProperties": [],
    }
    bp_array = data_node.get("businessProperties")
    if isinstance(bp_array, list):
        for bp in bp_array:
            if not isinstance(bp, dict):
                continue
            all_tables = bp.get("allTables")
            tbl = None
            if isinstance(all_tables, list) and all_tables:
                t0 = all_tables[0]
                if isinstance(t0, str):
                    tbl = t0
            summary = {
                "name": _text(bp, "name"),
                "displayName": _text(bp, "displayName"),
                "uri": _text(bp, "uri"),
                "tableName": tbl,
            }
            model["businessProperties"].append(summary)
            cfs = bp.get("characterFields")
            if isinstance(cfs, list):
                for cf in cfs:
                    if not isinstance(cf, dict):
                        continue
                    c_all = cf.get("allTables")
                    ctbl = None
                    if isinstance(c_all, list) and c_all and isinstance(c_all[0], str):
                        ctbl = c_all[0]
                    # 【v10.4 优化】从 allColumns 中提取特征字段的列信息
                    all_cols = cf.get("allColumns", [])
                    col_info = {}
                    if isinstance(all_cols, list) and all_cols:
                        for col in all_cols:
                            if isinstance(col, dict):
                                col_name = _text(col, "columnName")
                                if col_name:
                                    col_info[col_name] = {
                                        "displayName": _text(col, "displayName"),
                                        "type": _text(col, "type"),
                                        "typeUri": _text(col, "typeUri"),
                                    }
                    model["businessProperties"].append(
                        {
                            "name": _text(cf, "name"),
                            "displayName": _text(cf, "displayName"),
                            "uri": _text(cf, "uri"),
                            "tableName": ctbl,
                            "isCharacterField": True,  # 【v10.4】标记为特征字段
                            "columns": col_info,  # 【v10.4】特征字段的列信息
                        }
                    )
    return model


def parse_foreign_keys_util(
    entity_db: Any, _entity_uri: Optional[str]
) -> List[Dict[str, Any]]:
    """解析外键关联"""
    if not isinstance(entity_db, dict):
        return []
    code_val = _text(entity_db, "code") or _text(entity_db, "resultCode")
    if code_val != "200":
        return []
    data_node = entity_db.get("data")
    if not isinstance(data_node, dict):
        return []
    inner = data_node.get("data")
    if isinstance(inner, dict):
        data_node = inner
    assoc = data_node.get("associationAttributes")
    if not isinstance(assoc, list):
        return []
    fks: List[Dict[str, Any]] = []
    for attr in assoc:
        if not isinstance(attr, dict):
            continue
        biztype = str(attr.get("biztype", "")).replace('"', "")
        if biztype != "quote":
            continue
        col = attr.get("columnName")
        type_uri = attr.get("typeUri")
        col_s = str(col).replace('"', "") if col is not None else None
        uri_s = str(type_uri).replace('"', "") if type_uri is not None else None
        if col_s and uri_s:
            fks.append({"columnName": col_s, "refUri": uri_s})
    return fks


def load_scheme_map(path: Path) -> List[Dict[str, Any]]:
    """加载领域 schema 映射表"""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def scheme_by_domain(
    scheme_list: List[Dict[str, Any]], domain: Optional[str]
) -> Optional[str]:
    """根据领域获取对应的 schema"""
    if not domain or not str(domain).strip():
        return None
    target_key = "mdd_schema." + domain.strip()
    for scheme in scheme_list:
        if not isinstance(scheme, dict):
            continue
        key = scheme.get("key")
        if key == target_key:
            ex = scheme.get("exclusiveValue")
            if ex and str(ex).strip():
                return str(ex).strip()
            val = scheme.get("value")
            return str(val).strip() if val else None
    return None


def attr_to_map(attr: AttributeInfo) -> Dict[str, Any]:
    """将 AttributeInfo 转换为字典"""
    m: Dict[str, Any] = {
        "displayName": attr.display_name,
        "dbColumnName": attr.db_column_name,
        "type": attr.type,
        "name": attr.name,
        "tableName": attr.table_name,
    }
    if attr.uri:
        m["uri"] = attr.uri
    if attr.enums:
        m["enums"] = [{"code": e.code, "name": e.name} for e in attr.enums]
    return m


# ============================================================
# 语义匹配：Excel 列头 ↔ 元数据字段
# ============================================================

# 常用同义词映射（中文 → 中文/英文别名）
_SYNONYM_MAP: Dict[str, Set[str]] = {
    # 客户相关
    "客户": {"customer", "客户名称", "customerName", "客户名称", "customer_name"},
    "客户名称": {"客户", "customer", "customerName", "customer_name"},
    "customer": {"客户", "客户名称", "customerName", "customer_name"},
    "customerName": {"客户", "客户名称", "customer", "customer_name"},
    # 供应商相关
    "供应商": {"supplier", "供应商名称", "supplierName", "supplier_name", "vendor"},
    "供应商名称": {"供应商", "supplier", "supplierName", "supplier_name", "vendor"},
    "supplier": {"供应商", "供应商名称", "supplierName", "supplier_name", "vendor"},
    "vendor": {"供应商", "供应商名称", "supplier", "supplierName"},
    # 物料/产品相关
    "物料": {"material", "product", "物料名称", "产品", "item", "materialName", "productName", "itemName"},
    "物料名称": {"物料", "material", "product", "产品", "item", "materialName", "productName"},
    "产品": {"物料", "material", "product", "物料名称", "item", "materialName", "productName"},
    "product": {"物料", "material", "product", "物料名称", "产品", "item", "materialName", "productName"},
    "item": {"物料", "material", "product", "产品", "物料名称", "itemName"},
    # 单据相关
    "单据编号": {"单据号", "单号", "单据号", "单据编码", "单据code", "单据Code", "单号", "单据号码"},
    "单号": {"单据编号", "单据号", "单据编码", "单据code", "单据Code", "单据号码"},
    "单据日期": {"单据时间", "单据创建日期", "制单日期", "制单时间", "单据生成日期", "业务日期", "业务时间"},
    "制单日期": {"单据日期", "单据时间", "制单时间", "单据生成日期", "业务日期", "业务时间"},
    # 金额相关
    "金额": {"amount", "sum", "总价", "总金额", "金额", "金额合计", "本币金额", "原币金额", "金额不含税"},
    "数量": {"qty", "quantity", "num", "count", "count", "件数", "数量", "num"},
    "单价": {"price", "unitPrice", "unit_price", "单价", "含税单价", "不含税单价", "报价"},
    "price": {"单价", "unitPrice", "unit_price", "含税单价", "不含税单价", "报价"},
    # 组织相关
    "部门": {"department", "dept", "组织", "部门名称", "departmentName", "deptName"},
    "组织": {"org", "organization", "组织名称", "department", "部门", "orgName", "organizationName"},
    "部门名称": {"部门", "department", "dept", "组织", "departmentName", "deptName"},
    # 仓库相关
    "仓库": {"warehouse", "stock", "仓库名称", "warehouseName", "warehouse_name", "stockName"},
    "warehouse": {"仓库", "stock", "仓库名称", "warehouseName", "warehouse_name"},
    # 人员相关
    "人员": {"person", "员工", "职员", "employee", "user", "操作员", "制单人"},
    "制单人": {"人员", "person", "员工", "职员", "employee", "user", "操作员", "制单人", "maker"},
    # 业务相关
    "业务日期": {"单据日期", "单据时间", "单据创建日期", "制单日期", "制单时间", "单据生成日期", "业务时间"},
    "审核日期": {"审批日期", "审批时间", "审核时间", "批准日期", "审批完成日期"},
    "生效日期": {"生效时间", "启用日期", "生效", "effectiveDate"},
    "失效日期": {"失效时间", "停用日期", "失效", "expireDate", "endDate"},
}


def _normalize_text(text: str) -> str:
    """规范化文本：去除空格、特殊字符，转小写"""
    if not text:
        return ""
    # 去除空白字符、转小写
    normalized = "".join(text.split()).lower()
    return normalized


def _is_semantic_match(excel_col: str, meta_field: str) -> bool:
    """
    判断 Excel 列名与元数据字段是否语义匹配

    匹配规则（按优先级）：
    1. 规范化后精确相等 【修复】移除子串匹配，避免"物料"匹配到"物料id"、"物料权限"
    2. 同义词匹配
    """
    if not excel_col or not meta_field:
        return False

    # 规则1：规范化后精确相等
    excel_norm = _normalize_text(excel_col)
    meta_norm = _normalize_text(meta_field)
    if excel_norm == meta_norm:
        return True

    # 【修复】移除"互为子串"规则，只保留同义词匹配
    # 原因：子串匹配会导致"物料"错误匹配到"物料id"、"物料权限"等

    # 规则2：同义词匹配
    excel_lower = excel_col.lower()
    meta_lower = meta_field.lower()

    # 检查 excel_col 是否在同义词表中
    if excel_lower in _SYNONYM_MAP:
        synonyms = _SYNONYM_MAP[excel_lower]
        if meta_lower in synonyms or meta_norm in {_normalize_text(s) for s in synonyms}:
            return True

    # 检查 meta_field 是否在同义词表中
    if meta_lower in _SYNONYM_MAP:
        synonyms = _SYNONYM_MAP[meta_lower]
        if excel_lower in synonyms or excel_norm in {_normalize_text(s) for s in synonyms}:
            return True

    return False


# 【v12.x 新增】匹配阈值配置
_MATCH_SCORE_EXACT = 1.0       # 精准匹配（完全相等）
_MATCH_SCORE_SUBSTRING = 0.8   # 子串匹配
_MATCH_SCORE_SYNONYM = 0.6     # 同义词匹配
_MATCH_THRESHOLD_DEFAULT = 0.5  # 默认阈值


def calc_match_score(excel_col: str, meta_field: str) -> float:
    """
    计算 Excel 列名与元数据字段的相似度分数 (0.0 ~ 1.0)

    评分规则：
    1. 精准匹配（规范化后完全相等）= 1.0
    2. 【修复】移除子串匹配，避免"物料"匹配到"物料id"、"物料权限"
    3. 同义词匹配 = 0.6
    4. 编辑距离 ≤ 2 = 0.5
    5. 其他 = 0.0

    入参：
        excel_col: Excel 列头名称
        meta_field: 元数据字段 displayName
    返回：
        0.0 ~ 1.0 的相似度分数
    """
    if not excel_col or not meta_field:
        return 0.0

    excel_col = excel_col.strip()
    meta_field = meta_field.strip()
    if not excel_col or not meta_field:
        return 0.0

    excel_norm = _normalize_text(excel_col)
    meta_norm = _normalize_text(meta_field)

    # 规则1：精准匹配 = 1.0
    if excel_norm == meta_norm:
        return _MATCH_SCORE_EXACT

    # 【修复】移除子串匹配规则，避免"物料"错误匹配到"物料id"、"物料权限"
    # 原因：子串匹配会导致元数据字段名（如"物料权限"）被添加到 matched_attrs，
    # 而这些字段不在 Excel 表头中

    # 规则2：同义词匹配 = 0.6
    excel_lower = excel_col.lower()
    meta_lower = meta_field.lower()
    if excel_lower in _SYNONYM_MAP:
        synonyms = _SYNONYM_MAP[excel_lower]
        if meta_lower in synonyms or meta_norm in {_normalize_text(s) for s in synonyms}:
            return _MATCH_SCORE_SYNONYM
    if meta_lower in _SYNONYM_MAP:
        synonyms = _SYNONYM_MAP[meta_lower]
        if excel_lower in synonyms or excel_norm in {_normalize_text(s) for s in synonyms}:
            return _MATCH_SCORE_SYNONYM

    return 0.0

    # 规则4：编辑距离 ≤ 2 = 0.5
    # 【v12.x 新增】编辑距离容错
    try:
        if _levenshtein_distance(excel_norm, meta_norm) <= 2:
            return 0.5
    except Exception:
        pass

    return 0.0


def _levenshtein_distance(s1: str, s2: str) -> int:
    """计算两个字符串的编辑距离（Levenshtein Distance）"""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def _semantic_match_doc_fields(
    excel_cols: List[str],
    group: BizTableGroup,
    threshold: float = _MATCH_THRESHOLD_DEFAULT
) -> Tuple[List[str], List[str]]:
    """
    将 Excel 列头与元数据字段进行语义匹配（两阶段匹配）

    阶段1：精准匹配（score = 1.0），遍历所有字段，完全相等即匹配
    阶段2：阈值兜底，对未精准匹配的列，使用相似度阈值过滤

    返回: (matched_doc_fields, matched_attr_names)
      - matched_doc_fields: 与元数据字段语义匹配的 Excel 列名
      - matched_attr_names: 匹配上的元数据字段的 display_name 列表
    """
    if not excel_cols or not group.attributes:
        return [], []

    # 构建字段列表：(attr, display_name, attr_name)
    field_list: List[Tuple[Any, str, str]] = []
    for attr in group.attributes:
        display = (attr.display_name or "").strip()
        name = (attr.name or "").strip()
        if display:
            field_list.append((attr, display, name))

    # 阶段1：精准匹配
    matched_doc_fields: List[str] = []
    matched_attr_names: List[str] = []
    unmatched_cols: List[str] = []

    for col in excel_cols:
        col = col.strip()
        if not col:
            continue

        col_norm = _normalize_text(col)
        is_exact_matched = False

        # 遍历元数据字段，找精准匹配
        for attr, meta_display, meta_name in field_list:
            meta_norm = _normalize_text(meta_display)
            if col_norm == meta_norm:
                # 精准匹配
                if col not in matched_doc_fields:
                    matched_doc_fields.append(col)
                if meta_display not in matched_attr_names:
                    matched_attr_names.append(meta_display)
                is_exact_matched = True
                break  # 精准匹配找到一个就停止

        if not is_exact_matched:
            unmatched_cols.append(col)

    # 阶段2：阈值兜底（对未精准匹配的列）
    for col in unmatched_cols:
        col_norm = _normalize_text(col)
        best_score = 0.0

        for attr, meta_display, meta_name in field_list:
            # 尝试匹配 display_name
            score = 0.0
            if meta_display:
                score = calc_match_score(col, meta_display)

            # 尝试匹配 name（取较高分）
            if meta_name:
                name_score = calc_match_score(col, meta_name)
                score = max(score, name_score)

            if score >= threshold and score > best_score:
                best_score = score
                if col not in matched_doc_fields:
                    matched_doc_fields.append(col)
                # 优先使用 display_name
                matched_name = meta_display if meta_display else meta_name
                if matched_name not in matched_attr_names:
                    matched_attr_names.append(matched_name)

    return matched_doc_fields, matched_attr_names


# ============================================================
# V2 新增：Excel 业务对象解析与智能匹配
# ============================================================

def parse_business_objects_from_excel(excel_result: Dict[str, Any]) -> Tuple[List[str], Dict[str, List[str]]]:
    """
    从 Excel 解析业务对象

    规则：
    - 优先使用 billObjects（含关系描述的列表，更干净）
    - 支持 "主表.子表" 格式，自动拆分为独立的业务对象
    - 拆分后去重返回

    返回: (bill_names, bill_mappings)
      - bill_names: 独立业务对象名称列表
      - bill_mappings: 每行报表对应的业务对象映射
    """
    data_source = excel_result.get("dataSource") or {}
    # billObjects 在 parse_excel 返回值的顶层，不在 dataSource 内
    bill_objects = excel_result.get("billObjects", []) or data_source.get("billObjects", [])
    bill_names = data_source.get("billNames", [])
    raw_mappings = data_source.get("rawMappings", [])

    # 拆分后的独立业务对象列表
    all_bill_names: List[str] = []
    bill_mappings: Dict[str, List[str]] = {}  # report_no → bill_names

    # 优先使用 billObjects（更干净）
    if bill_objects:
        for bo in bill_objects:
            bn = bo.get("billName", "").strip()
            if bn and bn not in all_bill_names:
                all_bill_names.append(bn)
        logger.info(f"从 billObjects 提取到 {len(all_bill_names)} 个业务对象: {all_bill_names}")
    else:
        # 降级：解析 billNames（可能含脏数据）
        for bill_name in bill_names:
            if "." in bill_name:
                # 格式：材料出库单主表.材料出库单子表 → 拆分为两个独立业务对象
                parts = bill_name.split(".", 1)
                for part in parts:
                    part = part.strip()
                    if part and part not in all_bill_names:
                        all_bill_names.append(part)
            else:
                # 单个业务对象名称
                if bill_name.strip() and bill_name.strip() not in all_bill_names:
                    all_bill_names.append(bill_name.strip())

    # 【v16.x 修复】从 raw_mappings 提取报表与业务对象的映射
    # 注意：raw_mappings[i].billNames 来自 Excel "核心业务对象" 列，可能包含公式/描述等垃圾
    # 仅使用 billObjects 构建 all_bill_names（已做干净）；raw_mappings 用于映射关系（如果有）
    for mapping in raw_mappings:
        report_no = mapping.get("reportNo", "")
        # 【v16.x 修复】rawMappings 中的 billNames 通常是垃圾，改用 billObjects 中的 billName
        # rawMappings 的价值在于 reportNo → billNames 的映射关系，而非 billNames 本身的内容
        # 由于 rawMappings[i].billNames 不可信，跳过从 billNames 构建映射
        # 如需报表级映射，应从 billObjects 按 reportNo 重新组织
        if report_no:
            # 仅当有有效业务对象时才记录映射关系（暂时跳过，避免垃圾数据）
            bill_mappings[report_no] = []

    logger.info(f"解析到 {len(all_bill_names)} 个业务对象: {all_bill_names}")
    logger.info(f"报表映射: {len(bill_mappings)} 条")

    return all_bill_names, bill_mappings


def filter_excel_headers(excel_result: Dict[str, Any]) -> List[str]:
    """
    从 Excel 结果中提取列头字段（排除描述性列头）

    优先级：
    1. allCells[1]（第2行，真正的列头）
    2. columns（可能只有分组标题）

    排除词列表：
    - 序号、编号、报表编号、报表名称
    - 难度等级、场景说明、技能要求
    - 备注、内容、分类、类型
    - 单据名称、业务对象名称
    """
    exclude_cols = {
        "序号", "编号", "报表编号", "报表", "报表名称", "名称",
        "难度等级", "难度名称", "难度", "等级",
        "场景说明", "说明", "描述",
        "技能要求", "要求", "能力",
        "报表数量", "数量", "统计",
        "备注", "内容", "分类", "类型",
        "核心业务对象", "业务对象", "数据源",
        "单据名称", "业务对象名称", "报表对象",
        "单据", "实体", "来源单据",
        "数据源字段", "数据源(业务对象)", "来源业务对象",
        "来源", "业务对象来源",
        "查询条件", "关系描述", "使用到的业务对象",
    }

    # 尝试从 allCells[1] 获取真正的列头
    all_cells = excel_result.get("allCells", [])
    headers = []

    if len(all_cells) >= 2:
        # 使用第2行（真正的列头）
        row1 = all_cells[1]
        headers = [c.strip() for c in row1 if c.strip()]
    else:
        # 降级使用 columns
        headers = [c.strip() for c in excel_result.get("columns", []) if c.strip()]

    filtered = []
    for h in headers:
        if h and h not in exclude_cols:
            # 排除纯数字和短符号
            if len(h) >= 2 and not h.isdigit():
                filtered.append(h)

    return filtered


def _extract_field_keywords(field: str) -> Set[str]:
    """提取字段关键词"""
    if not field:
        return set()
    keywords = set(re.findall(r'[一-鿿]+', field))
    # 移除常见词
    stop_words = {"的", "和", "与", "或", "以及", "包括"}
    return keywords - stop_words


def _expand_reference_by_steps(
    cfg: dict,
    initial_ref_uri: str,
    steps: List[Dict[str, Any]]
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    根据 steps 数组展开多级参照。

    适用场景：
      材料出库单.物料.物料类别.名称

    steps 结构：
      [
        {"step": 1, "fieldName": "物料", "uri": "bd.material.MaterialVO"},
        {"step": 2, "fieldName": "物料类别", "uri": "bd.materialCategory.MaterialCategoryVO"},
        {"step": 3, "fieldName": "名称", "uri": ""}
      ]

    返回:
      (最终字段信息, 展开路径列表)
      - 最终字段: {"displayName": "名称", "dbColumnName": "name", ...}
      - 展开路径: [{"uri": "bd.material.MaterialVO", "fieldName": "物料", ...}, ...]
    """
    if not steps:
        return None, []

    def fetch_uri(u: str) -> str:
        return _query_by_uri_cached(cfg, u)

    expanded_path: List[Dict[str, Any]] = []
    current_ref_uri = initial_ref_uri

    for i, step in enumerate(steps):
        field_name = step.get("fieldName", "")
        if not field_name:
            continue

        # 获取当前参照实体数据
        ref_data = _query_by_uri_cached_parsed(cfg, current_ref_uri)
        if not ref_data:
            logger.warning(f"[STEPS EXPAND] 无法获取参照实体: {current_ref_uri}")
            break

        groups = parse(ref_data, fetch_uri_json=fetch_uri)
        if not groups:
            break

        ref_group = groups[0]
        target_norm = _normalize_text(field_name)

        # 在当前参照实体中查找目标字段
        found_attr = None
        for attr in ref_group.attributes:
            dn = attr.display_name or ""
            attr_name = attr.name or ""

            # 精确匹配
            if _normalize_text(dn) == target_norm or _normalize_text(attr_name) == target_norm:
                found_attr = attr
                break

            # 语义匹配
            if _is_semantic_match(field_name, dn):
                found_attr = attr
                break

        if not found_attr:
            logger.warning(f"[STEPS EXPAND] 在 {current_ref_uri} 中未找到字段: {field_name}")
            break

        # 记录展开路径
        expanded_path.append({
            "step": step.get("step", i + 1),
            "fieldName": field_name,
            "displayName": found_attr.display_name,
            "dbColumnName": found_attr.db_column_name,
            "refUri": found_attr.uri,
            "entityUri": current_ref_uri,
            "entityBillName": ref_group.bill_name,
            "entityTableName": ref_group.table_name,
        })

        # 判断是否需要继续递归
        if i == len(steps) - 1:
            # 最后一个 step，返回最终字段
            return {
                "displayName": found_attr.display_name,
                "dbColumnName": found_attr.db_column_name,
                "type": found_attr.type,
                "name": found_attr.name,
                "uri": found_attr.uri,
            }, expanded_path
        else:
            # 还有下一级，需要继续递归
            if found_attr.uri:
                current_ref_uri = found_attr.uri
            else:
                # 当前字段不是参照，无法继续递归
                logger.warning(f"[STEPS EXPAND] 字段 {field_name} 不是参照类型，无法继续展开")
                break

    return None, expanded_path


def _expand_reference_field(
    cfg: dict,
    ref_uri: str,
    target_field: str,
    max_depth: int = 3,
    current_depth: int = 0
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    递归展开参照属性 URI（模糊匹配版本，通过查找 name/code 类字段猜测路径）

    返回: (匹配的属性信息, 参照实体数据)
    """
    if current_depth >= max_depth:
        logger.warning(f"达到最大递归深度 {max_depth}，停止展开 {ref_uri}")
        return None, None

    # 获取参照实体数据
    ref_data = _query_by_uri_cached_parsed(cfg, ref_uri)
    if not ref_data:
        logger.warning(f"无法获取参照实体: {ref_uri}")
        return None, None

    # 解析实体
    def fetch_uri(u: str) -> str:
        return _query_by_uri_cached(cfg, u)

    groups = parse(ref_data, fetch_uri_json=fetch_uri)
    if not groups:
        return None, None

    ref_group = groups[0]

    # 在参照实体的属性中查找目标字段
    target_norm = _normalize_text(target_field)

    for attr in ref_group.attributes:
        dn = attr.display_name or ""
        name = attr.name or ""

        # 精确匹配
        if _normalize_text(dn) == target_norm or _normalize_text(name) == target_norm:
            return {
                "displayName": dn,
                "dbColumnName": attr.db_column_name,
                "type": attr.type,
                "name": name,
                "uri": attr.uri,
            }, {"uri": ref_uri, "billName": ref_group.bill_name, "tableName": ref_group.table_name}

        # 语义匹配
        if _is_semantic_match(target_field, dn):
            return {
                "displayName": dn,
                "dbColumnName": attr.db_column_name,
                "type": attr.type,
                "name": name,
                "uri": attr.uri,
            }, {"uri": ref_uri, "billName": ref_group.bill_name, "tableName": ref_group.table_name}

    # 如果没找到，检查是否是参照属性（需要继续展开）
    for attr in ref_group.attributes:
        if attr.uri and attr.uri != ref_uri:
            # 检查 name/code 等常见字段
            dn = attr.display_name or ""
            if "name" in dn.lower() or "code" in dn.lower() or "编码" in dn or "名称" in dn:
                # 递归展开
                sub_attr, sub_entity = _expand_reference_field(
                    cfg, attr.uri, target_field, max_depth, current_depth + 1
                )
                if sub_attr:
                    return sub_attr, {"uri": ref_uri, "billName": ref_group.bill_name, "tableName": ref_group.table_name}

    return None, None


def expand_all_reference_fields(
    cfg: dict,
    field_matches: List[Tuple[str, str, Optional[str]]],  # (excel_field, source_bill, ref_uri)
    max_depth: int = 3,
    steps_map: Dict[str, List[Dict[str, Any]]] = None  # {excel_field: steps_array}
) -> List[Dict[str, Any]]:
    """
    展开所有需要递归查询的参照字段

    Args:
        field_matches: [(excel_field, source_bill, ref_uri), ...]
        cfg: 配置字典
        max_depth: 最大递归深度
        steps_map: {excel_field: steps_array} - 多级参照的 steps 路径

    Returns:
        展开后的字段匹配列表
    """
    expanded: List[Dict[str, Any]] = []

    for excel_field, source_bill, ref_uri in field_matches:
        if ref_uri:
            # 检查是否有 steps_map（多级参照）
            steps = steps_map.get(excel_field) if steps_map else None
            if steps and len(steps) > 1:
                # 多级参照展开
                logger.info(f"[STEPS EXPAND] 多级参照展开: {excel_field}, steps={len(steps)} 级")
                logger.debug(f"  steps: {steps}")

                final_attr, expanded_path = _expand_reference_by_steps(cfg, ref_uri, steps)

                if final_attr:
                    logger.info(f"  找到: {final_attr.get('displayName')} ({final_attr.get('dbColumnName')})")
                    expanded.append({
                        "excelField": excel_field,
                        "sourceBill": source_bill,
                        "displayName": final_attr.get("displayName"),
                        "dbColumnName": final_attr.get("dbColumnName"),
                        "type": final_attr.get("type"),
                        "refUri": ref_uri,
                        "expanded": True,
                        "expandedPath": expanded_path,  # 多级展开路径
                        "isMultiLevel": True,
                    })
                else:
                    logger.warning(f"  多级展开失败: {excel_field}")
                    expanded.append({
                        "excelField": excel_field,
                        "sourceBill": source_bill,
                        "refUri": ref_uri,
                        "expanded": False,
                    })
            else:
                # 单级参照展开（原有逻辑）
                logger.info(f"展开参照: {ref_uri} → 查找 '{excel_field}'")

                final_attr, ref_entity_info = _expand_reference_field(
                    cfg, ref_uri, excel_field, max_depth
                )

                if final_attr:
                    logger.info(f"  找到: {final_attr.get('displayName')} ({final_attr.get('dbColumnName')})")
                    expanded.append({
                        "excelField": excel_field,
                        "sourceBill": source_bill,
                        "displayName": final_attr.get("displayName"),
                        "dbColumnName": final_attr.get("dbColumnName"),
                        "type": final_attr.get("type"),
                        "refUri": ref_uri,
                        "refEntity": ref_entity_info,
                        "expanded": True,
                        "isMultiLevel": False,
                    })
                else:
                    logger.warning(f"  未找到匹配字段: {excel_field}")
                    expanded.append({
                        "excelField": excel_field,
                        "sourceBill": source_bill,
                        "refUri": ref_uri,
                        "expanded": False,
                    })
        else:
            # 不需要展开，直接使用
            expanded.append({
                "excelField": excel_field,
                "sourceBill": source_bill,
                "expanded": False,
            })

    return expanded


# ============================================================
# 【v7.0 新增】从自然语言请求文本提取字段名
# ============================================================

def _extract_fields_from_request_text(request_text: str) -> List[str]:
    """
    【v7.0 新增】从自然语言请求文本中提取字段名模式。

    提取规则：
    1. 中文字段名：2-10个汉字（如"单据编号、客户名称"）
    2. 带"的"字字段：如"单据的编号" → 提取"编号"
    3. 英文/拼音字段名：2-20个字母数字组合（如 materialCode）
    4. 排除常见语气词和无关词

    这样做的好处：当用户没有传 --excel-file 时，
    AI Agent 可以把原始请求文本传进来，自动提取字段并过滤元数据。
    """
    if not request_text:
        return []
    import re
    # 排除词列表（常见语气词、疑问词、动词）
    stop_words = {
        "的", "和", "与", "或", "以及", "包括", "包含",
        "以及", "生成", "报表", "查询", "获取", "拉取",
        "帮我", "请", "需要", "只要", "只要", "只要",
        "字段", "信息", "数据", "列表", "单据", "业务",
        "哪些", "什么", "怎么", "如何", "是否", "能不能",
        "提供", "一下", "给我", "可以", "主要", "关键",
        "详细", "全部", "所有", "全量", "完整",
        "这个", "那个", "这些", "那些", "此类",
        "根据", "按照", "通过", "基于",
        "一个", "几个", "若干", "某些", "某个",
        "比如", "例如", "例如说", "比如说",
        "还有", "另外", "此外", "并且", "同时",
        "然后", "接着", "再", "又",
        "之后", "之前", "期间", "以内", "之外",
    }
    # 1. 提取纯中文字段名（2-10个连续汉字）
    chinese_pattern = re.findall(r'[\u4e00-\u9fff]{2,10}', request_text)
    fields = []
    for chunk in chinese_pattern:
        # 排除包含排除词的
        if any(sw in chunk for sw in stop_words):
            continue
        # 排除全是"的"的
        if chunk.strip('的') == '':
            continue
        fields.append(chunk)
    # 2. 提取英文/拼音字段名（驼峰或下划线）
    english_pattern = re.findall(r'[a-zA-Z][a-zA-Z0-9_]{1,30}', request_text)
    fields.extend(english_pattern)
    # 3. 去重，保持顺序
    seen = set()
    unique = []
    for f in fields:
        fl = f.lower()
        if fl not in seen:
            seen.add(fl)
            unique.append(f)
    logger.debug(f"从请求文本提取字段: {unique}")
    return unique


# ============================================================
# V2 新增：多实体智能匹配
# ============================================================

@dataclass
class V2FieldMatch:
    """字段匹配结果（V2版）"""
    excel_field: str           # Excel 中的原始字段名
    display_name: str          # 匹配的 display_name
    source: str                # 来源：direct / reference
    source_bill: str = ""      # 来源业务对象名称
    ref_attr: Optional[AttributeInfo] = None  # 对应的属性信息
    ref_entity: Optional[str] = None  # 关联实体 URI（如果有）
    final_attr: Optional[Dict[str, Any]] = None  # 最终匹配的属性（递归展开后）
    is_ambiguous: bool = False  # 是否存在歧义（多业务对象都有）


def _infer_needed_reference_fields_multi(
    excel_fields: List[str],
    entity_groups: List[BizTableGroup]
) -> List[V2FieldMatch]:
    """
    AI 推理：匹配 Excel 字段与多个实体的属性

    逻辑：
    1. 遍历 Excel 字段，在所有业务对象中找匹配的属性
    2. 如果多个业务对象都有相似字段，生成歧义标记
    3. 如果字段匹配到参照属性，记录下来待展开
    """
    matches: List[V2FieldMatch] = []

    for excel_field in excel_fields:
        excel_field_stripped = excel_field.strip()
        if not excel_field_stripped:
            continue

        matched_in_bills: List[Tuple[str, AttributeInfo]] = []  # [(bill_name, attr)]

        # 在所有业务对象中搜索匹配
        for group in entity_groups:
            bill_name = group.bill_name or ""

            # 精确匹配
            for attr in group.attributes:
                dn = attr.display_name or ""
                if _normalize_text(dn) == _normalize_text(excel_field_stripped):
                    matched_in_bills.append((bill_name, attr))
                    break

            # 语义匹配
            if not any(bn == bill_name for bn, _ in matched_in_bills):
                for attr in group.attributes:
                    dn = attr.display_name or ""
                    if _is_semantic_match(excel_field_stripped, dn):
                        matched_in_bills.append((bill_name, attr))
                        break

        # 判断匹配结果
        if len(matched_in_bills) == 1:
            # 唯一匹配
            bill_name, attr = matched_in_bills[0]
            matches.append(V2FieldMatch(
                excel_field=excel_field_stripped,
                display_name=attr.display_name or excel_field_stripped,
                source="direct",
                source_bill=bill_name,
                ref_attr=attr
            ))
        elif len(matched_in_bills) > 1:
            # 多个匹配（歧义），生成多个 Match
            for bill_name, attr in matched_in_bills:
                display_name = f"{attr.display_name}({bill_name})" if len(matched_in_bills) > 1 else (attr.display_name or excel_field_stripped)
                matches.append(V2FieldMatch(
                    excel_field=excel_field_stripped,
                    display_name=display_name,
                    source="direct",
                    source_bill=bill_name,
                    ref_attr=attr,
                    is_ambiguous=True
                ))
        else:
            # 未匹配到，在参照属性中查找
            logger.info(f"  未直接匹配: {excel_field_stripped}，将在关联属性中查找")
            ref_match = _find_in_reference_attributes_multi(
                excel_field_stripped, entity_groups
            )
            if ref_match:
                matches.append(ref_match)
            else:
                logger.warning(f"  无法匹配字段: {excel_field_stripped}")

    return matches


def _find_in_reference_attributes_multi(
    excel_field: str,
    entity_groups: List[BizTableGroup]
) -> Optional[V2FieldMatch]:
    """
    在多个实体的参照属性中查找匹配的字段
    例如：物料名称 → 物料参照的 name 字段

    逻辑：
    1. 提取 Excel 字段的关键词（如"物料名称" → "物料"）
    2. 在所有业务对象的属性中查找包含该关键词的参照属性
    3. 返回匹配到的参照属性供后续展开
    """
    # 提取关键词
    suffixes = ["名称", "编码", "编号", "代码", "数量", "金额", "规格", "型号", "类别"]
    base_name = excel_field
    for suffix in suffixes:
        if excel_field.endswith(suffix):
            base_name = excel_field[:-len(suffix)]
            break

    excel_keywords = [base_name, excel_field] if base_name != excel_field else [excel_field]

    for group in entity_groups:
        bill_name = group.bill_name or ""
        for attr in group.attributes:
            if not attr.uri:
                continue

            attr_display = attr.display_name or ""
            attr_name = attr.name or ""

            for kw in excel_keywords:
                kw_norm = _normalize_text(kw)
                attr_display_norm = _normalize_text(attr_display)
                attr_name_norm = _normalize_text(attr_name)

                if kw_norm in attr_display_norm or kw_norm in attr_name_norm:
                    return V2FieldMatch(
                        excel_field=excel_field,
                        display_name=excel_field,
                        source="reference",
                        source_bill=bill_name,
                        ref_attr=attr,
                        ref_entity=attr.uri
                    )

    return None


def build_matched_entities(
    cfg: dict,
    excel_fields: List[str],
    entity_groups: List[BizTableGroup]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    V2 智能匹配：匹配字段并展开参照

    Args:
        cfg: 配置字典
        excel_fields: Excel 列头字段列表
        entity_groups: 业务对象实体组列表

    Returns:
        (matched_entities, reference_entities) - 匹配的实体列表和参照实体列表
    """
    if not entity_groups:
        return [], []

    # 1. AI 推理匹配（遍历所有业务对象）
    field_matches = _infer_needed_reference_fields_multi(excel_fields, entity_groups)

    # 【v45.x 新增】从 cfg 中获取 columnFieldMappings，构建 steps_map
    steps_map: Dict[str, List[Dict[str, Any]]] = {}
    column_field_mappings = cfg.get("_column_field_mappings", [])
    if column_field_mappings:
        # 构建 excel_field -> steps 数组的映射
        for mapping in column_field_mappings:
            source_desc = mapping.get("sourceDescription", "")
            steps = mapping.get("steps", [])
            if source_desc and steps and len(steps) > 1:
                # 多级参照：从 sourceDescription 提取最终字段名作为 key
                # 例如: "材料出库单.物料.物料类别.名称" -> 提取 "物料类别.名称" 或用 columnName
                column_name = mapping.get("columnName", "")
                # 用 columnName 或 sourceDescription 最后一部分作为 key
                key = column_name if column_name else source_desc.split(".")[-1] if "." in source_desc else source_desc
                steps_map[key] = steps
                logger.debug(f"[STEPS MAP] {key} -> {len(steps)} 级 steps")
        if steps_map:
            logger.info(f"【v45.x】构建 steps_map: {len(steps_map)} 个多级参照字段")

    # 2. 展开参照字段
    field_to_expand = [
        (m.excel_field, m.source_bill, m.ref_entity)
        for m in field_matches
        if m.ref_entity
    ]
    expanded = expand_all_reference_fields(cfg, field_to_expand, steps_map=steps_map)

    # 构建展开结果映射
    expanded_map: Dict[str, Dict[str, Any]] = {}
    for exp in expanded:
        key = exp.get("excelField", "")
        if exp.get("expanded") and exp.get("displayName"):
            exp["final_attr"] = {
                "displayName": exp.get("displayName"),
                "dbColumnName": exp.get("dbColumnName"),
                "type": exp.get("type"),
            }
        expanded_map[key] = exp

    # 3. 构建结果
    matched_entities: List[Dict[str, Any]] = []
    reference_entities: List[Dict[str, Any]] = []
    seen_ref_entities: Set[str] = set()  # 去重

    # 为每个业务对象构建匹配的实体
    for group in entity_groups:
        bill_name = group.bill_name or ""
        entity_attrs = []

        for match in field_matches:
            # 检查匹配是否属于当前实体
            if match.source_bill == bill_name:
                # 使用展开后的结果
                exp = expanded_map.get(match.excel_field, {})
                final_attr = exp.get("final_attr") or (match.ref_attr and {
                    "displayName": match.ref_attr.display_name,
                    "dbColumnName": match.ref_attr.db_column_name,
                    "type": match.ref_attr.type,
                })

                if final_attr:
                    attr_dict = {
                        "displayName": final_attr.get("displayName"),
                        "dbColumnName": final_attr.get("dbColumnName"),
                        "type": final_attr.get("type"),
                        "source": match.source,
                        "isAmbiguous": match.is_ambiguous
                    }
                    # 如果是参照属性，添加参照结构
                    if match.ref_entity:
                        attr_dict["refUri"] = match.ref_entity
                        attr_dict["refDisplayName"] = match.excel_field
                    entity_attrs.append(attr_dict)

        if entity_attrs:
            matched_entities.append({
                "billName": bill_name,
                "uri": group.uri or "",
                "domain": group.domain,
                "tableName": group.table_name,
                "attributes": entity_attrs,
                "referenceEntities": [],
            })

    # 处理参照实体（去重）
    for match in field_matches:
        if match.ref_entity and match.ref_entity not in seen_ref_entities:
            exp = expanded_map.get(match.excel_field, {})
            if exp.get("expanded") and exp.get("displayName"):
                seen_ref_entities.add(match.ref_entity)
                ref_entity_data = {
                    "billName": match.excel_field,
                    "uri": match.ref_entity,
                    "displayName": exp.get("displayName"),
                    "dbColumnName": exp.get("dbColumnName"),
                    "type": exp.get("type"),
                }
                reference_entities.append(ref_entity_data)

    return matched_entities, reference_entities


def biz_table_group_to_entity_map(
    group: BizTableGroup,
    entity_result: Dict[str, Optional[str]],
    entity_model: Optional[Dict[str, Any]],
    foreign_keys: List[Dict[str, Any]],
    doc_fields: List[str],
    is_sql_y: bool,
    table_template: Optional[str],
    fetch_uri_json: Callable[[str], str],
    cfg: dict,
    excel_cols: Optional[List[str]] = None,
    request_text: Optional[str] = None,
    default_schema: Optional[str] = None,  # 【v8.5】FastLookup 或其他来源提供的默认 schema
    match_threshold: Optional[float] = None,  # 【v12.x】语义匹配阈值
    effective_fields: Optional[List[str]] = None,  # 【v17.x】从 Excel 描述行提取的有效字段
) -> Optional[Dict[str, Any]]:
    """将 BizTableGroup 转换为实体映射字典"""
    max_ref = _max_reference_fields(cfg)

    # 【v10.0 修复】默认必需字段：报表 SQL 必需的基础字段，不依赖于 Excel 列头
    # 这些字段在生成 SQL 时通常需要，但用户可能没有在 Excel 中显式指定
    _DEFAULT_REQUIRED_FIELDS: Set[str] = {
        # 通用主表字段
        "id", "ID",
        # 审批/结算状态（筛选条件必需）
        "审批状态", "审批状态", "verifystate", "审批", "审核状态",
        "结算状态", "结算状态", "isettlestatus", "结算",
        "审批日期", "审批日期", "audit_date", "auditdate", "审核日期",
        "单据状态", "单据状态",
        # 金额相关
        "借款金额", "预付金额", "nloanmny",
        "核销金额", "已核销金额", "ncavmny", "核销(未清)",
        # 租户/组织字段
        "ytenant_id", "租户", "tenant_id",
    }

    # 【v8.8】schema 优先级：entity_model.schema > group.schema > default_schema
    schema_val = entity_model.get("schema") if entity_model else None
    if not schema_val:
        schema_val = getattr(group, 'schema', None) or default_schema

    # 【v7.0 修复 Bug】原代码用 `doc_fields = matched_attrs` 试图修改函数参数，
    # Python 函数参数是不可变引用，重新赋值只影响局部变量，不影响调用方。
    # 正确做法：使用局部变量 `effective_doc_fields` 存储语义匹配结果，
    # 该变量在函数内部用于过滤，不会与函数参数混淆。
    effective_doc_fields: List[str] = list(doc_fields)  # 复制，避免修改原列表

    # 【v7.0 重构】语义匹配优先级：
    # 1. doc_fields（用户显式指定）：直接使用，不做语义匹配
    # 2. excel_cols（Excel 列头）：语义匹配（用户传了 --excel-file 但没传 --doc-fields）
    # 3. request_text（自然语言）：提取字段后语义匹配
    # 当上述都有值时，优先级 1 > 2 > 3
    # 只有当所有来源都为空时（全量获取）
    _candidate_fields_for_match: List[str] = []
    if doc_fields:
        # 优先级1：用户显式指定，直接使用
        effective_doc_fields = list(doc_fields)
    elif excel_cols:
        # 优先级2：Excel 列头语义匹配
        _candidate_fields_for_match = excel_cols
    elif request_text:
        # 优先级3：request_text 提取字段后语义匹配
        _candidate_fields_for_match = _extract_fields_from_request_text(request_text)
    # 执行语义匹配（仅优先级2和3）
    if _candidate_fields_for_match:
        # 【v12.x】动态阈值：根据 Excel 列数量确定阈值
        # - 列多(>20)：宽松匹配，threshold = 0.3
        # - 列少(<=20)：严格匹配，threshold = 0.5
        if match_threshold is None:
            n_cols = len(excel_cols) if excel_cols else 0
            match_threshold = 0.3 if n_cols > 20 else 0.5
        matched_docs, matched_attrs = _semantic_match_doc_fields(_candidate_fields_for_match, group, threshold=match_threshold)
        if matched_attrs:
            if excel_cols and not doc_fields:
                logger.info(
                    f"语义匹配成功: Excel 列头 {len(matched_docs)} 个 → "
                    f"元数据字段 {len(matched_attrs)} 个: {matched_attrs}"
                )
            elif request_text and not doc_fields and not excel_cols:
                logger.info(
                    f"【v7.0】请求文本字段语义匹配: {_candidate_fields_for_match} → "
                    f"元数据字段 {len(matched_attrs)} 个: {matched_attrs}"
                )
            effective_doc_fields = matched_attrs

    # 【v10.0 修复】确保默认必需字段的中文名称被包含在 effective_doc_fields 中
    # 这样过滤逻辑才能正确保留这些字段
    _DEFAULT_REQUIRED_DISPLAY_NAMES: Set[str] = {
        "审批状态", "结算状态", "审批日期", "单据状态",
        "借款金额", "预付金额", "核销金额", "已核销金额",
        "租户", "ytenant_id",
    }
    if effective_doc_fields and isinstance(effective_doc_fields, list):
        for name in _DEFAULT_REQUIRED_DISPLAY_NAMES:
            if name not in effective_doc_fields:
                effective_doc_fields.append(name)

    # 【v36.x 新增】保留 relation_type 用于区分主表/平行表/扩展表
    m: Dict[str, Any] = {
        "tableName": group.table_name,
        "billName": group.bill_name,
        "domain": group.domain,
        "uri": entity_result.get("uri"),
        "businessObjectCode": entity_result.get("businessObjectCode"),
        "schema": schema_val,
        "relation_type": group.relation_type,  # 主表/平行表/扩展表标识
    }
    if foreign_keys:
        m["foreignKeys"] = foreign_keys
    prop_map: Dict[str, str] = {}
    # 【v10.4 优化】收集特征字段信息，用于补充到 attributes
    character_fields_info: List[Dict[str, Any]] = []
    if entity_model:
        for bp in entity_model.get("businessProperties") or []:
            if not isinstance(bp, dict):
                continue
            tn = bp.get("tableName")
            if not tn:
                continue
            n = bp.get("name")
            dn = bp.get("displayName")
            if n:
                prop_map[str(n)] = str(tn)
            if dn:
                prop_map[str(dn)] = str(tn)
            # 【v10.4】收集特征字段信息（包括主表的 characterFields）
            # 【v28.x 新增】也检查 isCharacterRef（自定义项使用此字段）
            # 【v10.4】收集特征字段信息（包括主表的 characterFields）
            # 【v28.x 新增】也检查 isCharacterRef（自定义项使用此字段）
            if bp.get("isCharacterField") or bp.get("isCharacterRef"):
                # 【v30.x 修复】对于自定义项（isCharacterRef），不要添加其 characterFields 到主表
                # 自定义项的 characterFields 属于另一个表（character_def 表），不是主表
                # 自定义项本身应该作为参照字段添加到主表（通过 fieldName）
                if bp.get("isCharacterRef"):
                    # 自定义项：只添加自身，不添加 characterFields
                    character_fields_info.append({
                        "name": bp.get("name"),
                        "displayName": bp.get("displayName"),
                        "uri": bp.get("dataType", {}).get("uri") if isinstance(bp.get("dataType"), dict) else None,
                        "tableName": bp.get("allTables", [None])[0] if bp.get("allTables") else None,
                        "fieldName": bp.get("fieldName"),  # 【v30.x】保存 fieldName 用于添加到主表
                        "isCharacterRef": True,
                        "isCharacterField": True,
                    })
                else:
                    # 普通特征字段：添加自身和 characterFields
                    character_fields_info.append(bp)
                    # 【v10.5 新增】也收集主表 businessProperty 中的 characterFields 信息
                    cfs = bp.get("characterFields")
                    if isinstance(cfs, list):
                        for cf in cfs:
                            if isinstance(cf, dict):
                                # 提取 characterField 的信息
                                cf_all_tables = cf.get("allTables", [])
                                cf_table_name = cf_all_tables[0] if cf_all_tables else None
                                cf_all_cols = cf.get("allColumns", [])
                                cf_cols_info = {}
                                if isinstance(cf_all_cols, list):
                                    for col_item in cf_all_cols:
                                        if isinstance(col_item, dict):
                                            col_n = _text(col_item, "columnName")
                                            if col_n:
                                                cf_cols_info[col_n] = {
                                                    "displayName": _text(col_item, "displayName"),
                                                    "type": _text(col_item, "type"),
                                                    "typeUri": _text(col_item, "typeUri"),
                                                }
                                character_fields_info.append({
                                    "name": _text(cf, "name"),
                                    "displayName": _text(cf, "displayName"),
                                    "uri": _text(cf, "uri"),
                                    "tableName": cf_table_name,
                                    "isCharacterField": True,
                                    "columns": cf_cols_info,
                                })

    # 【优化 v5.0】仅收集匹配字段的参照 URI，不展开所有参照
    # 1. 先过滤出实际需要的属性（matched_attrs）
    # 【v7.0 修复】使用 effective_doc_fields 而非 doc_fields，
    # 因为 doc_fields 在函数内部重新赋值无法影响外部传入的值
    # 【v10.0 修复】当有 effective_doc_fields 时，同时保留默认必需字段
    # 【v12.1 修复】支持路径部分匹配：Excel列头可能是 "收款单基本信息.单据日期" 格式，
    #              需要检查 displayName 是否在路径字符串中
    matched_attrs: List[AttributeInfo] = []
    # 【v10.1 修复】将默认必需字段转为小写，用于不区分大小写的比较
    _default_required_lower: Set[str] = {f.lower() for f in _DEFAULT_REQUIRED_FIELDS}
    # 【v17.x】合并所有过滤字段来源
    _all_filter_fields: Set[str] = set()
    if effective_fields:
        _all_filter_fields.update(f.lower() for f in effective_fields)
    if effective_doc_fields:
        _all_filter_fields.update(f.lower() for f in effective_doc_fields)
    _all_filter_fields.update(_default_required_lower)
    _path_doc_fields_lower: List[str] = [f.lower() for f in (effective_fields or effective_doc_fields or []) if "." in f]

    if is_sql_y and group.attributes:
        for attr in group.attributes:
            # 【v17.x 启用】表头过滤逻辑
            if _all_filter_fields:
                dn = (attr.display_name or "").lower()
                dbn = (attr.db_column_name or "").lower()
                # 【修复】匹配条件：精确匹配 OR 默认必需字段（移除子串匹配）
                # 1. 精确匹配：displayName 或 dbColumnName 在列表中
                # 2. 默认必需字段：dbColumnName 在默认列表中
                matched = (
                    dn in _all_filter_fields or
                    dbn in _all_filter_fields or
                    dbn in _default_required_lower
                )
                if not matched:
                    continue
            matched_attrs.append(attr)
    # 2. 只对匹配的属性收集 URI
    uris: Set[str] = set()
    for attr in matched_attrs:
        if attr.uri:
            uris.add(attr.uri)

    # 【v17.x 调试】统计属性和参照情况
    total_attrs = len(matched_attrs)
    ref_attrs = len(uris)
    # 按 URI 分组统计
    uri_count: Dict[str, int] = {}
    for attr in matched_attrs:
        if attr.uri:
            uri_count[attr.uri] = uri_count.get(attr.uri, 0) + 1

    logger.info(f"【v17.x】实体属性统计: 总属性数={total_attrs}, 参照字段数={ref_attrs}")
    if ref_attrs > 0:
        logger.info(f"【v17.x】参照 URI 明细（{ref_attrs} 个）:")
        for i, (uri, cnt) in enumerate(sorted(uri_count.items(), key=lambda x: -x[1]), 1):
            logger.info(f"  [{i:2d}] {uri} (出现{cnt}次)")

    # 引用字段数量限制（防止过多引用字段导致超时/网关压力）
    skipped_uris: Set[str] = set()
    if len(uris) > max_ref:
        uri_list = list(uris)
        skipped = uri_list[max_ref:]
        logger.warning(
            "⚠️ 参照字段超过上限！总数=%d，上限=%d，已跳过 %d 个参照。",
            len(uris), max_ref, len(skipped)
        )
        logger.warning("【v17.x】已展开的参照 URI:")
        for i, uri in enumerate(sorted(uri_list[:max_ref]), 1):
            logger.warning(f"  [{i:2d}] {uri}")
        logger.warning("【v17.x】跳过的参照 URI:")
        for i, uri in enumerate(sorted(skipped), 1):
            logger.warning(f"  [{i:2d}] {uri}")
        logger.warning("如需展开全部参照，请在 docFields 中指定具体字段以缩小范围，"
                      "或通过 config.yaml 调高 performance.max_reference_fields_expand")
        uris = set(uri_list[:max_ref])
        skipped_uris = set(skipped)

    # 【v6.0 优化】使用批量查询，返回已解析的对象字典（无需 json.loads）
    uri_to_obj: Dict[str, Any] = (
        fetch_entity_db_info_batch(cfg, uris) if uris else {}
    )

    # 【优化 v5.0】直接遍历 matched_attrs（已过滤），避免重复过滤
    attrs_out: List[Dict[str, Any]] = []
    for attr in matched_attrs:
        am = attr_to_map(attr)
        tn = prop_map.get(attr.name or "") or prop_map.get(attr.display_name or "")
        if not tn:
            tn = group.table_name
        am["tableName"] = tn
        attr_uri = attr.uri
        if attr_uri and is_sql_y:
            # 检查该 URI 是否被跳过（因数量过多）
            if attr_uri in skipped_uris:
                am["referenceSkipped"] = True
                am["referenceSkipReason"] = (
                    f"参照总数超过上限({max_ref})，此参照被跳过。"
                    f"如需展开请在 docFields 中指定该字段，或调高 max_reference_fields_expand"
                )
            else:
                # 【v6.0 优化】uri_to_obj 已是已解析的对象，无需 json.loads()
                ref_obj = uri_to_obj.get(attr_uri)
                if ref_obj:
                    try:
                        ref_groups = parse(ref_obj, fetch_uri_json)
                        if ref_groups:
                            ref_group = ref_groups[0]
                            ref_attrs: List[Dict[str, Any]] = []
                            for ra in ref_group.attributes or []:
                                an = ra.name
                                dn = ra.display_name or ""
                                if an in ("name", "code") or (
                                    table_template and dn and dn in str(table_template)
                                ):
                                    ref_attrs.append(
                                        {
                                            "dbColumnName": ra.db_column_name,
                                            "displayName": ra.display_name,
                                            "primarykey": "id",
                                        }
                                    )
                            domain = ref_group.domain
                            ref_structure: Dict[str, Any] = {
                                "billName": ref_group.bill_name,
                                "domain": domain,
                                "tableName": ref_group.table_name,
                                "attributes": ref_attrs,
                            }
                            # 【v12.x 修复】优先使用 FastLookup 获取 schema
                            sch = None
                            fast_lookup = _get_fast_lookup(cfg)
                            if fast_lookup.is_loaded and attr_uri:
                                hit = fast_lookup.get_by_uri(attr_uri)
                                if hit and hit.schema:
                                    sch = hit.schema
                                    logger.info(f"[FastLookup] 参照 schema 命中: {attr_uri} → {sch}")
                            # 【v12.x】FastLookup URI 未命中时，调用 searchByName → byboid → entityid 三级 API 获取 schema
                            if not sch and attr_uri:
                                sch = _fetch_schema_via_three_apis(cfg, attr_uri)
                            if not sch or sch == "scheme":
                                sch = None  # 避免写入占位符
                            ref_structure["scheme"] = sch
                            am["referenceStructure"] = ref_structure
                    except (KeyError, IndexError) as e:
                        logger.warning(f"解析引用 {attr_uri} 失败: {e}")
        attrs_out.append(am)

    # 【v10.4 优化】将 businessProperties 中的特征字段信息补充到 attributes
    # 这些字段可能不在 group.attributes 中，但需要能被查询到
    existing_attr_names = {attr.db_column_name for attr in matched_attrs}
    for cf_info in character_fields_info:
        cf_table = cf_info.get("tableName", "")
        is_character_ref = cf_info.get("isCharacterRef", False)
        # 【v30.x 新增】对于自定义项，直接使用 fieldName 作为列名
        # 自定义项有 fieldName 但没有 columns
        cf_field_name = cf_info.get("fieldName")
        if is_character_ref and cf_field_name:
            # 自定义项：使用 fieldName 作为列名
            col_name = cf_field_name
            if col_name in existing_attr_names:
                continue
            # 【v30.x】添加自定义项参照字段到主表
            cf_attr_map: Dict[str, Any] = {
                "displayName": cf_info.get("displayName") or "自定义项",
                "dbColumnName": col_name,
                "type": "参照类型，此处为参照对象id，long类型，需使用toString()",
                "tableName": group.table_name,
                "isCharacterField": True,
            }
            # 如果有 URI，添加到参照处理
            cf_uri = cf_info.get("uri")
            if cf_uri and is_sql_y and cf_uri not in skipped_uris:
                try:
                    ref_obj = uri_to_obj.get(cf_uri)
                    if ref_obj:
                        ref_groups = parse(ref_obj, fetch_uri_json)
                        if ref_groups:
                            ref_group = ref_groups[0]
                            ref_attrs: List[Dict[str, Any]] = []
                            for ra in ref_group.attributes or []:
                                if ra.name in ("name", "code"):
                                    ref_attrs.append({
                                        "dbColumnName": ra.db_column_name,
                                        "displayName": ra.display_name,
                                        "primarykey": "id",
                                    })
                            ref_structure: Dict[str, Any] = {
                                "billName": ref_group.bill_name,
                                "domain": ref_group.domain,
                                "tableName": ref_group.table_name,
                                "attributes": ref_attrs,
                            }
                            # 获取 schema
                            sch = None
                            fast_lookup = _get_fast_lookup(cfg)
                            if fast_lookup.is_loaded and cf_uri:
                                hit = fast_lookup.get_by_uri(cf_uri)
                                if hit and hit.schema:
                                    sch = hit.schema
                            if not sch:
                                sch = _fetch_schema_via_three_apis(cfg, cf_uri)
                            ref_structure["scheme"] = sch if sch and sch != "scheme" else None
                            cf_attr_map["referenceStructure"] = ref_structure
                except (KeyError, IndexError) as Exception:
                    pass
            attrs_out.append(cf_attr_map)
            existing_attr_names.add(col_name)
            continue
        # 【v29.x 新增】如果 _include_from_parent_table 标记为 True，则包含该字段
        include_from_parent = cf_info.get("_include_from_parent_table", False)
        # 只处理属于当前 group 表的特征字段，或者标记为从父表包含的字段
        if cf_table and cf_table != group.table_name and not include_from_parent:
            continue
        # 获取特征字段的列信息
        cols = cf_info.get("columns", {})
        for col_name, col_data in cols.items():
            if col_name in existing_attr_names:
                continue
            # 【v10.4】添加特征字段到输出
            cf_attr_map: Dict[str, Any] = {
                "displayName": col_data.get("displayName") or cf_info.get("displayName") or col_name,
                "dbColumnName": col_name,
                "type": col_data.get("type") or col_data.get("typeUri") or "String",
                "tableName": cf_table or group.table_name,
                "isCharacterField": True,  # 【v10.4】标记为特征字段
            }
            # 如果特征字段有 URI，添加到 uris 列表以便后续处理
            cf_uri = cf_info.get("uri")
            if cf_uri and is_sql_y and cf_uri not in skipped_uris:
                # 为参照类型创建 referenceStructure
                ref_obj = uri_to_obj.get(cf_uri)
                if ref_obj:
                    try:
                        ref_groups = parse(ref_obj, fetch_uri_json)
                        if ref_groups:
                            ref_group = ref_groups[0]
                            ref_attrs: List[Dict[str, Any]] = []
                            for ra in ref_group.attributes or []:
                                if ra.name in ("name", "code"):
                                    ref_attrs.append({
                                        "dbColumnName": ra.db_column_name,
                                        "displayName": ra.display_name,
                                        "primarykey": "id",
                                    })
                            ref_structure: Dict[str, Any] = {
                                "billName": ref_group.bill_name,
                                "domain": ref_group.domain,
                                "tableName": ref_group.table_name,
                                "attributes": ref_attrs,
                            }
                            # 【v12.x 修复】优先使用 FastLookup 获取 schema
                            sch = None
                            fast_lookup = _get_fast_lookup(cfg)
                            if fast_lookup.is_loaded and cf_uri:
                                hit = fast_lookup.get_by_uri(cf_uri)
                                if hit and hit.schema:
                                    sch = hit.schema
                                    logger.info(f"[FastLookup] 特征字段参照 schema 命中: {cf_uri} → {sch}")
                            # 【v12.x】FastLookup URI 未命中时，调用 searchByName → byboid → entityid 三级 API 获取 schema
                            if not sch and cf_uri:
                                sch = _fetch_schema_via_three_apis(cfg, cf_uri)
                            ref_structure["scheme"] = sch if sch and sch != "scheme" else None
                            cf_attr_map["referenceStructure"] = ref_structure
                    except (KeyError, IndexError) as e:
                        logger.warning(f"解析特征字段参照 {cf_uri} 失败: {e}")
            attrs_out.append(cf_attr_map)

    m["attributes"] = attrs_out
    return m


def filter_strip_attribute_names(root: Dict[str, Any]) -> Dict[str, Any]:
    """移除 attributes 中的 name 字段（对齐 ReportSQLGenTool）"""
    entities = root.get("entities")
    if not isinstance(entities, list):
        return root
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        attributes = ent.get("attributes")
        if not isinstance(attributes, list):
            continue
        for attr in attributes:
            if isinstance(attr, dict) and "name" in attr:
                del attr["name"]
    return root


def _should_interactive_selection_prompt() -> bool:
    """与插件 / Agent 非 TTY 环境：不阻塞 input，仅输出可解析的 stdout（见 Chat 侧栏多 URI 选择器）。"""
    if not sys.stdin.isatty():
        return False
    v = (os.environ.get("HYPERION_NON_INTERACTIVE") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return False
    return True


def _format_byname_bo_selection_stdout(selection_info: Dict[str, Any]) -> str:
    """与 Chat.parseBillNameChoiceFromStdout 一致（searchByName 多业务对象）。"""
    names = selection_info.get("names") or []
    return "停止继续往下走，请从以下单据名称中选择：" + "、".join(str(x) for x in names if x)


def _format_selection_stdout_for_ui(selection_info: Dict[str, Any]) -> str:
    """格式化业务对象选择提示（仅 byname 路径）。"""
    return _format_byname_bo_selection_stdout(selection_info)


def build_entities_for_bill(
    cfg: dict, billname_trim: str, bill_info: List[Dict[str, str]] = None
) -> Tuple[List[Dict[str, Any]], Optional[str], Dict[str, str]]:
    """为单个单据构建实体列表

    Returns:
        Tuple of (entities, error, char_class_ids)
    """
    api = cfg.get("api") or {}
    req = cfg.get("request") or {}
    doc_fields = parse_doc_fields(req.get("docFields"))
    if str(req.get("isDescField", "Y")).upper() == "N":
        doc_fields = []

    # 【v7.0 优化】requestText 提取移至 biz_table_group_to_entity_map 内部，
    # 确保 Excel 列头（excel_cols）优先于 requestText 语义匹配，
    # 符合"字段必须来源于 Excel 描述"的要求。
    # doc_fields 保持为用户显式指定的字段列表（request.docFields），不在此处提前提取。
    request_text = req.get("requestText", "") or ""

    # 【优化】如果指定了 excelFile，自动从 Excel 提取信息
    excel_file = req.get("excelFile") or getattr(cfg, "excel_file", None)
    excel_cols: List[str] = []  # Excel 列头（用于语义匹配字段 displayName）
    data_source_fields: List[str] = []  # 【v5.1 新增】Excel "数据源字段"列值（用于过滤参照元数据）
    effective_fields: List[str] = []  # 【v17.x】从 Excel 描述行提取的有效字段（用于参照展开过滤）

    # 【优化 v11.x】优先使用 run_all_bills 中预解析的 Excel 结果，避免重复解析
    if cfg.get("_excel_parsed"):
        excel_cols = cfg.get("_excel_cols", [])
        data_source_fields = cfg.get("_data_source_fields", [])
        effective_fields = cfg.get("_effective_fields", [])
        logger.info(f"【优化 v11.x】复用预解析的 Excel 结果: {len(excel_cols)} 个字段列头, {len(effective_fields)} 个有效字段")
    elif excel_file:
        try:
            excel_path = Path(excel_file)
            if excel_path.exists():
                logger.info(f"从 Excel 自动提取信息: {excel_file}")
                excel_result = parse_excel(excel_path, int(req.get("excelSheetIndex", 0) or 0))
                # Excel 列头（用于语义匹配）
                all_excel_cols = excel_result.get("docFields", [])
                # 【v7.0 优化】过滤描述性列头，只保留真正的业务字段列
                # 这些列头是元信息列，不是业务字段，不应参与语义匹配
                _EXCLUDE_EXCEL_HEADERS: Set[str] = {
                    "序号", "编号", "报表编号", "报表编号", "报表",
                    "报表名称", "报表名称", "名称",
                    "难度等级", "难度名称", "难度", "等级",
                    "场景说明", "说明", "描述",
                    "技能要求", "要求", "能力",
                    "报表数量", "数量", "统计",
                    "备注", "内容",
                    "分类", "类型",
                    "核心业务对象", "业务对象", "数据源",
                    "单据名称", "业务对象名称", "报表对象",
                    "单据", "实体", "来源单据",
                    "数据源字段", "数据源(业务对象)", "来源业务对象",
                    "来源", "业务对象来源",
                }
                excel_cols = [
                    c for c in all_excel_cols
                    if c.strip() and c.strip() not in _EXCLUDE_EXCEL_HEADERS
                ]
                if excel_cols:
                    logger.info(f"自动提取 Excel 字段列头: {excel_cols}")
                # 【v17.x 新增】从 Excel 描述行提取的有效字段（用于参照展开过滤）
                effective_fields = excel_result.get("effectiveFields", [])
                if effective_fields:
                    logger.info(f"【v17.x】从 Excel 提取有效字段: {len(effective_fields)} 个")
                    logger.debug(f"  effective_fields: {effective_fields}")
                # 【v5.1 新增】"数据源字段"列的值（按分隔符拆分后的业务对象名称，用于过滤参照字段）
                data_source = excel_result.get("dataSource") or {}
                data_source_fields = data_source.get("dataSourceFields", [])
                if data_source_fields:
                    logger.info(f"【v5.1】提取数据源字段: {data_source_fields}")
        except Exception as e:
            logger.warning(f"从 Excel 提取信息失败: {e}")

    is_sql_y = str(req.get("is_sql", "Y")).upper() == "Y"
    table_template = req.get("tableTemplate")
    # 【优化 v5.0】永远不查询子表，只获取用户明确指定的业务对象
    # 好处：
    # 避免is_include_sub=Y 时自动把主表下所有子实体都拉进来，导致：
    # 数据量膨胀（几十个实体）
    # 无用实体干扰后续字段分析
    # SQL JOIN 链变得复杂且不准确
    is_include_sub = False

    # queryUri 优先路径：直接用 URI 查，跳过 searchByName/byboid
    quri = (req.get("queryUri") or "").strip()
    # 【v8.5】检查是否有 URI 列表（来自 run_all_bills 的分割处理）
    uri_list = req.get("_query_uri_list") or []
    # 【v16.x 重构】直接从 bill_info 获取 URI 和 schema
    _bill_info = bill_info or []
    _item = _get_bill_info_item(_bill_info, billname_trim)
    _quri_from_bill_info = _item["uri"] if _item and _item.get("uri") else ""
    _schema = _item["schema"] if _item and _item.get("schema") else ""
    # 优先使用显式 quri，否则使用 bill_info 中的 quri
    if not quri:
        quri = _quri_from_bill_info

    if quri:
        logger.info(f"【v16.x】直接通过 queryUri 查询: {quri}")

        # 【v33.x 修复】即使 schema 已存在，也需要调用 _fetch_schema_via_three_apis
        # 以获取 businessProperties（自定义项信息）
        # 原逻辑只在 schema 不存在时才调用，导致 businessProperties 永远无法获取
        if _schema:
            logger.info(f"【v16.x】bill_info 中已有 schema: {_schema}，但仍需获取 businessProperties")
        else:
            logger.info(f"【v16.x】URI={quri} 但 schema 不存在，调用 _fetch_schema_via_three_apis")
        # 【v33.x 修改】总是调用 _fetch_schema_via_three_apis 以获取 businessProperties
        global _session_cache
        cache_key = _build_entity_info_cache_key(cfg, quri)
        _fetched_schema = None
        if _session_cache and cache_key in _session_cache:
            raw_ent = _session_cache[cache_key]
            _fetched_schema = _extract_schema_from_raw_ent(raw_ent)
            if _fetched_schema:
                logger.info(f"【v16.x】从 session 缓存获取 schema: {_fetched_schema}")
        if not _fetched_schema:
            _fetched_schema = _fetch_schema_via_three_apis(cfg, quri)
        if _fetched_schema:
            logger.info(f"【v16.x】获取到 schema: {_fetched_schema}")
            # 更新 bill_info 中的 schema
            _update_bill_info(_bill_info, billname_trim, schema=_fetched_schema)
            _schema = _fetched_schema
        else:
            logger.warning(f"【v16.x】无法获取 URI={quri} 的 schema，将继续查询")

        def fetch_uri_direct(u: str) -> str:
            return _query_by_uri_cached(cfg, u)

        # 【v6.0 优化】使用 _query_by_uri_cached_parsed 直接获取已解析的对象
        # 【v16.x 优化】传入 _schema，让缓存数据也包含 schema
        db_obj = _query_by_uri_cached_parsed(cfg, quri, _schema)
        if db_obj is None:
            return [], f"错误: 无法获取 URI {quri} 的元数据", {}

        # 【v16.x 优化】schema 已通过 bill_info 前置补全，db_obj 缓存时也已注入 schema，跳过 FastLookup 补全

        # 提取 groups_data
        groups_data = db_obj.get("groups") or []
        if not groups_data:
            # 【v10.2 修复】处理多层嵌套的 data 结构
            data_obj = db_obj.get("data", {})
            # 逐层检查 groups
            for level in range(5):
                if "groups" in data_obj:
                    groups_data = data_obj["groups"] if isinstance(data_obj["groups"], list) else []
                    logger.info(f"【v16.x】groups_data: {len(groups_data)} 个")
                    break
                if isinstance(data_obj.get("data"), dict):
                    data_obj = data_obj["data"]
                else:
                    break

        # 【v16.x 优化】schema 已前置补全，只需检查 groups_data 中是否有 schema
        has_schema = any(
            isinstance(g, dict) and g.get("schema")
            for g in groups_data
        ) or bool(_schema)

        if not has_schema:
            logger.warning(f"[WARN] queryUri 返回缺少 schema（URI: {quri}）")

        groups = parse(db_obj, fetch_uri_json=fetch_uri_direct)
        fkeys = parse_foreign_keys_util(db_obj, quri)

        results: List[Dict[str, Any]] = []
        # 【v23.x 修复】queryUri 模式下不做任何过滤
        # queryUri 本身就是从 billObjects 中获取的
        # 【v24.x】根据当前 billName 判断是否需要 tableName 过滤：
        # - 如果 billName 有对应的 tableName，用 tableName 精确匹配
        # - 如果 billName 没有 tableName（如"金融产品"），保留所有返回的实体

        # 获取 billName -> tableName 映射
        bill_name_to_table = cfg.get("_bill_name_to_table", {})
        bill_names_without_table = cfg.get("_bill_names_without_table", set())

        # 当前 billName 对应的 tableName
        current_bill_name_lower = billname_trim.lower()
        expected_table_name = bill_name_to_table.get(current_bill_name_lower, "")
        skip_table_filter = current_bill_name_lower in bill_names_without_table or not expected_table_name

        if skip_table_filter:
            logger.info(f"【v24.x】billName='{billname_trim}' 无 tableName 声明，保留所有返回实体")
        else:
            logger.info(f"【v24.x】billName='{billname_trim}' 使用 tableName 过滤: {expected_table_name}")

        for group in groups:
            # 【v24.x】tableName 过滤：只有当有声明 tableName 时才进行过滤
            group_table_name = getattr(group, 'table_name', None) or ""
            if not skip_table_filter and group_table_name:
                if group_table_name.lower() != expected_table_name:
                    logger.debug(f"【v24.x】过滤掉非目标表: {group_table_name} (期望: {expected_table_name})")
                    continue
            # 【v25.x】排除接口表（I 开头的表名，如 IYTenant, ITenant, IAuditInfo）
            if group_table_name and group_table_name.startswith("I"):
                logger.debug(f"【v25.x】过滤掉接口表: {group_table_name}")
                continue
            # 【v31.x 新增】从 db_obj 中提取 businessProperties 用于自定义项处理
            # 【v32.x 修复】处理嵌套的 data.data 结构：API 返回 {data: {data: {...实际数据...}}}
            entity_model_from_db: Optional[Dict[str, Any]] = None
            if isinstance(db_obj, dict):
                data_node = db_obj.get("data")
                if isinstance(data_node, dict):
                    # 检查 businessProperties 是否在 data.data 中（嵌套结构）
                    inner_data = data_node.get("data")
                    if isinstance(inner_data, dict) and inner_data.get("businessProperties"):
                        biz_props = inner_data.get("businessProperties") or []
                    elif data_node.get("businessProperties"):
                        # businessProperties 在第一层 data 中
                        biz_props = data_node.get("businessProperties") or []
                    else:
                        biz_props = []
                    if biz_props:
                        entity_model_from_db = {"businessProperties": biz_props}
                        logger.debug(f"【v32.x】从 db_obj 提取 {len(biz_props)} 个 businessProperties 用于自定义项处理")
            # 伪造一个 entity 字典供 biz_table_group_to_entity_map 使用
            fake_ent: Dict[str, Optional[str]] = {
                "uri": quri,
                "entityId": None,
                "boId": None,
                "businessObjectCode": None,
            }
            # 【v9.0】传递 default_schema
            default_schema = getattr(group, 'schema', None)
            emap = biz_table_group_to_entity_map(
                group,
                fake_ent,
                entity_model_from_db,  # 【v31.x】传递 businessProperties 用于自定义项处理
                fkeys,
                data_source_fields or doc_fields,  # 【v5.1】优先用数据源字段过滤参照
                is_sql_y,
                str(table_template) if table_template is not None else None,
                fetch_uri_direct,
                cfg,
                excel_cols=excel_cols,  # 用于语义匹配
                request_text=request_text,  # 【v7.0】自然语言请求文本
                default_schema=default_schema,  # 【v9.0】传递 schema
                effective_fields=effective_fields,  # 【v17.x】从 Excel 描述行提取的有效字段
            )
            if emap:
                results.append(emap)
        logger.info(f"queryUri 直查完成，共获取 {len(results)} 个实体")
        return results, None, {}

    # 【v17.x 重构】直接使用 bill_info 中的 URI（已由 run_all_bills 预补全）
    logger.info(f"【v17.x】从 bill_info 构建实体: {billname_trim}")

    # 从 bill_info 获取 URI 和 schema
    _item = _get_bill_info_item(_bill_info, billname_trim)
    if not _item or not _item.get("uri"):
        return [], f"错误: bill_info 中缺少 URI: {billname_trim}", {}

    uri = _item.get("uri", "") if _item else ""
    schema = _item.get("schema", "") or ""

    # 如果 schema 不存在，尝试补全
    if not schema:
        fetched_schema = _fetch_schema_via_three_apis(cfg, uri) or ""
        if fetched_schema:
            schema = fetched_schema
            # 【v51.x 修复】将补全的 schema 更新到 bill_info，避免后续调用丢失
            _item["schema"] = schema
            logger.info(f"【v51.x】schema 已更新到 bill_info: {billname_trim} -> {schema}")

    # 调用 queryUri 获取元数据
    db_obj = _query_by_uri_cached_parsed(cfg, uri, schema)
    if db_obj is None:
        return [], f"错误: 无法获取 URI {uri} 的元数据", {}

    # 提取 groups_data
    groups_data = db_obj.get("groups") or []
    if not groups_data:
        inner = db_obj.get("data", {}).get("groups", [])
        if isinstance(inner, list):
            groups_data = inner

    # 构造实体信息
    ent: Dict[str, Optional[str]] = {
        "uri": uri,
        "entityId": None,
        "boId": None,
        "businessObjectCode": _item.get("businessObjectCode") or "",
    }

    # 【v32.x 修复】处理 businessProperties 合并
    # API 返回结构是 db_obj = {data: {data: {...实际数据...}}}
    bp = _item.get("businessProperties") or []
    if bp and isinstance(bp, list):
        outer_data = db_obj.get("data", {})
        if isinstance(outer_data, dict):
            inner_data = outer_data.get("data")
            if isinstance(inner_data, dict):
                existing_bp = inner_data.get("businessProperties", [])
                if not isinstance(existing_bp, list):
                    existing_bp = []
                existing_names = {item.get("name") for item in existing_bp if isinstance(item, dict)}
                for item in bp:
                    if isinstance(item, dict) and item.get("name") not in existing_names:
                        existing_bp.append(item)
                        existing_names.add(item.get("name"))
                inner_data["businessProperties"] = existing_bp

    # 【v10.7 新增】累积特征表 classId 映射
    all_char_class_ids: Dict[str, str] = {}

    def fetch_uri(u: str) -> str:
        return _query_by_uri_cached(cfg, u)

    # 复用 queryUri 路径的处理逻辑
    groups = parse(db_obj, fetch_uri_json=fetch_uri)
    fkeys = parse_foreign_keys_util(db_obj, uri)

    # 【v32.x 新增】提取 businessProperties 用于自定义项处理
    # API 返回结构是 db_obj = {data: {data: {...实际数据...}}}
    entity_model_from_db: Optional[Dict[str, Any]] = None
    outer_data = db_obj.get("data", {})
    if isinstance(outer_data, dict):
        inner_data = outer_data.get("data", {})
        if isinstance(inner_data, dict):
            biz_props = inner_data.get("businessProperties", [])
            if biz_props:
                entity_model_from_db = {"businessProperties": biz_props}
                logger.debug(f"【v32.x】从 bill_info 路径提取 {len(biz_props)} 个 businessProperties")

    results: List[Dict[str, Any]] = []
    # queryUri 路径：若 billname_trim 是自动填充的占位符（如 "metadata"），则不过滤
    skip_filter = billname_trim in ("metadata", "query", "")

    for group in groups:
        gb = group.bill_name or ""
        tn = group.table_name or ""
        # 【v17.x 简化】过滤非业务实体
        if not skip_filter:
            if not (gb == billname_trim or billname_trim in gb or gb in billname_trim):
                continue
        if "_dcs" in tn or "ibpm" in tn or "审批" in gb or "税单" in gb or "calc" in tn or "result" in tn:
            continue

        default_schema = getattr(group, 'schema', None) or schema
        emap = biz_table_group_to_entity_map(
            group,
            ent,
            entity_model_from_db,  # 【v32.x】传递 businessProperties 用于自定义项处理
            fkeys,
            data_source_fields or doc_fields,
            is_sql_y,
            str(table_template) if table_template is not None else None,
            fetch_uri,
            cfg,
            excel_cols=excel_cols,
            request_text=request_text,
            default_schema=default_schema,
            effective_fields=effective_fields,  # 【v17.x】从 Excel 描述行提取的有效字段
        )
        if emap:
            results.append(emap)

    logger.info(f"【v17.x】bill_info 构建完成，共获取 {len(results)} 个实体")

    # 【v17.x 简化】过滤非主子表实体
    def should_filter(entity: dict) -> bool:
        table_name = entity.get("tableName", "")
        bill_name = entity.get("billName", "")
        relation_type = entity.get("relation_type")  # 【v36.x 新增】主表/平行表/扩展表标识

        # 【v44.x 新增】优先保护：Excel billObjects 中明确列出的 tableName 必须保留
        bill_object_table_names = cfg.get("_bill_object_table_names", set())
        if bill_object_table_names and table_name.lower() in bill_object_table_names:
            logger.info(f"【v44.x】保护 Excel billObjects 中的 tableName: {table_name}")
            return False  # 保留

        if "_dcs" in table_name:
            return True
        if "ibpmcurrentauditor" in table_name or "ibpmstep" in table_name:
            return True
        if "审批" in bill_name:
            return True
        # 【v36.x 新增】保护所有特征表和特性表（保留）
        if "_characteristics" in table_name or "_character_define" in table_name or "_feature" in table_name:
            return False  # 保留所有特征表和特性表
        # 【v36.x 新增】过滤扩展表：relation_type 为 None 且不是平行表
        if relation_type is None and "_parallel" not in table_name:
            # 扩展表黑名单
            EXTENSION_TABLES = {"ExternalSystem", "FiEventAccInfo", "FiInfoRecord", "QiMen",
                               "QiMenMultiplePostback", "FiInfo", "FiEventAcc"}
            attrs = entity.get("attributes", [])
            if table_name in EXTENSION_TABLES:
                return True  # 黑名单表
            if not attrs or len(attrs) == 0:
                return True  # 空表
        return False

    filtered_entities = [e for e in results if not should_filter(e)]
    if len(filtered_entities) < len(results):
        logger.info(f"[FILTER] 过滤非主子表实体: {len(results)} → {len(filtered_entities)} 个")

    return filtered_entities, None, all_char_class_ids


def load_excel_parsed_json(cfg: dict, json_path: Path) -> None:
    """
    【v16.x 新增】读取预解析的 excel_parsed.json（v2.0 格式），
    自动提取 billNames、uri、columns，填入 cfg["request"]。

    v2.0 结构：
    {
      "version": "2.0",
      "sheets": [{
        "sheetIndex": 0,
        "sheetName": "Sheet2",
        "columns": [...],
        "sections": {
          "businessObjects": {
            "items": [{billName, uri, tableName, relation, relatedFields}, ...]
          }
        }
      }]
    }
    """
    import json as _json

    path = Path(json_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"excel_parsed.json 不存在: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = _json.load(f)

    if data.get("version") != "2.0":
        logger.warning(f"excel_parsed.json version={data.get('version')}，可能不兼容 v2.0 格式")

    sheets = data.get("sheets", [])
    if not sheets:
        logger.warning("excel_parsed.json 中没有 sheets")
        return

    # 【v16.x 重构】使用 _bill_info 统一管理 billName + uri + schema
    bill_info: List[Dict[str, str]] = []  # [{billName, uri, schema}, ...]
    seen_bill_lower: Set[str] = set()
    all_columns: List[str] = []
    all_effective_fields: List[str] = []  # 【v17.x 新增】收集 effectiveFields
    parent_names: List[str] = []
    bill_object_table_names: Set[str] = set()  # 【v19.x 新增】收集 billObjects 的 tableName，用于过滤
    bill_names_without_table: Set[str] = set()  # 【v22.x 新增】收集没有 tableName 的 billName，不过滤
    bill_name_to_table: Dict[str, str] = {}  # 【v24.x 新增】billName -> tableName 映射

    for sheet in sheets:
        columns = sheet.get("columns", [])
        if columns:
            all_columns.extend(columns)

        # 【v17.x 新增】提取 effectiveFields
        effective_fields = sheet.get("effectiveFields", [])
        if effective_fields:
            all_effective_fields.extend(effective_fields)

        bo_section = sheet.get("sections", {}).get("businessObjects", {})
        items = bo_section.get("items", [])
        for item in items:
            bn = (item.get("billName") or "").strip()
            uri = (item.get("uri") or "").strip()
            parent = (item.get("parentName") or "").strip()
            table_name = (item.get("tableName") or "").strip()  # 【v19.x 新增】
            if not bn:
                continue
            bn_key = bn.lower()
            if bn_key not in seen_bill_lower:
                seen_bill_lower.add(bn_key)
                bill_info.append({"billName": bn, "uri": uri, "schema": ""})
            # 【v19.x 新增】收集 tableName
            if table_name:
                bill_object_table_names.add(table_name.lower())
                # 【v24.x 新增】收集 billName -> tableName 映射
                bill_name_to_table[bn.lower()] = table_name.lower()
            else:
                # 【v22.x 新增】没有 tableName 的 billName，不过滤
                bill_names_without_table.add(bn.lower())
            if parent and parent.lower() not in seen_bill_lower:
                parent_names.append(parent)

    # 【v16.x 新增】将 parentName 补充到 bill_info
    for pn in parent_names:
        if pn.lower() not in seen_bill_lower:
            seen_bill_lower.add(pn.lower())
            bill_info.append({"billName": pn, "uri": "", "schema": ""})

    # 【v38.x 新增】优先使用 dataSource.billNameUriMap 中的 URI 更新 bill_info
    # 这样可以避免用 billName 去搜索，直接使用 Excel 提供的 URI
    # 注意：dataSource 在 sheets[0] 或顶层，需要遍历 sheets 收集
    billname_uri_map: Dict[str, str] = {}  # 【v38.x】初始化
    for sheet in sheets:
        data_source = sheet.get("dataSource", {}) or data.get("dataSource", {})
        billname_uri_map_sheet = data_source.get("billNameUriMap", {})
        if billname_uri_map_sheet:
            billname_uri_map.update(billname_uri_map_sheet)
    # 如果 sheets 中没有，尝试从顶层获取
    if not billname_uri_map:
        data_source = data.get("dataSource", {})
        billname_uri_map = data_source.get("billNameUriMap", {})
    if billname_uri_map:
        for item in bill_info:
            bn = item.get("billName", "")
            if bn in billname_uri_map and billname_uri_map[bn]:
                item["uri"] = billname_uri_map[bn]
                logger.info(f"【v38.x】使用 billNameUriMap 更新 URI: {bn} -> {billname_uri_map[bn]}")
        logger.info(f"【v38.x】billNameUriMap 已应用，共 {len(billname_uri_map)} 个映射")

    # 构建 allbillname 和 uri_list（从 bill_info 提取）
    all_bill_names = [b["billName"] for b in bill_info]
    all_uris = [b["uri"] for b in bill_info]

    req = cfg.setdefault("request", {})

    if all_bill_names:
        req["allbillname"] = ",".join(all_bill_names)
        logger.info(f"【v16.x】从 excel_parsed.json 提取到 {len(all_bill_names)} 个业务对象: {all_bill_names}")

    # uri 列表（用于 queryUri 路径，按 index 与 allbillname 对齐）
    req["_query_uri_list"] = all_uris

    # 【v16.x 新增】存储统一的 bill_info
    cfg["_bill_info"] = bill_info

    # 【v19.x 新增】存储 billObjects 的 tableName，用于 queryUri 模式过滤
    if bill_object_table_names:
        cfg["_bill_object_table_names"] = bill_object_table_names
        logger.info(f"【v19.x】billObjects tableNames: {bill_object_table_names}")

    # 【v22.x 新增】存储没有 tableName 的 billName，这些实体的 tableName 不进行过滤
    if bill_names_without_table:
        cfg["_bill_names_without_table"] = bill_names_without_table
        logger.info(f"【v22.x】billNames without tableName (no filter): {bill_names_without_table}")

    # 【v24.x 新增】存储 billName -> tableName 映射
    if bill_name_to_table:
        cfg["_bill_name_to_table"] = bill_name_to_table
        logger.info(f"【v24.x】billName -> tableName 映射: {bill_name_to_table}")

    # 设置 columns（用于语义匹配）
    if all_columns:
        cfg["_excel_cols"] = all_columns
        cfg["_excel_parsed"] = True
        logger.info(f"【v16.x】从 excel_parsed.json 提取到 {len(all_columns)} 个列头")

    # 【v17.x 新增】设置 effectiveFields（用于字段语义匹配）
    if all_effective_fields:
        cfg["_effective_fields"] = all_effective_fields
        logger.info(f"【v17.x】从 excel_parsed.json 提取到 {len(all_effective_fields)} 个有效字段: {all_effective_fields}")

    # 【v44.x 新增】设置 columnFieldNames（业务字段名，用于字段过滤）
    all_column_field_names: List[str] = []
    all_column_field_mappings: List[Dict[str, Any]] = []
    for sheet in sheets:
        cfn = sheet.get("_columnFieldNames", [])
        if cfn:
            all_column_field_names.extend(cfn)
        # 【v45.x 新增】保存完整字段映射（含 steps），用于多级参照展开
        cfm = sheet.get("_columnFieldMappings", [])
        if cfm:
            all_column_field_mappings.extend(cfm)
    if all_column_field_names:
        cfg["_column_field_names"] = list(dict.fromkeys(all_column_field_names))  # 去重保持顺序
        logger.info(f"【v44.x】从 excel_parsed.json 提取到 {len(cfg['_column_field_names'])} 个业务字段名: {cfg['_column_field_names']}")
    if all_column_field_mappings:
        cfg["_column_field_mappings"] = all_column_field_mappings
        logger.info(f"【v45.x】从 excel_parsed.json 提取到 {len(cfg['_column_field_mappings'])} 个字段映射")

    # 记录文件来源
    cfg["_excel_parsed_path"] = str(path)


def run_all_bills(cfg: dict) -> Tuple[Dict[str, Any], Optional[str]]:
    """运行所有单据的元数据拉取（自动去重相同单据名称）"""
    # 【P0 优化】启动时加载 session 缓存，实现跨进程去重
    session_cache = _load_session_cache(cfg)

    api = cfg.get("api") or {}
    req = cfg.get("request") or {}
    quri = (req.get("queryUri") or req.get("query_uri") or "").strip()

    # 【v8.5 修复】参照实体批量查询时不加载旧索引，避免实体数量膨胀
    # 只有在主表/子表查询时（有 allbillname 且无 queryUri）才加载索引
    if not quri:
        # 【v6.0 优化】提前初始化 entities 索引，后续 write_outputs 复用内存中的索引
        _get_entities_index(cfg)
    allbill = str(req.get("allbillname", "")).strip()
    if not allbill and quri:
        # 【v8.5】queryUri 支持逗号分隔的多个 URI
        # 分割 URI 列表，每个 URI 作为一个 billname 处理
        uri_parts = [u.strip() for u in quri.split(",") if u.strip()]
        if len(uri_parts) > 1:
            # 多个 URI：分割成多个 parts，每个 URI 对应一个 "metadata" billname
            allbill = ",".join(["metadata"] * len(uri_parts))
            # 将 URI 列表存入配置，供后续 build_entities_for_bill 使用
            req["_query_uri_list"] = uri_parts
        else:
            allbill = "metadata"
    if not allbill:
        return (
            {"entities": []},
            "错误: 缺少必需的参数 'allbillname'（单据名称），或提供 queryUri 以仅按元数据 uri 直查",
        )
    parts = [p.strip() for p in allbill.split(",") if p.strip()]
    if not parts:
        return (
            {"entities": []},
            "错误: 缺少必需的参数 'allbillname'（单据名称），或提供 queryUri 以仅按元数据 uri 直查",
        )

    # 【v10.2 新增】解析 billname 中的 URI：如果包含 URI，严格按 URI 查询
    # 格式：单据名称（uri）或 单据名称（xxx.xxx.XxxVO）
    # 例如："物料（productcenter.pc_product）" → 提取 uri: productcenter.pc_product
    # 【v10.4 修复】合并 URI 提取和去重逻辑，确保 parts 和 uri_list 一一对应
    # 【v16.x 修复】如果是从 excel_parsed.json 预解析的，跳过此逻辑，保留预存的 URI
    # 【v18.x 修复】保留从 excel_parsed.json 加载的 bill_info
    if not cfg.get("_excel_parsed"):
        # 【v17.x 新增】初始化 bill_info 列表，包含所有 billName
        bill_info: List[Dict[str, str]] = [{"billName": p} for p in parts]
        cfg["_bill_info"] = bill_info
        import re as _re

        # 建立 (billname, uri) 配对列表
        bill_uri_pairs: List[Tuple[str, Optional[str]]] = []
        for p in parts:
            # 匹配中文括号（）或英文括号() 中的内容
            uri_match = _re.search(r'[（(]([^）)]+)[）)]', p)
            if uri_match:
                potential_uri = uri_match.group(1).strip()
                # URI 格式验证：包含 . 分隔符
                if '.' in potential_uri:
                    bill_uri_pairs.append(("metadata", potential_uri))
                    logger.info(f"【v10.2】从 billname 提取 URI: {p} → {potential_uri}")
                    continue
            bill_uri_pairs.append((p, None))

        # 【v10.4 修复】对配对列表去重
        # 去重规则：
        # - 有 URI 的（metadata）：按 URI 去重（不同 URI 保留）
        # - 无 URI 的（名称）：按名称去重（忽略大小写）
        seen_billnames: Set[str] = set()
        seen_uris: Set[str] = set()
        unique_pairs: List[Tuple[str, Optional[str]]] = []

        for billname, uri in bill_uri_pairs:
            if uri:
                # 有 URI：按 URI 去重
                if uri not in seen_uris:
                    seen_uris.add(uri)
                    unique_pairs.append((billname, uri))
            else:
                # 无 URI：按名称去重
                bill_key = billname.lower().strip()
                if bill_key not in seen_billnames:
                    seen_billnames.add(bill_key)
                    unique_pairs.append((billname, uri))

        # 重新构建 parts 和 uri_list
        parts = [p[0] for p in unique_pairs]
        uri_list = [p[1] for p in unique_pairs if p[1]]

        # 存入配置
        if uri_list:
            req["_query_uri_list"] = uri_list
            logger.info(f"【v10.4】billname 提取 URI 列表: {uri_list}")
            logger.info(f"【v10.4】queryUri 批量查询模式：{len(uri_list)} 个 URI")

        # 去重日志
        if len(unique_pairs) < len(bill_uri_pairs):
            dupes = len(bill_uri_pairs) - len(unique_pairs)
            logger.info(f"单据名称去重：原始 {len(bill_uri_pairs)} 个 → 去重后 {len(unique_pairs)} 个 （跳过 {dupes} 个重复）")
    else:
        logger.info("【v16.x】跳过 billname URI 提取（使用 excel_parsed.json 预存的 URI）")

    cached_parts: List[str] = []
    fresh_parts: List[str] = []

    sc_index = None
    if _SESSION_CACHE_INDEX_AVAILABLE:
        try:
            sc_index = get_session_cache_index(cfg)
        except Exception:
            pass

    # 【v16.x 修复】使用 uri 作为 session 缓存的 key，避免 billName 重复问题
    bill_info = cfg.get("_bill_info", [])
    use_uri_cache = cfg.get("_excel_parsed") and bill_info

    for p in parts:
        item = _get_bill_info_item(bill_info, p) if use_uri_cache else None
        uri = item.get("uri", "") if item else ""
        # 有 URI 且在缓存中 → 命中；否则走 API 拉取
        if use_uri_cache and uri and uri in session_cache:
            cached_parts.append(p)
        else:
            fresh_parts.append(p)

    if cached_parts:
        total_cached_entities = sum(
            len(session_cache.get(_get_bill_info_item(bill_info, p)["uri"] if _get_bill_info_item(bill_info, p) else "", []))
            for p in cached_parts
        )
        logger.info(
            f"【SESSION CACHE】跳过 {len(cached_parts)} 个已缓存业务对象的 API 调用 "
            f"({total_cached_entities} 个实体): {cached_parts}"
        )

    merged: List[Dict[str, Any]] = []
    if cached_parts:
        for p in cached_parts:
            item = _get_bill_info_item(bill_info, p)
            uri = item.get("uri", "") if item else ""
            if uri:
                cached_data = session_cache.get(uri, [])
                # 【v18.x 修复】确保 cached_data 是 list 类型，避免 entity_info: 条目混入
                if isinstance(cached_data, list):
                    merged.extend(cached_data)

    parts_to_fetch = fresh_parts

    if not parts_to_fetch:
        logger.info(
            f"【SESSION CACHE】所有 {len(parts)} 个业务对象均已缓存，跳过所有 API 调用，"
            f"合并 {len(merged)} 个实体直接返回"
        )
        # 【v10.6】补充特征表实体（已注释，char_class_ids=None 时无法调用 API）
        # merged = _supplement_characteristic_tables(cfg, merged, None)
        return {"entities": merged}, None

    logger.info(f"开始拉取 {len(parts_to_fetch)} 个单据: {', '.join(parts_to_fetch)}")

    # 【v17.x 新增】初始化 excel_parent_hints（在 Excel 解析块之外，确保总是可用）
    excel_parent_hints: Dict[Tuple[str, int], str] = {}

    # 【优化 v11.x】如果指定了 excelFile，在此处统一解析一次，避免并行时重复解析
    # 将解析结果存入 cfg，供所有 build_entities_for_bill 调用复用
    excel_file = req.get("excelFile") or getattr(cfg, "excel_file", None)
    if excel_file and not cfg.get("_excel_parsed"):
        try:
            excel_path = Path(excel_file)
            if excel_path.exists():
                excel_sheet_index = int(req.get("excelSheetIndex", 0) or 0)
                logger.info(f"【优化 v11.x】统一解析 Excel（避免并行重复解析）: {excel_file}")
                excel_result = parse_excel(excel_path, excel_sheet_index)
                all_excel_cols = excel_result.get("docFields", [])
                _EXCLUDE_EXCEL_HEADERS: Set[str] = {
                    "序号", "编号", "报表编号", "报表编号", "报表",
                    "报表名称", "报表名称", "名称",
                    "难度等级", "难度名称", "难度", "等级",
                    "场景说明", "说明", "描述",
                    "技能要求", "要求", "能力",
                    "报表数量", "数量", "统计",
                    "备注", "内容",
                    "分类", "类型",
                    "核心业务对象", "业务对象", "数据源",
                    "单据名称", "业务对象名称", "报表对象",
                    "单据", "实体", "来源单据",
                    "数据源字段", "数据源(业务对象)", "来源业务对象",
                    "来源", "业务对象来源",
                }
                cfg["_excel_cols"] = [
                    c for c in all_excel_cols
                    if c.strip() and c.strip() not in _EXCLUDE_EXCEL_HEADERS
                ]
                # 【v17.x 新增】保存 effective_fields
                cfg["_effective_fields"] = excel_result.get("effectiveFields", [])
                if cfg.get("_effective_fields"):
                    logger.info(f"【v17.x】有效字段已缓存: {len(cfg['_effective_fields'])} 个")
                # 【v44.x 新增】保存 columnFieldNames（业务字段名，用于字段过滤）
                # 从 sheets 中提取 _columnFieldNames
                all_cfn: List[str] = []
                for sheet in excel_result.get("sheets", []):
                    cfn = sheet.get("_columnFieldNames", [])
                    if cfn:
                        all_cfn.extend(cfn)
                    # 【v45.x 新增】保存完整字段映射（含 steps），用于多级参照展开
                    cfm = sheet.get("_columnFieldMappings", [])
                    if cfm:
                        cfg["_column_field_mappings"] = cfm
                        logger.info(f"【v45.x】业务字段映射已缓存: {len(cfm)} 个")
                if all_cfn:
                    cfg["_column_field_names"] = list(dict.fromkeys(all_cfn))  # 去重保持顺序
                    logger.info(f"【v44.x】业务字段名已缓存: {len(cfg['_column_field_names'])} 个: {cfg['_column_field_names']}")
                data_source = excel_result.get("dataSource") or {}
                cfg["_data_source_fields"] = data_source.get("dataSourceFields", [])
                cfg["_excel_parsed"] = True
                if cfg.get("_excel_cols"):
                    logger.info(f"【优化 v11.x】Excel 字段列头已缓存: {cfg['_excel_cols']}")
                if cfg.get("_data_source_fields"):
                    logger.info(f"【优化 v11.x】数据源字段已缓存: {cfg['_data_source_fields']}")

                # 【v12.x 新增】从 Excel billObjects 扩展 parts_to_fetch
                bill_objects = data_source.get("billObjects", [])
                if bill_objects:
                    excel_bill_names = []
                    excel_occurrence_counter: Dict[str, int] = {}
                    for bo in bill_objects:
                        bn = bo.get("billName", "")
                        if bn:
                            excel_bill_names.append(bn)
                            parent_hint = bo.get("parentName", "")
                            if parent_hint:
                                bn_lower = bn.lower()
                                occurrence = excel_occurrence_counter.get(bn_lower, 0)
                                excel_occurrence_counter[bn_lower] = occurrence + 1
                                excel_parent_hints[(bn_lower, occurrence)] = parent_hint
                                logger.info(f"【v17.x】收集 parent_hint: {bn}[{occurrence}] -> {parent_hint}")
                    existing_set = {p.lower() for p in parts_to_fetch}
                    new_bills = [bn for bn in excel_bill_names if bn.lower() not in existing_set]
                    if new_bills:
                        logger.info(f"【v12.x】从 Excel billObjects 扩展业务对象: {new_bills}")
                        parts_to_fetch.extend(new_bills)
        except Exception as e:
            logger.warning(f"【优化 v11.x】Excel 预解析失败，将使用原有逻辑: {e}")

    # 【v16.x 新增】预补全：无 URI 的 billName 通过 searchByName 补全 URI
    # 保证 bill_info 中的 URI 都是存在且唯一的
    bill_info = cfg.get("_bill_info", [])
    needs_lookup: List[Tuple[str, int]] = []  # (billName, occurrence)
    # 跟踪每个 billName 的出现次数，用于正确匹配 parent_hint
    lookup_occurrence_counter: Dict[str, int] = {}
    for bn in parts_to_fetch:
        bn_lower = bn.lower()
        occurrence = lookup_occurrence_counter.get(bn_lower, 0)
        lookup_occurrence_counter[bn_lower] = occurrence + 1
        item = _get_bill_info_item(bill_info, bn, occurrence)
        uri = item.get("uri", "") if item else ""
        if not uri:
            needs_lookup.append((bn, occurrence))

    if needs_lookup:
        logger.info(f"【v17.x】开始补全 {len(needs_lookup)} 个 billName 的 URI: {[b[0] for b in needs_lookup]}")
        byname_path = api.get("metadata_byname", "")
        byboid_path = api.get("metadata_byboid", "")
        fast_lookup = _get_fast_lookup(cfg)
        for bn, occurrence in needs_lookup:
            # 【v17.x 新增】从 excel_parent_hints 获取 parent_hint
            bn_lower = bn.lower()
            parent_hint = excel_parent_hints.get((bn_lower, occurrence), "")
            try:
                # 【v17.x 优化】如果有 parent_hint，先用 parent_hint 查询获取父实体 URI
                # 然后通过 getEntityListByBOId 获取正确的主子关系
                target_uri = None
                if parent_hint:
                    # 先查询 parent_hint 获取父实体的 URI
                    logger.info(f"【v17.x】{bn}[{occurrence}] 有 parent_hint={parent_hint}，先查询父实体")
                    cached_parent = _get_cached_byname(parent_hint)
                    if cached_parent:
                        raw_parent = json.loads(cached_parent)
                    else:
                        raw_parent = http_get_json(cfg, byname_path, {"key": parent_hint})
                        _set_cached_byname(parent_hint, json.dumps(raw_parent, ensure_ascii=False))
                    cn_parent = parse_business_object_code_name(raw_parent, parent_hint, cfg)
                    if cn_parent and cn_parent.uri:
                        parent_uri = cn_parent.uri
                        logger.info(f"【v17.x】父实体 {parent_hint} -> uri={parent_uri}")
                        # 用父实体的 URI 调用 getEntityListByBOId 获取所有子实体
                        boid_key = f"{cn_parent.id or ''}|{cn_parent.code or ''}"
                        cached_byboid = _get_cached_byboid(boid_key)
                        if cached_byboid:
                            raw_bo = json.loads(cached_byboid)
                        else:
                            raw_bo = http_get_json(cfg, byboid_path, {
                                "boId": cn_parent.id or "",
                                "businessObjectCode": cn_parent.code or ""
                            })
                            _set_cached_byboid(boid_key, json.dumps(raw_bo, ensure_ascii=False))
                        # 从子实体中找到 billName 匹配的
                        details = collect_entity_details(raw_bo)
                        # 【v17.x 改进】优先使用精确匹配（中文名）
                        for detail in details:
                            # 尝试精确匹配 billName（中文名）
                            detail_billname = detail.get("billName", "") or ""
                            if bn == detail_billname:
                                target_uri = detail.get("uri", "")
                                logger.info(f"【v17.x】精确匹配中文名 {bn} -> uri={target_uri}")
                                break
                        # 如果精确匹配失败，尝试语义匹配
                        if not target_uri:
                            for detail in details:
                                detail_uri = detail.get("uri", "")
                                if detail_uri and _semantic_match_billname(bn, detail_uri):
                                    target_uri = detail_uri
                                    logger.info(f"【v17.x】语义匹配 {bn} -> uri={target_uri}")
                                    break
                        # 如果语义匹配失败，尝试 URI 包含匹配
                        if not target_uri:
                            for detail in details:
                                detail_name = detail.get("billName", "") or ""
                                if bn in detail_name or detail_name in bn:
                                    target_uri = detail.get("uri", "")
                                    logger.info(f"【v17.x】URI包含匹配 {bn} -> uri={target_uri}")
                                    break

                # 如果没有通过 parent_hint 找到，用原始 billname 查询
                if not target_uri:
                    logger.info(f"【v35.x DEBUG】调用 parse_business_object_code_name，bn={bn}, parent_hint={parent_hint}")
                    cached = _get_cached_byname(bn)
                    logger.info(f"【v35.x DEBUG】cached={bool(cached)}")
                    if cached:
                        raw = json.loads(cached)
                    else:
                        raw = http_get_json(cfg, byname_path, {"key": bn})
                        _set_cached_byname(bn, json.dumps(raw, ensure_ascii=False))
                    cn = parse_business_object_code_name(raw, bn, cfg)
                    if cn:
                        logger.info(f"【v35.x DEBUG】cn 结果: uri={cn.uri}, code={cn.code}, id={cn.id}, name={cn.name}")
                        if cn.uri:
                            # 直接获取到 URI
                            target_uri = cn.uri
                        elif cn.code or cn.id:
                            # 【v35.x 新增】没有 URI 但有 code/id，通过 getEntityListByBOId 获取主表
                            logger.info(f"【v35.x】通过 getEntityListByBOId 获取主表: code={cn.code}, id={cn.id}")
                            boid_key = f"{cn.id or ''}|{cn.code or ''}"
                            cached_byboid = _get_cached_byboid(boid_key)
                            if cached_byboid:
                                raw_bo = json.loads(cached_byboid)
                            else:
                                raw_bo = http_get_json(cfg, byboid_path, {
                                    "boId": cn.id or "",
                                    "businessObjectCode": cn.code or ""
                                })
                                _set_cached_byboid(boid_key, json.dumps(raw_bo, ensure_ascii=False))
                            # 从子实体中找到主表（包含 "主表" 的）
                            details = collect_entity_details(raw_bo)
                            for detail in details:
                                detail_name = detail.get("billName", "") or ""
                                detail_uri = detail.get("uri", "") or ""
                                # 优先选择主表（包含 "主表" 的，且不包含其他后缀）
                                if "主表" in detail_name and detail_uri:
                                    # 进一步排除变体
                                    if not any(kw in detail_uri for kw in ["IBpmStep", "Dcs", "审批"]):
                                        target_uri = detail_uri
                                        logger.info(f"【v35.x】通过 getEntityListByBOId 找到主表: {detail_name} -> {detail_uri}")
                                        break
                            # 如果没找到包含"主表"的，取第一个不含变体后缀的
                            if not target_uri:
                                for detail in details:
                                    detail_uri = detail.get("uri", "") or ""
                                    if detail_uri and not any(kw in detail_uri for kw in ["IBpmStep", "Dcs", "审批"]):
                                        target_uri = detail_uri
                                        detail_name = detail.get("billName", "") or ""
                                        logger.info(f"【v35.x】通过 getEntityListByBOId 找到候选: {detail_name} -> {detail_uri}")
                                        break

                if target_uri:
                    _update_bill_info(bill_info, bn, uri=target_uri, occurrence=occurrence)
                    logger.info(f"【v17.x】补全 URI: {bn}[{occurrence}]（parent_hint={parent_hint}）→ {target_uri}")
                    # 【v16.x 新增】通过 FastLookup 补全 schema
                    if fast_lookup and fast_lookup.is_loaded:
                        fl_result = fast_lookup.get_by_uri(target_uri)
                        if fl_result and fl_result.schema:
                            _update_bill_info(bill_info, bn, schema=fl_result.schema, occurrence=occurrence)
                            logger.info(f"【v16.x】补全 schema: {bn}[{occurrence}] → {fl_result.schema}")
                else:
                    logger.warning(f"【v17.x】无法获取 {bn} 的 URI")
            except Exception as e:
                logger.warning(f"【v17.x】补全 URI 失败 [{bn}]: {e}")
        # 同步更新 uri_list
        new_uri_list = [item.get("uri", "") for item in bill_info]
        req["_query_uri_list"] = new_uri_list

    if len(parts_to_fetch) == 1:
        bill_info = cfg.get("_bill_info", [])
        ents, err, char_class_ids = build_entities_for_bill(cfg, parts_to_fetch[0], bill_info)
        if err:
            if isinstance(err, dict) and err.get("type") == "selection":
                return {"entities": [], "selection": err}, None
            return {"entities": []}, err
        # 合并已缓存的实体
        merged.extend(ents)
        # 【v10.6】补充特征表实体
        merged = _supplement_characteristic_tables(cfg, merged, char_class_ids)
        return {"entities": merged}, None

    max_b = _max_concurrent_bills(cfg)
    bill_results: Dict[str, Tuple[List[Dict[str, Any]], Optional[str], Dict[str, str]]] = {}
    bill_info = cfg.get("_bill_info", [])

    with ThreadPoolExecutor(max_workers=min(max_b, len(parts_to_fetch))) as executor:
        future_to_part = {
            executor.submit(build_entities_for_bill, cfg, p, bill_info): p for p in parts_to_fetch
        }
        for future in as_completed(future_to_part):
            part = future_to_part[future]
            try:
                ents, err, char_class_ids = future.result()
                if isinstance(err, dict) and err.get("type") == "selection":
                    return {"entities": [], "selection": err}, None
                bill_results[part] = (ents, err, char_class_ids)
            except Exception as e:
                import traceback
                logger.error(f"拉取单据失败 [{part}]: {e}")
                logger.error(f"详细错误: {traceback.format_exc()}")
                return {"entities": []}, f"拉取单据失败 [{part}]: {e}"

    failed_bills: List[str] = []
    not_found_bills: List[str] = []
    # 【v10.7 新增】累积所有特征表 classId 映射
    all_char_class_ids: Dict[str, str] = {}
    for p in parts_to_fetch:
        ents, err, char_class_ids = bill_results[p]
        if err:
            if isinstance(err, str) and "未找到" in err:
                not_found_bills.append(p)
            else:
                failed_bills.append(p)
        else:
            merged.extend(ents)
            if char_class_ids:
                all_char_class_ids.update(char_class_ids)

    # 汇总失败情况
    if not_found_bills or failed_bills:
        logger.warning("=== 拉取失败汇总 ===")
        for bill in not_found_bills:
            logger.warning(f"  ⚠ 未找到: {bill}（searchByName 返回空数据，请确认单据名称正确且已在 BIP 平台发布）")
        for bill in failed_bills:
            logger.warning(f"  ✗ 拉取失败: {bill}")
        if not merged:
            # 【v12.x 修复】所有单据都失败（merged=0），检查 session cache 是否已有数据
            if session_cache:
                # 【v19.x 修复】正确过滤 entity_info: 条目，并确保只添加列表类型的数据
                valid_ents = []
                for k, v in session_cache.items():
                    if k.startswith("entity_info:"):
                        continue  # 跳过 entity_info: 条目（这些是原始 API 响应）
                    if isinstance(v, list):
                        valid_ents.extend(v)
                cached_count = len(valid_ents)
                logger.info(f"【v12.x 修复】merged=0，从 session cache 恢复 {cached_count} 个实体")
                merged.extend(valid_ents)
            if not merged:
                first_err = bill_results.get(parts_to_fetch[0], (None, "未知错误", {}))[1]
                return {"entities": []}, first_err
        # 【v10.6】部分失败但有结果，补充特征表后继续
        merged = _supplement_characteristic_tables(cfg, merged, all_char_class_ids)
        return {"entities": merged}, None

    # 【v12.x 新增】无失败但 merged=0（全部快速返回空结果）时，从 session cache 恢复
    # 【v18.x 修复】过滤 entity_info: 条目，这些是原始 API 响应而非实体列表
    if not merged:
        if session_cache:
            # 过滤出真正的实体列表（排除 entity_info: 条目）
            valid_ents = []
            for k, v in session_cache.items():
                if k.startswith("entity_info:"):
                    continue
                if isinstance(v, list):
                    valid_ents.extend(v)
            cached_count = len(valid_ents)
            logger.info(f"【v12.x 修复】merged=0（并行快速返回），从 session cache 恢复 {cached_count} 个实体")
            merged.extend(valid_ents)

    logger.info(f"全部单据拉取完成，共获取 {len(merged)} 个实体")
    # 缓存统计：帮助诊断是否命中缓存
    redis_stats = get_cache_stats()
    # 【v6.0 新增】进程内对象缓存统计
    obj_cache_stats = _uri_object_cache.get_stats()
    logger.info(
        f"【缓存统计 v6.0】"
        f"进程内对象缓存: size={obj_cache_stats['size']}, "
        f"命中率={obj_cache_stats['hit_rate']} "
        f"(hit={obj_cache_stats['hit_count']}, miss={obj_cache_stats['miss_count']}) | "
        f"redis.enabled={redis_stats.get('enabled', False)} | "
        f"disk.enabled={_disk_cache_enabled}"
    )

    # 【v8.4 新增】保存 session 缓存
    # 【v16.x 修复】使用 uri 作为 key，避免 billName 重复问题
    # 【v18.x 修复】防御性检查：确保 ent 是 dict 类型
    # 【v39.x 修复】同时存储 raw queryByUri JSON 响应（包含完整 businessProperties）
    if _SESSION_CACHE_INDEX_AVAILABLE:
        try:
            # 更新 session cache，按 uri 分组
            for ent in merged:
                if not isinstance(ent, dict):
                    continue
                uri = (ent.get("uri") or "").strip().lower()
                if uri:
                    if uri not in session_cache:
                        session_cache[uri] = []
                    session_cache[uri].append(ent)
            # 保存到磁盘
            _save_session_cache(cfg, session_cache)
        except Exception as e:
            logger.warning(f"Session Cache 保存失败: {e}")

    # 【v10.6 新增】检测并补充特征表实体
    # 某些业务对象包含特征表（如 storeProRecordDefineCharacter），其字段（如 vcol1）需要被包含在元数据中
    merged = _supplement_characteristic_tables(cfg, merged, all_char_class_ids)

    return {"entities": merged}, None


def _construct_character_uri(entity_uri: str, field_name: str) -> Optional[str]:
    """
    【v10.6 新增】从主表 URI 和特征字段名构造特征表 URI

    例如:
      entity_uri: "st.storeprorecord.StoreProRecord"
      field_name: "storeProRecordDefineCharacter"
      -> "st.storeprorecord.StoreProRecordDefineCharacter"

    Args:
        entity_uri: 主表实体 URI
        field_name: 特征字段名（如 storeProRecordDefineCharacter）

    Returns:
        特征表 URI，如果不匹配模式则返回 None
    """
    if not entity_uri or not field_name:
        return None

    # 特征字段名模式
    patterns = ["DefineCharacter", "CharacterDefine", "Characteristics"]
    for pattern in patterns:
        if pattern in field_name:
            # 去掉 Pattern 部分得到表名部分
            # storeProRecordDefineCharacter -> StoreProRecord
            prefix = field_name.replace(pattern, "")
            # 首字母小写 -> 大写
            if prefix and prefix[0].islower():
                prefix = prefix[0].upper() + prefix[1:]
            # 构造 URI: {domain}.{prefix}{pattern}VO
            # 例如: st.storeprorecord.StoreProRecord + DefineCharacter -> st.storeprorecord.StoreProRecordDefineCharacter
            parts = entity_uri.rsplit(".", 1)
            if len(parts) == 2:
                return f"{parts[0]}.{prefix}{pattern}"
            return None
    return None


def _extract_character_class_ids(raw_entity_data: Any) -> Dict[str, str]:
    """
    【v10.7 新增】从主表完整数据中提取特征字段的 classId 映射

    Args:
        raw_entity_data: getEntityInfoByBOIdAndEntityId 返回的完整数据

    Returns:
        Dict[str, str]: key=特征表URI, value=classId (entityId)
    """
    char_class_ids: Dict[str, str] = {}
    if not isinstance(raw_entity_data, dict):
        return char_class_ids

    _CHARACTER_FIELD_PATTERNS = ["DefineCharacter", "CharacterDefine", "Characteristics"]

    data_node = raw_entity_data.get("data", {})
    if isinstance(data_node, dict):
        inner = data_node.get("data", {})
        if isinstance(inner, dict):
            data_node = inner

    logger.info(f"[v10.7 DEBUG] data_node keys: {list(data_node.keys()) if isinstance(data_node, dict) else 'not a dict'}")

    # 特征字段在 businessProperties 中
    biz_props = data_node.get("businessProperties", [])
    if not isinstance(biz_props, list):
        biz_props = []

    logger.info(f"[v10.7 DEBUG] 检查特征表 classId，共 {len(biz_props)} 个业务属性")
    for prop in biz_props:
        if not isinstance(prop, dict):
            continue
        col_name = prop.get("fieldName", "") or prop.get("name", "") or ""

        # 检查是否是特征字段引用（名称匹配模式）
        is_char_field = False
        for pattern in _CHARACTER_FIELD_PATTERNS:
            if pattern in col_name:
                is_char_field = True
                break

        if not is_char_field:
            continue

        # 获取 classId（entityId）- 在 dataType.id 中
        data_type = prop.get("dataType", {})
        if isinstance(data_type, dict):
            class_id = data_type.get("id", "") or ""
            type_uri = data_type.get("uri", "") or ""
            if class_id and type_uri:
                char_class_ids[type_uri] = class_id
                logger.info(f"[v10.7] 发现特征字段: {col_name} -> classId={class_id}, uri={type_uri}")

    logger.info(f"[v10.7 DEBUG] 找到 {len(char_class_ids)} 个特征表 classId: {char_class_ids}")
    return char_class_ids


def _supplement_characteristic_tables(
    cfg: dict, entities: List[Dict[str, Any]], char_class_ids: Optional[Dict[str, str]] = None
) -> List[Dict[str, Any]]:
    """
    【v10.6 新增，v10.7 修复】检测并补充特征表实体

    检测每个实体的字段中是否有指向特征表的引用（如 *DefineCharacter, *Characteristics），
    如果有则查询特征表实体并添加到输出列表。

    Args:
        cfg: 配置字典
        entities: 已获取的实体列表
        char_class_ids: 【v10.7 新增】特征表 URI -> classId 的映射，用于调用 getEntityInfoByBOIdAndEntityId

    Returns:
        补充特征表后的实体列表
    """
    logger.info("[v10.7] _supplement_characteristic_tables 被调用，当前实体数: %d", len(entities))
    logger.info("[v10.7] 传入的特征表 classId 映射: %s", char_class_ids or {})

    # 特征表字段的常见模式（字段名匹配这些模式说明指向特征表）
    _CHARACTER_FIELD_PATTERNS = [
        "DefineCharacter",
        "CharacterDefine",
        "Characteristics",
    ]

    # 已存在的实体 URI 列表（用于去重）
    # 【v18.x 修复】防御性检查：确保 e 是 dict 类型
    existing_uris: Set[str] = set()
    for e in entities:
        if isinstance(e, dict) and e.get("uri"):
            existing_uris.add((e.get("uri") or "").lower())

    # 需要补充的特征表 URI 列表
    char_uris_to_fetch: Set[str] = set()

    # 检测字段中的特征表引用
    # 【v18.x 修复】防御性检查：确保 entity 是 dict 类型
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        uri = entity.get("uri") or ""
        attrs = entity.get("attributes") or []

        for attr in attrs:
            if not isinstance(attr, dict):
                continue
            col_name = attr.get("dbColumnName") or ""
            attr_type = attr.get("type") or ""

            # 检查是否是特征字段引用（Long 类型 + 名称匹配模式）
            if attr_type == "Long":
                for pattern in _CHARACTER_FIELD_PATTERNS:
                    if pattern in col_name:
                        # 构造特征表 URI
                        char_uri = _construct_character_uri(uri, col_name)
                        if char_uri and char_uri.lower() not in existing_uris:
                            char_uris_to_fetch.add(char_uri)
                        break

    logger.info("[v10.7] 检测到 %d 个特征表 URI 需要补充", len(char_uris_to_fetch))
    if not char_uris_to_fetch:
        return entities

    logger.info(f"[v10.7] 检测到 {len(char_uris_to_fetch)} 个特征表需要补充: {char_uris_to_fetch}")

    # 查询特征表实体
    api = cfg.get("api") or {}

    new_entities: List[Dict[str, Any]] = []
    for uri in char_uris_to_fetch:
        try:
            # 【v10.7 修复】优先使用 classId 调用 getEntityInfoByBOIdAndEntityId 获取完整数据
            raw_json: Optional[str] = None
            if char_class_ids and uri in char_class_ids:
                class_id = char_class_ids[uri]
                logger.info(f"[v10.7] 特征表 {uri} 有 classId={class_id}，调用 getEntityInfoByBOIdAndEntityId")
                cached_ent = _get_cached_entityid(class_id)
                if cached_ent:
                    logger.debug(f"[CACHE] entityId 缓存命中: {class_id}")
                    raw_json = cached_ent
                else:
                    ent_path = api.get("metadata_entityid", "")
                    logger.info(f"[API] getEntityInfoByBOIdAndEntityId 请求: entityId={class_id}, uri={uri}")
                    raw_ent = http_get_json(
                        cfg,
                        ent_path,
                        {
                            "entityId": class_id,
                            "uri": uri,
                            "boId": "",
                            "businessObjectCode": "",
                        },
                    )
                    raw_json = json.dumps(raw_ent, ensure_ascii=False)
                    _set_cached_entityid(class_id, raw_json)
                    logger.info(f"[v10.7] 特征表 {uri} getEntityInfoByBOIdAndEntityId 数据已缓存")

            # 如果没有 classId 或调用失败，fallback 到 queryByUri
            if not raw_json:
                logger.warning(f"[v10.7] 特征表 {uri} 无 classId，回退到 queryByUri")
                fetch_uri_json: Callable[[str], str] = lambda u: _query_by_uri_cached(cfg, u)
                raw_json = fetch_uri_json(uri)

            if not raw_json:
                logger.warning(f"[v10.7] 特征表 URI 查询失败: {uri}")
                continue

            db_obj = json.loads(raw_json)
            code_val = _text(db_obj, "code") or _text(db_obj, "resultCode")
            if code_val != "200":
                logger.warning(f"[v10.7] 特征表查询返回错误码: {code_val}, URI={uri}")
                continue

            # 【v10.8 修复】从 businessProperties 中找特征字段(vcol/lcol/scol)的 allTables
            correct_table_name = ""
            data_node = db_obj.get("data", {})
            if isinstance(data_node, dict):
                inner = data_node.get("data", {})
                if isinstance(inner, dict):
                    biz_props = inner.get("businessProperties", [])
                    if isinstance(biz_props, list) and biz_props:
                        # 优先取特征字段(vcol/lcol/scol)的 allTables，其次取第一个字段的
                        for prop in biz_props:
                            if isinstance(prop, dict):
                                fn = prop.get("fieldName", "") or ""
                                at = prop.get("allTables", [])
                                if at and at[0]:
                                    # 优先取特征字段
                                    if fn.startswith(("vcol", "lcol", "scol", "ncol", "dcol", "tcol")):
                                        correct_table_name = at[0]
                                        logger.info(f"[v10.8] 特征表 {uri} 特征字段 {fn} allTables={at}，使用 {correct_table_name}")
                                        break
                        # 如果没找到特征字段，取第一个有 allTables 的字段
                        if not correct_table_name:
                            for prop in biz_props:
                                if isinstance(prop, dict):
                                    at = prop.get("allTables", [])
                                    if at and at[0]:
                                        correct_table_name = at[0]
                                        logger.info(f"[v10.8] 特征表 {uri} 首个字段 allTables={at}，使用 {correct_table_name}")
                                        break
            if not correct_table_name:
                logger.warning(f"[v10.8] 特征表 {uri} 无法获取 allTables")
                continue

            # 【v10.7 修复】直接从 getEntityInfoByBOIdAndEntityId 返回数据构建特征表实体
            # 不依赖 parse() 函数，因为它无法正确解析这个 API 返回的数据格式
            data_node = db_obj.get("data", {})
            if isinstance(data_node, dict):
                inner = data_node.get("data", {})
                if isinstance(inner, dict):
                    entity_name = inner.get("name", "") or inner.get("displayName", "") or ""
                    entity_domain = inner.get("domain", "") or ""
                    entity_schema = inner.get("schema", "") or ""
                    biz_props = inner.get("businessProperties", [])

                    # 构建特征表实体
                    char_entity: Dict[str, Any] = {
                        "tableName": correct_table_name,
                        "billName": f"特征表_{correct_table_name}",
                        "domain": entity_domain,
                        "uri": uri,
                        "schema": entity_schema or entity_domain,
                        "isCharacterTable": True,
                    }

                    # 提取属性
                    attrs_out: List[Dict[str, Any]] = []
                    if isinstance(biz_props, list):
                        for prop in biz_props:
                            if not isinstance(prop, dict):
                                continue
                            field_name = prop.get("fieldName", "") or prop.get("name", "") or ""
                            display_name = prop.get("displayName", "") or field_name
                            prop_type = prop.get("dataType", {})
                            if isinstance(prop_type, dict):
                                type_name = prop_type.get("name", "") or "String"
                            else:
                                type_name = "String"

                            am = {
                                "displayName": display_name,
                                "dbColumnName": field_name,
                                "type": type_name,
                                "tableName": correct_table_name,
                            }
                            # 标记特征字段
                            if field_name.startswith("vcol") or field_name.startswith("lcol") or field_name.startswith("scol"):
                                am["isCharacterField"] = True
                            attrs_out.append(am)

                    char_entity["attributes"] = attrs_out
                    new_entities.append(char_entity)
                    logger.info(
                        f"[v10.7] 特征表已补充: {uri} → {correct_table_name}, "
                        f"字段数={len(attrs_out)}"
                    )
                else:
                    logger.warning(f"[v10.7] 特征表 {uri} 无内层 data 对象")
            else:
                logger.warning(f"[v10.7] 特征表 {uri} 无 data 对象")

        except Exception as e:
            logger.warning(f"[v10.6] 处理特征表失败: {uri}, 错误: {e}")

    # 合并并返回
    if new_entities:
        logger.info(f"[v10.6] 特征表补充完成，新增 {len(new_entities)} 个实体")
        return entities + new_entities
    return entities


def _get_tenant_id(cfg: dict) -> Optional[str]:
    """从配置或环境变量获取租户ID"""
    # 优先从环境变量获取
    tenant_id = os.environ.get("YONBIP_TENANT_ID", "").strip()
    if tenant_id:
        return tenant_id
    # 从配置中获取
    db = cfg.get("database") or {}
    queries = db.get("queries") or {}
    elastic = queries.get("elastic_field_check") or {}
    tenant_id = str(elastic.get("ytenant_id", "")).strip()
    if tenant_id:
        return tenant_id
    return None


def _print_main_entities_summary(entities: List[Dict[str, Any]]) -> None:
    """
    【v8.5 新增】输出主子实体清单到控制台

    根据 billName 和 URI 识别主实体和子实体：
    - 主实体：billName 包含"主表"/"主"/"表头"，或 URI 以 BillVO 结尾（但不是 BVO/SettleInfoVO）
    - 子实体：billName 包含"表体"、"明细"、"结算"，或 URI 包含 BVO/SettleInfo
    """
    # 主实体识别规则
    main_keywords = ["主表", "主", "表头", "BillVO"]
    # 子实体识别规则
    sub_keywords = ["表体", "明细", "结算", "BVO", "SettleInfo"]

    def get_entity_type(entity: Dict[str, Any]) -> str:
        bill_name = entity.get("billName", "")
        uri = entity.get("uri", "")

        # 子实体判断（优先）
        for kw in sub_keywords:
            if kw in bill_name or kw in uri:
                return "sub"

        # 主实体判断
        for kw in main_keywords:
            if kw in bill_name:
                return "main"

        # 通过 URI 模式判断：以 BillVO 结尾但不是 SettleInfo/BVO 的是主实体
        if uri.endswith("BillVO") and "SettleInfo" not in uri and "BVO" not in uri:
            return "main"

        return entity.get("entityType", "")

    main_entities = [e for e in entities if get_entity_type(e) == "main"]
    sub_entities = [e for e in entities if get_entity_type(e) == "sub"]
    other_entities = [e for e in entities if get_entity_type(e) not in ("main", "sub") and e.get("entityType") not in ("main", "sub")]

    print("\n" + "=" * 60, file=sys.stderr)
    print("【阶段一输出】主子实体清单", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    if main_entities:
        print("\n主实体（主表）:", file=sys.stderr)
        for e in main_entities:
            bill_name = e.get("billName", "")
            table_name = e.get("tableName", "")
            schema = e.get("schema", "")
            uri = e.get("uri", "")
            print(f"  - {bill_name}", file=sys.stderr)
            print(f"    表名: {schema}.{table_name}", file=sys.stderr)
            print(f"    URI: {uri}", file=sys.stderr)

    if sub_entities:
        print(f"\n子实体（共 {len(sub_entities)} 个）:", file=sys.stderr)
        for e in sub_entities:
            bill_name = e.get("billName", "")
            table_name = e.get("tableName", "")
            schema = e.get("schema", "")
            print(f"  - {bill_name} [{schema}.{table_name}]", file=sys.stderr)

    if other_entities:
        print(f"\n其他实体（共 {len(other_entities)} 个）:", file=sys.stderr)
        for e in other_entities:
            bill_name = e.get("billName", "")
            table_name = e.get("tableName", "")
            schema = e.get("schema", "")
            print(f"  - {bill_name} [{schema}.{table_name}]", file=sys.stderr)

    print("\n" + "=" * 60, file=sys.stderr)
    print(f"合计: {len(main_entities)} 个主实体 + {len(sub_entities)} 个子实体 + {len(other_entities)} 个其他 = {len(entities)} 个实体", file=sys.stderr)
    print("=" * 60 + "\n", file=sys.stderr)


def write_outputs(
    cfg: dict,
    payload: Dict[str, Any],
    skip_merge: bool = False,
    req: Optional[Dict[str, Any]] = None
) -> None:
    """
    将元数据写入输出文件

    Args:
        cfg: 配置
        payload: 元数据
        skip_merge: 是否跳过旧索引合并（用于 queryUri 模式）
        req: 请求配置（用于判断是否是参照实体批量查询）
    """
    out_cfg = cfg.get("output") or {}
    paths = cfg.get("paths") or {}
    ws_base = workspace_base(cfg)
    logger.info(f"Workspace (outputs): {ws_base}")

    out_dir = resolve_workspace_path(paths.get("output_dir", "output"), cfg)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = dict(payload)
    if out_cfg.get("strip_attribute_names", True):
        data = filter_strip_attribute_names(data)

    if out_cfg.get("write_entities_json", True):
        p = out_dir / str(out_cfg.get("entities_json_filename", "entities.json"))

        if skip_merge:
            # 【v8.4】skip_merge 模式：直接写入新数据，不合并旧索引
            # 用于 queryUri 模式，避免加载旧的 152 个实体导致数量膨胀
            def should_filter_entity(ent: dict) -> bool:
                table_name = ent.get("tableName", "")
                bill_name = ent.get("billName", "")
                relation_type = ent.get("relation_type")  # 【v36.x 新增】主表/平行表/扩展表标识

                # 【v44.x 新增】优先保护：Excel billObjects 中明确列出的 tableName 必须保留
                bill_object_table_names = cfg.get("_bill_object_table_names", set())
                if bill_object_table_names and table_name.lower() in bill_object_table_names:
                    logger.info(f"【v44.x】保护 Excel billObjects 中的 tableName: {table_name}")
                    return False  # 保留

                if "_dcs" in table_name:
                    return True
                if "ibpmcurrentauditor" in table_name or "ibpmstep" in table_name:
                    return True
                if "审批" in bill_name:
                    return True
                # 【v36.x 新增】保护所有特征表和特性表（保留）
                if "_characteristics" in table_name or "_character_define" in table_name or "_feature" in table_name:
                    return False  # 保留所有特征表和特性表
                # 【v43.x 新增】保护所有主子表关系中的子表（保留）
                if relation_type == "child" or relation_type == "compositionChild":
                    return False  # 保留所有子表
                # 【v43.x 新增】如果 billName 包含"子表"或"明细"，保留（兜底保护）
                if "子表" in bill_name or "明细" in bill_name:
                    return False  # 保留子表和明细表
                # 【v36.x 新增】过滤扩展表
                if relation_type is None and "_parallel" not in table_name:
                    EXTENSION_TABLES = {"ExternalSystem", "FiEventAccInfo", "FiInfoRecord", "QiMen",
                                       "QiMenMultiplePostback", "FiInfo", "FiEventAcc"}
                    attrs = ent.get("attributes", [])
                    if table_name in EXTENSION_TABLES:
                        return True  # 黑名单表
                    if not attrs or len(attrs) == 0:
                        return True  # 空表
                return False

            new_entities = data.get("entities", [])
            filtered_entities = [ent for ent in new_entities if not should_filter_entity(ent)]
            filtered_count = len(new_entities) - len(filtered_entities)
            if filtered_count > 0:
                logger.info(f"写入时过滤非主子表实体: {filtered_count} 个")
            data = dict(data)
            data["entities"] = filtered_entities
            logger.info(f"直接写入新实体: {len(filtered_entities)} 个（跳过旧索引合并）")
            # 【v43.x 修复】skip_merge 模式下也要调用 _enrich_business_properties
            # 因为 queryByUri 返回的数据没有 businessProperties，需要通过三个接口获取
            _enrich_business_properties(filtered_entities, cfg)
            # 【v41.x 新增】对 skip_merge 路径也处理多选参照字段
            # 【修复】传入 cfg 以便过滤 businessProperties
            _merge_business_properties_into_attributes(filtered_entities, cfg)
            for ent in filtered_entities:
                _expand_character_fields(ent)
        else:
            # 【v7.0 优化】先确保索引已加载，再合并，再写入
            # 索引加载路径（按速度）：
            #   1. 索引已全量加载到内存（iterate_all 直接读内存）→ <0.1ms
            #   2. SQLite 有数据（load_from_json 加载到内存）→ <2ms
            #   3. JSON 文件存在（load_from_json 从文件加载并建 SQLite 索引）→ 50ms+
            # 关键修复：旧代码在此处直接调用 get_count() 判断是否有数据，但此时索引尚未 load，
            #   导致走 json.loads() 降级路径（即使文件存在）。新逻辑先 load，再用内存索引合并。
            idx = _get_entities_index(cfg)
            idx_loaded_count = 0
            if idx and p.exists():
                idx_loaded_count = idx.load_from_json(str(p))
                logger.info(f"索引加载完成: {idx_loaded_count} 个实体")

            # 从内存索引合并（不走 json.loads()）
            # 复用已加载的 _memory_index（通过 iterate_all 触发内存索引路径）
            existing_entities: List[Dict[str, Any]] = []
            # 【v8.5 修复】参照实体批量查询时不加载旧索引，避免实体数量膨胀
            # 通过检查 skip_merge 标志来判断是否是参照实体批量查询
            is_ref_batch_query = bool(req.get("_query_uri_list"))
            if is_ref_batch_query:
                # 参照实体批量查询：跳过加载旧索引，直接使用新实体
                # 注意：这意味着参照实体不会与主表实体合并，由调用方手动处理
                logger.info("参照实体批量查询：跳过旧索引合并")
            elif idx and idx.get_count() > 0:
                existing_entities = idx.iterate_all()
                logger.info(f"从内存索引合并已有实体: {len(existing_entities)} 个")

            # 合并 entities 列表（去重：按 uri + tableName 唯一性判断）
            # 【v10.1 修复】当存在相同 uri|tableName 时，用新实体替换旧实体（保留最新字段）
            new_entities = data.get("entities", [])
            if new_entities or existing_entities:
                seen_keys: Set[str] = set()
                merged_entities: List[Dict[str, Any]] = []
                # 先追加新的（替换已存在的）
                # 【v8.3 过滤规则】过滤掉 _dcs、ibpm 等非主子表实体
                def should_filter_entity_merge(ent: dict) -> bool:
                    table_name = ent.get("tableName", "")
                    bill_name = ent.get("billName", "")
                    relation_type = ent.get("relation_type")  # 【v36.x 新增】主表/平行表/扩展表标识
                    uri = ent.get("uri", "")
                    reason = ""
                    if "_dcs" in table_name:
                        reason = f"_dcs in table_name: {table_name}"
                    elif "ibpmcurrentauditor" in table_name or "ibpmstep" in table_name:
                        reason = f"ibpm in table_name: {table_name}"
                    elif "审批" in bill_name:
                        reason = f"审批 in bill_name: {bill_name}"
                    # 【v36.x 新增】保护所有特征表和特性表（保留）
                    elif "_characteristics" in table_name or "_character_define" in table_name or "_feature" in table_name:
                        return False  # 保留所有特征表和特性表
                    # 【v43.x 新增】保护所有主子表关系中的子表（保留）
                    elif relation_type == "child" or relation_type == "compositionChild":
                        return False  # 保留所有子表
                    # 【v43.x 新增】如果 billName 包含"子表"或"明细"，保留（兜底保护）
                    elif "子表" in bill_name or "明细" in bill_name:
                        return False  # 保留子表和明细表
                    # 【v36.x 新增】过滤扩展表
                    elif relation_type is None and "_parallel" not in table_name:
                        EXTENSION_TABLES = {"ExternalSystem", "FiEventAccInfo", "FiInfoRecord", "QiMen",
                                           "QiMenMultiplePostback", "FiInfo", "FiEventAcc"}
                        attrs = ent.get("attributes", [])
                        if table_name in EXTENSION_TABLES:
                            reason = f"扩展表: {table_name}"
                        elif not attrs or len(attrs) == 0:
                            reason = f"空表: {table_name}"
                    if reason:
                        logger.debug(f"[FILTER] 过滤实体: {table_name} ({bill_name}), 原因: {reason}")
                        return True
                    return False

                added_count = 0
                skipped_count = 0
                filtered_count = 0
                # 【v18.x 修复】防御性检查：确保 ent 是 dict 类型
                for ent in new_entities:
                    if not isinstance(ent, dict):
                        continue
                    key = f"{ent.get('uri', '')}|{ent.get('tableName', '')}"
                    # 【v10.1】先过滤，再判断是否替换
                    if should_filter_entity_merge(ent):
                        filtered_count += 1
                        continue
                    # 【v10.1 修复】如果 key 已存在，移除旧实体（用新实体替换）
                    if key in seen_keys:
                        merged_entities = [e for e in merged_entities
                                         if not isinstance(e, dict) or f"{e.get('uri', '')}|{e.get('tableName', '')}" != key]
                        skipped_count += 1
                    merged_entities.append(ent)
                    seen_keys.add(key)
                    added_count += 1
                # 再加入旧实体中没有被新实体替换的
                for ent in existing_entities:
                    key = f"{ent.get('uri', '')}|{ent.get('tableName', '')}"
                    if key and key not in seen_keys:
                        merged_entities.append(ent)
                        seen_keys.add(key)
                if filtered_count > 0:
                    logger.info(f"合并时过滤非主子表实体: {filtered_count} 个")
                if skipped_count > 0:
                    logger.info(f"【v10.1】合并时替换旧实体: {skipped_count} 个（保留最新字段）")
                logger.info(f"合并 entities: 新增 {added_count} 个，替换 {skipped_count} 个，合计 {len(merged_entities)} 个")
                data = dict(data)
                data["entities"] = merged_entities

        # 【v11.0 新增】补充 businessProperties
        # 从 getEntityInfoByBOIdAndEntityId 的磁盘缓存中提取 businessProperties
        # 解决：主表的 businessProperties 在 queryByUri 中为空，导致特征表 allTables 解析问题
        # 【v33.x 修复】传入 cfg 以便从 session 缓存通过 URI 查找
        _enrich_business_properties(data.get("entities", []), cfg)

        # 【v41.x 新增】整合 businessProperties 到 attributes 并展开多选参照字段
        # 1. 将 businessProperties 整合到 attributes（移除 businessProperties）
        # 2. 展开 characterFields（多选参照字段）为独立的 attributes
        # 【修复】传入 cfg 以便过滤 businessProperties
        entities_for_postprocess = data.get("entities", [])
        _merge_business_properties_into_attributes(entities_for_postprocess, cfg)
        for ent in entities_for_postprocess:
            _expand_character_fields(ent)
        logger.info(f"[POSTPROCESS] 多选参照字段展开完成，entities={len(entities_for_postprocess)}")

        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"写入: {p}")

        # 【v8.5】输出主子实体清单到控制台
        _print_main_entities_summary(data.get("entities", []))

        # 【v8.5】写入后重置索引，下次查询会重新从磁盘加载
        _reset_entities_index()

    if out_cfg.get("write_bundle_md", True):
        guide = resolve_skill_path(
            paths.get("sql_guide_md", "reference/旗舰版通用_后端_报表_SQL生成.md")
        )
        body = ""
        if guide.exists():
            body = guide.read_text(encoding="utf-8")
        p = out_dir / str(out_cfg.get("bundle_md_filename", "report_sql_context.md"))
        header = "业务对象属性信息如下:\n\n```json\n"
        footer = (
            "\n```\n\n实体模型字段说明: schema为schema，tableName 为表名，"
            "billName 为单据名称，domain 为领域；attributes 为属性列表，"
            "每项含 displayName(显示名)、dbColumnName(数据库列名)、type(数据类型)、"
            "enums(枚举值列表)。referenceStructure 为属性字段的引用元数据结构信息,,"
            "referenceStructure中primarykey是引用的主键\n"
        )
        # 租户信息（仅提供租户ID供参考，不强制要求添加到WHERE条件
        tenant_id = _get_tenant_id(cfg)
        tenant_section = ""
        if tenant_id:
            tenant_section = (
                f"\n## 租户信息\n\n"
                f"当前租户ID (ytenant_id): `{tenant_id}`\n\n"
                f"> **说明**: 此租户ID仅供参考，**仅当用户明确要求按租户过滤时**才需要在WHERE条件中使用。\n"
                f"> 禁止猜测添加此租户ID过滤条件。\n\n"
            )
        # 数据库校验环境信息
        db_cfg_out = cfg.get("database") or {}
        db_section = ""
        if str(db_cfg_out.get("enabled", "false")).lower() in ("true", "1", "yes"):
            db_driver = db_cfg_out.get("driver", "mysql")
            db_host = db_cfg_out.get("host", "")
            db_port = db_cfg_out.get("port", "")
            db_name = db_cfg_out.get("database", "")
            db_section = (
                f"\n## 数据库校验环境\n\n"
                f"- 数据库类型: `{db_driver}`\n"
                f"- 主机: `{db_host}`\n"
                f"- 端口: `{db_port}`\n"
                f"- 数据库名: `{db_name}`\n\n"
                f"> 生成的SQL将在此数据库上执行校验，请确保SQL语法兼容。\n\n"
            )
        # Windows 记事本等默认用 ANSI 打开无 BOM 的 UTF-8 易误判为系统编码
        md_enc = "utf-8-sig" if sys.platform == "win32" else "utf-8"
        p.write_text(
            header + json.dumps(data, ensure_ascii=False, indent=2) + footer + tenant_section + db_section + body,
            encoding=md_enc,
        )
        logger.info(f"写入: {p}")


def _apply_workspace_cli_overrides(cfg: dict, args: argparse.Namespace) -> None:
    """应用命令行的工作空间覆盖"""
    wr = getattr(args, "workspace_root", None)
    if wr is not None and str(wr).strip():
        cfg.setdefault("paths", {})["workspace_root"] = str(wr).strip()


def _apply_request_cli_overrides(cfg: dict, args: argparse.Namespace) -> None:
    """将命令行显式传入的项合并进 cfg['request']，覆盖 config.yaml 同名字段。"""
    req = cfg.setdefault("request", {})
    if getattr(args, "allbillname", None) is not None:
        req["allbillname"] = args.allbillname
    if getattr(args, "lookup_json", None) is not None:
        cfg.setdefault("paths", {})["metadata_lookup_json"] = args.lookup_json
    if getattr(args, "is_include_sub", None) is not None:
        logger.info("isIncludeSub 参数已废弃，固定为 N（不查询子表）")
    if getattr(args, "doc_fields", None) is not None:
        req["docFields"] = args.doc_fields
    if getattr(args, "excel_file", None) is not None:
        req["excelFile"] = str(args.excel_file)
    if getattr(args, "excel_sheet_index", None) is not None:
        req["excelSheetIndex"] = args.excel_sheet_index
    if getattr(args, "is_sql", None) is not None:
        req["is_sql"] = args.is_sql
    if getattr(args, "is_desc_field", None) is not None:
        req["isDescField"] = args.is_desc_field
    if getattr(args, "request_text", None) is not None:
        req["requestText"] = args.request_text
    if getattr(args, "table_template", None) is not None:
        req["tableTemplate"] = args.table_template
    if getattr(args, "selected_bo_code", None) is not None:
        req["_selected_bo_code"] = args.selected_bo_code
    if getattr(args, "selected_bo_id", None) is not None:
        req["_selected_bo_id"] = args.selected_bo_id
    if getattr(args, "auto_select_first", None) is not None:
        req["_auto_select_first"] = args.auto_select_first
    if getattr(args, "query_uri", None) is not None:
        req["queryUri"] = args.query_uri


def main() -> int:
    configure_stdio_utf8()
    setup_logging("fetch_metadata")

    ap = argparse.ArgumentParser(
        description="拉取旗舰版业务对象元数据；request.* 可由话术解析后通过下列参数注入（覆盖 config.yaml）。"
    )
    ap.add_argument("--config", default=str(SKILL_DIR / "config.yaml"))
    ap.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file (default: <config_dir>/.env)",
    )
    ap.add_argument(
        "--workspace-root",
        default=None,
        metavar="DIR",
        help="覆盖 paths.workspace_root：元数据输出 output_dir 相对此目录（默认自动推断项目根）",
    )
    ap.add_argument(
        "--run-db-check",
        action="store_true",
        help="拉取完成后运行 database.queries.elastic_field_check",
    )
    ap.add_argument(
        "--validate",
        action="store_true",
        help="仅验证配置，不拉取数据",
    )
    ap.add_argument(
        "--allbillname",
        default=None,
        help="覆盖 request.allbillname（多个单据用英文逗号分隔）",
    )
    ap.add_argument(
        "--is-include-sub",
        "--isIncludeSub",
        dest="is_include_sub",
        default=None,
        metavar="Y|N",
        help="[已废弃] isIncludeSub 固定为 N，不再查询子表",
    )
    ap.add_argument(
        "--excel-file",
        "-e",
        dest="excel_file",
        default=None,
        type=Path,
        metavar="PATH",
        help="Excel 报表模板路径（自动提取 docFields 和 billNames，按需获取参照）",
    )
    ap.add_argument(
        "--excel-sheet-index",
        dest="excel_sheet_index",
        default=0,
        type=int,
        metavar="INDEX",
        help="Excel 工作表索引（配合 --excel-file 使用，默认 0）",
    )
    ap.add_argument(
        "--excel-parsed",
        dest="excel_parsed",
        default=None,
        type=Path,
        metavar="PATH",
        help="【v16.x】读取预解析的 excel_parsed.json（v2.0 格式），自动提取 billNames 和 uri，"
             "支持多 sheet。优先级高于 --excel-file。",
    )
    ap.add_argument(
        "--doc-fields",
        "--docFields",
        dest="doc_fields",
        default=None,
        metavar="CSV",
        help="覆盖 request.docFields：仅保留这些 displayName，逗号分隔；空字符串表示不过滤",
    )
    ap.add_argument(
        "--is-sql",
        dest="is_sql",
        default=None,
        metavar="Y|N",
        help="覆盖 request.is_sql（config 中字段名）：需要参照 referenceStructure 时为 Y",
    )
    ap.add_argument(
        "--is-desc-field",
        "--isDescField",
        dest="is_desc_field",
        default=None,
        metavar="Y|N",
        help="覆盖 request.isDescField：为 N 时忽略 docFields",
    )
    ap.add_argument(
        "--request-text",
        dest="request_text",
        default=None,
        metavar="TEXT",
        help="【v7.0 新增】自然语言请求文本，用于自动提取字段名并做语义匹配过滤元数据",
    )
    ap.add_argument(
        "--table-template",
        "--tableTemplate",
        dest="table_template",
        default=None,
        metavar="STR",
        help="覆盖 request.tableTemplate：参照属性额外保留的 displayName 匹配子串",
    )
    ap.add_argument(
        "--selected-bo-code",
        dest="selected_bo_code",
        default=None,
        metavar="CODE",
        help="指定已选择的业务对象编码（用于交互式选择后继续执行）",
    )
    ap.add_argument(
        "--selected-bo-id",
        dest="selected_bo_id",
        default=None,
        metavar="ID",
        help="指定已选择的业务对象ID（用于交互式选择后继续执行）",
    )
    ap.add_argument(
        "--auto-select-first",
        dest="auto_select_first",
        action="store_true",
        default=None,
        help="【自动化】当 searchByName 返回多个匹配时，自动选择第一个并继续执行，不退出等待用户选择",
    )
    ap.add_argument(
        "--query-uri",
        "--queryUri",
        dest="query_uri",
        default=None,
        metavar="URI",
        help="覆盖 request.queryUri：本地快速索引多义时选定元数据实体 uri；也可单独与空 allbillname 搭配直查",
    )
    ap.add_argument(
        "--lookup-json",
        "--lookupJson",
        dest="lookup_json",
        default=None,
        metavar="PATH",
        help="指定 metadata_lookup.json 路径（用于 FastLookup 精确匹配业务对象名称）",
    )
    ap.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="输出详细日志",
    )

    args = ap.parse_args()

    # 设置详细日志
    if args.verbose:
        import logging
        logging.getLogger("fetch_metadata").setLevel(logging.DEBUG)

    # 加载配置（支持环境变量）
    cfg_path = Path(args.config).expanduser().resolve()
    if not cfg_path.exists():
        print(f"错误: 配置文件不存在: {cfg_path}", file=sys.stderr)
        return ExitCode.CONFIG_ERROR

    # 显式加载 .env 文件
    if args.env_file:
        env_path = Path(args.env_file).expanduser().resolve()
    else:
        env_path = cfg_path.parent / ".env"
    load_dotenv(env_path)
    if env_path.exists():
        logger.info(f"已加载环境配置: {env_path}")
    else:
        logger.warning(f"未找到 .env 文件: {env_path}，请确认文件路径是否正确")

    try:
        cfg = resolve_config(cfg_path)
    except Exception as e:
        print(f"错误: 配置加载失败: {e}", file=sys.stderr)
        return ExitCode.CONFIG_ERROR

    # 【v7.0 新增】初始化性能配置（统一并发配置管理）
    _init_performance_config(cfg)

    # 初始化磁盘持久化缓存（从配置读取参数）
    _init_disk_cache(cfg)

    # 初始化 Redis 缓存（从配置读取参数）
    init_redis_cache(cfg)
    if is_redis_available():
        logger.info("Redis 缓存已启用（跨进程共享）")
    else:
        logger.debug("Redis 缓存未启用，将跳过")

    # 先合并命令行覆盖再校验（与 ultimate_metadata_query 一致，避免仅依赖 --allbillname 仍报 yaml 空值）
    _apply_workspace_cli_overrides(cfg, args)
    _apply_request_cli_overrides(cfg, args)

    # 【v16.x】处理 --excel-parsed 参数（优先级高于 --excel-file）
    if args.excel_parsed:
        excel_parsed_path = Path(args.excel_parsed).expanduser().resolve()
        try:
            load_excel_parsed_json(cfg, excel_parsed_path)
        except FileNotFoundError as e:
            print(f"错误: {e}", file=sys.stderr)
            return ExitCode.CONFIG_ERROR
        except Exception as e:
            print(f"错误: 读取 excel_parsed.json 失败: {e}", file=sys.stderr)
            return ExitCode.CONFIG_ERROR

    # 【v34.x】处理 --excel-file 参数，在验证前解析 Excel
    if args.excel_file and not args.excel_parsed:
        excel_file_path = args.excel_file
        if isinstance(excel_file_path, str):
            excel_file_path = Path(excel_file_path)
        excel_file_path = excel_file_path.expanduser().resolve()
        if excel_file_path.exists():
            try:
                from parse_excel import parse_excel as _parse_excel
                sheet_index = getattr(args, "excel_sheet_index", 0) or 0
                result = _parse_excel(excel_file_path, sheet_index=sheet_index)
                # 写入临时 excel_parsed.json
                import tempfile
                temp_dir = Path(tempfile.gettempdir())
                temp_json = temp_dir / "excel_parsed_temp.json"
                with open(temp_json, 'w', encoding='utf-8') as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                load_excel_parsed_json(cfg, temp_json)
                logger.info(f"【v34.x】Excel 文件已解析: {excel_file_path}")
            except Exception as e:
                import traceback
                print(f"警告: Excel 解析失败: {e}", file=sys.stderr)
                traceback.print_exc()
        else:
            print(f"警告: Excel 文件不存在: {excel_file_path}", file=sys.stderr)

    errors = validate_api_config(cfg)
    if errors:
        print("配置验证失败:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        if not args.validate:
            return ExitCode.CONFIG_ERROR

    if args.validate:
        print("配置验证通过", file=sys.stderr)
        return ExitCode.SUCCESS

    payload, err = run_all_bills(cfg)
    if err:
        print(err, file=sys.stderr)
        print(json.dumps({"error": err}, ensure_ascii=False))
        return ExitCode.NETWORK_ERROR

    # 【v8.5】判断是否使用 queryUri 模式
    req = cfg.get("request") or {}
    quri = req.get("queryUri") or req.get("query_uri") or ""
    uri_list = req.get("_query_uri_list") or []
    # 【v8.5 修复】参照实体批量查询时跳过合并旧索引，避免实体数量膨胀
    # 注意：skip_merge=True 时 write_outputs 不会加载旧索引，
    # 这意味着参照实体查询的结果不会与主表实体合并。
    # 这是预期行为：参照实体由 AI 手动追加到主表实体的输出中。
    skip_merge = bool(quri) and len(uri_list) > 0

    # 处理选择：插件 / Agent 下 stdin 非 TTY，只输出可解析的 stdout，由侧栏点选后带 code/id 重试
    selection_info = payload.get("selection")
    if selection_info:
        items = selection_info.get("items", [])
        names = selection_info.get("names", [])
        billname = selection_info.get("billname", "")
        if not items or not names:
            return ExitCode.CONFIG_ERROR

        ui_text = _format_selection_stdout_for_ui(selection_info)
        auto_select = cfg.get("request", {}).get("_auto_select_first", False) or _perf(cfg).get("auto_select_first", False)
        if not _should_interactive_selection_prompt() or auto_select:
            if auto_select:
                # 【自动化 v11.x】当 searchByName 返回多个匹配时，存储所有匹配项到请求上下文
                # 后续 build_entities_for_bill 会直接使用这些预选项目，跳过二次搜索
                req = cfg.setdefault("request", {})
                req["_pre_selected_items"] = items  # 存储完整的 item 信息（含 id、uri）
                logger.info(f"[自动选择 v11.x] 批量处理 {len(items)} 个匹配项")
                for item in items:
                    logger.info(f"[自动选择] - {item.get('name')} (code={item.get('code')}, id={item.get('id')})")
                # 构建唯一的 billname 列表，用 code=xxx 格式区分
                billname_base = selection_info.get("billname", "")
                composite_parts = []
                for item in items:
                    code = item.get("code", "")
                    name = item.get("name", "") or billname_base
                    composite_parts.append(f"{name}（code={code}）")
                req["allbillname"] = ",".join(composite_parts)
                logger.info(f"[自动选择 v11.x] allbillname 更新为: {req['allbillname']}")
            else:
                # searchByName 多对象的提示须独占 stdout 末尾，不可再拼接 JSON，
                # 否则 Chat 侧栏 parseBillNameChoiceFromStdout 会误把 JSON 并入选项。
                print(ui_text, end="")
                return ExitCode.SUCCESS
        else:
            # 交互模式：打印选项列表
            print(ui_text, end="", file=sys.stdout)
            print(
                f"\n找到多个业务对象，请在终端为 [{billname}] 选择对应序号：",
                file=sys.stderr,
            )

            while True:
                for i, name in enumerate(names):
                    print(f"  [{i+1}] {name}", file=sys.stderr)
                try:
                    choice = input("\n请输入序号 (1-" + str(len(names)) + ") 或 q 退出: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n已退出", file=sys.stderr)
                    return ExitCode.USER_CANCEL

                if choice.lower() == "q":
                    print("已退出", file=sys.stderr)
                    return ExitCode.USER_CANCEL

                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(items):
                        selected = items[idx]
                        req = cfg.setdefault("request", {})
                        req["_selected_bo_code"] = selected.get("code")
                        req["_selected_bo_id"] = selected.get("id")
                        req["_selected_bo_name"] = selected.get("name")
                        logger.info(
                            f"已选择: {selected.get('name')} (code={selected.get('code')})"
                        )
                        break
                    else:
                        print(f"无效的选择，请输入 1-{len(names)} 之间的数字", file=sys.stderr)
                except ValueError:
                    print("无效的输入，请输入数字或 q 退出", file=sys.stderr)

        # 重新运行，使用选中的业务对象
        payload, err = run_all_bills(cfg)
        if err:
            print(err, file=sys.stderr)
            print(json.dumps({"error": err}, ensure_ascii=False))
            return ExitCode.NETWORK_ERROR

    write_outputs(cfg, payload, skip_merge=skip_merge, req=req)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    # 【v7.0 新增】退出前刷新 Redis 写入缓冲区
    _flush_redis_write_buffer()

    if args.run_db_check:
        import subprocess

        db = cfg.get("database") or {}
        if db.get("enabled"):
            logger.info("运行数据库校验...")
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).parent / "db_query.py"),
                    "--config",
                    args.config,
                ],
                check=False,
            )

    return ExitCode.SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
