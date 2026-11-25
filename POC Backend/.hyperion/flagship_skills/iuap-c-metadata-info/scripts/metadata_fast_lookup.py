"""
快速元数据查找模块
使用预处理的metadata_lookup.json进行快速匹配，避免多次API调用
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


# exact_biz: bizName 键精确命中; exact_metadata: 仅 metadataName 键命中; partial: 模糊
MatchSource = str


@dataclass
class FastLookupResult:
    """快速查找结果"""
    found: bool = False
    uri: Optional[str] = None
    domain: Optional[str] = None
    table_name: Optional[str] = None
    schema: Optional[str] = None
    biz_name: Optional[str] = None
    metadata_name: Optional[str] = None
    match_type: Optional[str] = None  # 兼容旧逻辑: 'exact' / 'partial' / 'uri'
    match_source: Optional[str] = None  # 'exact_biz' | 'exact_metadata' | 'partial'


class MetadataFastLookup:
    """元数据快速查找器"""

    _instance: Optional["MetadataFastLookup"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                # Double-check locking pattern
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._lookup_data = None
        return cls._instance

    def __init__(self):
        self._lookup_data: Optional[Dict[str, Any]] = None

    def load_lookup_json(self, json_path: str) -> bool:
        """加载查找JSON文件"""
        try:
            p = Path(json_path)
            if not p.exists():
                return False
            with p.open("r", encoding="utf-8") as f:
                self._lookup_data = json.load(f)
            return True
        except Exception:
            return False

    def _normalize(self, s: str) -> str:
        """规范化字符串用于比较"""
        return s.strip().lower() if s else ""

    def _exact_index_hits(
        self, name: str, index: Dict[str, List[int]], records: List[Dict], source: MatchSource
    ) -> List[FastLookupResult]:
        """索引键与 name 完全一致（同字符串）时返回所有记录。"""
        if name not in index:
            return []
        out: List[FastLookupResult] = []
        for idx in index[name]:
            if idx >= len(records):
                continue
            rec = records[idx]
            out.append(
                FastLookupResult(
                    found=True,
                    uri=rec.get("uri"),
                    domain=rec.get("domain"),
                    table_name=rec.get("tableName"),
                    schema=rec.get("schema"),
                    biz_name=rec.get("bizName"),
                    metadata_name=rec.get("metadataName"),
                    match_type="exact",
                    match_source=source,
                )
            )
        return out

    def _partial_hits(
        self, name: str, index: Dict[str, List[int]], records: List[Dict]
    ) -> List[FastLookupResult]:
        """子串模糊匹配，总量上限与原先一致（约 3 条）。"""
        results: List[FastLookupResult] = []
        norm_name = self._normalize(name)
        if not norm_name:
            return results
        for key in index.keys():
            norm_key = self._normalize(key)
            if not norm_key or norm_name not in norm_key:
                continue
            for idx in index[key]:
                if idx >= len(records):
                    continue
                rec = records[idx]
                results.append(
                    FastLookupResult(
                        found=True,
                        uri=rec.get("uri"),
                        domain=rec.get("domain"),
                        table_name=rec.get("tableName"),
                        schema=rec.get("schema"),
                        biz_name=rec.get("bizName"),
                        metadata_name=rec.get("metadataName"),
                        match_type="partial",
                        match_source="partial",
                    )
                )
                if len(results) >= 3:
                    return results
            if len(results) >= 3:
                break
        return results

    # 标准业务 URI 前缀优先级（数字越小优先级越高）
    _URI_PRIORITY = {
        "voucher.": 1,       # 凭证系统（最标准）
        "udinghuo.": 2,      # 销售系统
        "scm.": 3,           # 供应链
        "bd.": 4,            # 基础数据
        "aa.": 5,            # 应收应付
        "earap.": 6,         # 应收应付
        "pu.": 7,            # 采购
        "st.": 8,            # 库存
        "mp.": 9,            # 生产
        "qm.": 10,           # 质量
        "fi.": 11,           # 财务
        "org.": 12,          # 组织
        "check.": 20,        # 客开系统（较低优先级）
        "cpu-": 20,          # 客开系统
        "imi.": 30,          # VR/接口系统（最低优先级）
    }

    def _get_uri_priority(self, uri: str) -> int:
        """获取 URI 优先级，数字越小优先级越高"""
        if not uri:
            return 999
        for prefix, priority in self._URI_PRIORITY.items():
            if uri.startswith(prefix):
                return priority
        return 50  # 未知的 URI 默认中等优先级

    def _is_standard_uri(self, uri: str) -> bool:
        """判断是否为标准业务 URI（而非客开或第三方系统）"""
        if not uri:
            return False
        # 标准系统通常以小写字母开头，且不在低优先级列表中
        low_priority_prefixes = ["imi.", "check.", "cpu-", "yonyou.", "custom."]
        for prefix in low_priority_prefixes:
            if uri.startswith(prefix):
                return False
        return True

    def _dedup_and_sort(self, results: List[FastLookupResult]) -> List[FastLookupResult]:
        """
        去重并智能排序
        1. 按 URI 去重
        2. 按优先级排序：标准系统 > 非标准系统
        3. 精确匹配 > 模糊匹配
        """
        # 按 URI 去重，保留优先级最高的结果
        uri_map: Dict[str, FastLookupResult] = {}
        priority_map: Dict[str, int] = {}

        for r in results:
            uri = r.uri or ""
            if not uri:
                continue

            # 计算综合优先级
            base_priority = self._get_uri_priority(uri)
            match_priority = 0 if r.match_type == "exact" else 10
            is_standard = 0 if self._is_standard_uri(uri) else 100
            combined_priority = base_priority * 100 + match_priority + is_standard

            if uri not in uri_map or combined_priority < priority_map.get(uri, 9999):
                uri_map[uri] = r
                priority_map[uri] = combined_priority

        # 按优先级排序
        sorted_results = sorted(
            uri_map.values(),
            key=lambda r: priority_map.get(r.uri or "", 9999)
        )

        return sorted_results

    def lookup(self, billname: str) -> List[FastLookupResult]:
        """
        根据单据名称查找元数据，同时考虑 bizName 和 metadataName。

        匹配策略：
        1. 精确匹配 bizName（只做精确，不做模糊）
        2. 精确匹配 metadataName（只做精确，不做模糊）
        3. 精确匹配阶段结果去重
           - 命中1个 → 直接返回
           - 命中多个 → 返回让用户确认
           - 命中0个 → 进入模糊匹配兜底
        4. 模糊匹配 metadataName（仅精确匹配为空时触发）
        """
        if not self._lookup_data:
            return []

        records = self._lookup_data.get("records", [])
        biz_index = self._lookup_data.get("bizNameIndex", {})
        metadata_index = self._lookup_data.get("metadataNameIndex", {})

        if not records:
            return []

        # =============================================
        # 第一阶段：精确匹配
        # =============================================
        exact_results: List[FastLookupResult] = []

        # 1) 精确匹配 bizName
        exact_biz = self._exact_index_hits(billname, biz_index, records, "exact_biz")
        exact_results.extend(exact_biz)

        # 2) 精确匹配 metadataName
        exact_metadata = self._exact_index_hits(billname, metadata_index, records, "exact_metadata")
        exact_results.extend(exact_metadata)

        # 精确匹配去重
        exact_results = self._dedup_and_sort(exact_results)

        # =============================================
        # 核心判断：精确匹配阶段的 URI 数量
        # =============================================
        exact_uri_count = len(unique_uris_in_results(exact_results))

        if exact_uri_count == 1:
            # ✅ 精确匹配到1个URI，直接返回（无需模糊匹配）
            return exact_results
        elif exact_uri_count > 1:
            # ⚠️ 精确匹配到多个URI，返回让用户确认
            return exact_results

        # =============================================
        # 第二阶段：模糊匹配（仅精确匹配为空时触发）
        # =============================================
        partial_results = self._partial_hits(billname, metadata_index, records)
        return self._dedup_and_sort(partial_results)

    def lookup_with_match_info(self, billname: str) -> tuple:
        """
        返回 (results, match_type, exact_uri_count)
        match_type: 'exact_single' | 'exact_multiple' | 'partial' | 'none'
        """
        if not self._lookup_data:
            return [], 'none', 0

        records = self._lookup_data.get("records", [])
        biz_index = self._lookup_data.get("bizNameIndex", {})
        metadata_index = self._lookup_data.get("metadataNameIndex", {})

        if not records:
            return [], 'none', 0

        # 第一阶段：精确匹配
        exact_results: List[FastLookupResult] = []
        exact_biz = self._exact_index_hits(billname, biz_index, records, "exact_biz")
        exact_results.extend(exact_biz)
        exact_metadata = self._exact_index_hits(billname, metadata_index, records, "exact_metadata")
        exact_results.extend(exact_metadata)
        exact_results = self._dedup_and_sort(exact_results)
        exact_uri_count = len(unique_uris_in_results(exact_results))

        if exact_uri_count == 1:
            return exact_results, 'exact_single', exact_uri_count
        elif exact_uri_count > 1:
            return exact_results, 'exact_multiple', exact_uri_count

        # 第二阶段：模糊匹配
        partial_results = self._partial_hits(billname, metadata_index, records)
        partial_results = self._dedup_and_sort(partial_results)

        if partial_results:
            return partial_results, 'partial', 0
        return [], 'none', 0

    def lookup_multiple(self, billnames: List[str]) -> Dict[str, List[FastLookupResult]]:
        """批量查找多个单据名称"""
        return {name: self.lookup(name) for name in billnames}

    def get_by_uri(self, uri: str) -> Optional[FastLookupResult]:
        """根据URI直接查找"""
        if not self._lookup_data:
            return None

        uri_index = self._lookup_data.get("uriIndex", {})
        records = self._lookup_data.get("records", [])

        if uri in uri_index:
            idx = uri_index[uri]
            if idx < len(records):
                rec = records[idx]
                return FastLookupResult(
                    found=True,
                    uri=rec.get("uri"),
                    domain=rec.get("domain"),
                    table_name=rec.get("tableName"),
                    schema=rec.get("schema"),
                    biz_name=rec.get("bizName"),
                    metadata_name=rec.get("metadataName"),
                    match_type="uri",
                    match_source="uri",
                )
        return None

    @property
    def is_loaded(self) -> bool:
        return self._lookup_data is not None

    @property
    def total_records(self) -> int:
        return self._lookup_data.get("totalCount", 0) if self._lookup_data else 0


def unique_uris_in_results(results: List[FastLookupResult]) -> List[str]:
    """保序去重后的 uri 列表（跳过空）。"""
    seen: Set[str] = set()
    out: List[str] = []
    for r in results:
        u = (r.uri or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def get_fast_lookup(cfg: dict = None) -> MetadataFastLookup:
    """获取快速查找器实例"""
    lookup = MetadataFastLookup()

    if lookup.is_loaded:
        return lookup

    # 尝试从配置或默认路径加载
    if cfg:
        paths = cfg.get("paths") or {}
        json_path = paths.get("metadata_lookup_json")
        if json_path:
            if lookup.load_lookup_json(json_path):
                return lookup

    # 默认路径
    default_paths = [
        Path(__file__).parent.parent / "reference" / "metadata_lookup.json",
        Path(__file__).parent.parent.parent / "reference" / "metadata_lookup.json",
    ]

    for p in default_paths:
        if lookup.load_lookup_json(str(p)):
            return lookup

    return lookup
