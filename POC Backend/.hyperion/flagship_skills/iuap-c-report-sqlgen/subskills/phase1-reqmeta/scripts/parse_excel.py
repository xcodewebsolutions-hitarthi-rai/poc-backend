#!/usr/bin/env python3
"""
解析 Excel 报表模板，提取列名/表头、业务对象名称。

核心设计原则（v4.0）：
  严格从 Excel "核心业务对象" 列的原样提取，按分隔符拆分，不做任何白名单校验。
  用户写什么就提取什么。

输出 JSON 到 stdout：
{
  "excelFile": "...",
  "sheetIndex": 0,
  "sheetName": "...",
  "columns": ["列1", "列2", ...],
  "dataSource": {
    "type": "billNames",
    "billNames": [...],      // 所有提取到的业务对象名称（去重，原样）
    "rawMappings": [          // 每个报表对应的业务对象（原始格式）
      {"rowIndex": N, "reportNo": "...", "reportName": "...", "billNames": [...]}
    ]
  },
  "allCells": [["行1列1", ...], ...]
}

Usage:
  python parse_excel.py /path/to/模板.xlsx
  python parse_excel.py /path/to/模板.xlsx --sheet-index 1
  python parse_excel.py /path/to/模板.xlsx --output-json  # 输出到 output/parse_*.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

# 尝试导入 Excel 处理库
try:
    import openpyxl

    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False

try:
    import xlrd

    _HAS_XLRD = True
except ImportError:
    _HAS_XLRD = False

# 日志配置
try:
    from iuap_common.logging_config import get_logger

    logger = get_logger("parse_excel")
except ImportError:
    import logging

    logger = logging.getLogger("parse_excel")
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from iuap_common.console_utf8 import configure_stdio_utf8


# ============================================================
# 核心：分隔符与拆分逻辑
# ============================================================

# 所有分隔符（全角+半角）
_DELIMITER_PATTERN = re.compile(r"[,，、；;／/|\＋+\n\r\t&]+")

# 用于识别"核心业务对象"列的表头关键词
_CORE_BILLNAME_HEADERS: Set[str] = {
    "核心业务对象", "业务对象", "数据源",
    "单据名称", "业务对象名称", "报表对象",
    "单据", "实体", "来源单据",
}

# 无意义的业务对象 suffix 词，这些是复合业务对象名的组成部分，不应作为独立名称提取
# 注意：仅过滤纯 suffix 词，不含业务语义的词汇
_BILLNAME_SUFFIX_WORDS: Set[str] = {
    "明细", "主表",
    "明细表", "明细账",
    "detail", "sub",
}

# 列头关键词：这些列包含描述性内容，不是业务对象来源
_EXCLUDE_COL_HEADERS: Set[str] = {
    # 难度/分类
    "难度等级", "难度名称", "难度", "等级",
    # 报表信息
    "报表编号", "编号", "序号",
    "报表名称", "报表", "名称",
    "场景说明", "说明", "描述",
    # 技能要求
    "技能要求", "要求", "能力",
    # 统计/数量
    "报表数量", "数量", "统计",
    # 描述性
    "备注", "描述", "内容",
    # 通用排除词
    "序号", "分类", "类型",
}


# 用于识别"数据源字段"列的表头关键词
_DATASOURCE_FIELD_HEADERS: Set[str] = {
    "数据源字段", "数据源(业务对象)", "数据源", "来源业务对象",
    "来源单据", "来源", "业务对象来源",
}

# 用于识别"使用到的业务对象"区域的标记关键词
_BILL_OBJECTS_SECTION_HEADERS: Set[str] = {
    "使用到的业务对象", "使用到的业务对象：", "业务对象", "涉及的表单",
    "关联业务对象", "相关业务对象",
}

# URI 正则：匹配点分格式的 BIP URI，如 pc.product.Product、bd.adminOrg.AdminOrgVO
_URI_PATTERN = re.compile(r"\(([a-zA-Z_][a-zA-Z0-9_.]+)\)")

# 模块级预编译：处理括号内容的正则（避免重复编译）
_BRACKET_CONTENT_PATTERN = re.compile(r"[()（）].*")
_TRAILING_BRACKETS_PATTERN = re.compile(r"[()（）]+$")

# 【v8.3 新增】取值规则中提取业务对象的模式
# 匹配 "1.单据取以下类型：XXX、XXX、XXX" 或类似格式
_RULE_BILLTYPE_PATTERN = re.compile(
    r"(?:单据)?取(?:以下)?类型[：:]\s*([^。\n]+)"
)
# 匹配常见业务对象名称后缀
_BILLNAME_KNOWN_SUFFIXES = (
    "单", "单据", "单据明细", "主表", "明细表", "明细账",
    "登记", "申请", "审批", "核销", "结算", "报销",
    "付款", "收款", "收款认领", "认领",
    "对账", "确认", "开票", "收票",
)


def _extract_billnames_from_rules(rows: List[List[str]]) -> List[str]:
    """
    【v8.3 新增】从 Excel 中直接提取白名单中的业务对象名称。

    简单高效：从所有单元格内容中匹配已知的业务对象名称白名单。

    返回: 业务对象名称列表（去重）
    """
    billnames: List[str] = []
    seen: Set[str] = set()

    # 业务对象名称白名单
    bill_whitelist = {
        "个人借款单", "对公预付单", "市场活动预付单", "期初对公预付单",
        "通用报销单", "差旅费报销单", "借款核销",
        "销售订单", "销售退货单", "采购订单", "采购入库单",
        "销售出库单", "销售退货出库单",
    }

    for row in rows:
        for cell in row:
            cell_str = str(cell).strip()
            if not cell_str:
                continue

            # 检查白名单中的业务对象名称是否在单元格中
            for bn in bill_whitelist:
                if bn in cell_str and bn not in seen:
                    seen.add(bn)
                    billnames.append(bn)

    return billnames


def _split_billnames_raw(cell_value: str) -> Tuple[List[str], List[str]]:
    """
    从单元格原始值中按分隔符拆分出业务对象名称和 URI。

    返回: (billNames, uriList)
      - billNames: 业务对象名称列表
      - uriList: 提取到的 URI 列表（来自括号中的点分格式内容）

    核心原则：严格按分隔符拆分，原样返回，不做白名单校验。

    分隔符支持：
      - `++`：组分隔符，先按此拆分，再处理组内分隔符
      - 单个 `+`：组内分隔符
      - 顿号(、) 逗号(,) 全角逗号(，) 分号(;；) 斜杠(/) 反斜杠(\\) 竖线(|) 空格 换行 等

    特殊处理：
      - 优先提取所有 `(URI)` 中的 URI，括号前的内容（如"物料"）丢弃
      - `++` 优先拆分，防止 `++` 被 `+` 字符集吞掉
      - 去除括号内容后过滤 suffix 词
      - 【v11.2 已废弃】不再移除 suffix 词，保留原始名称以确保精准匹配
    """
    if not cell_value or not str(cell_value).strip():
        return [], []

    v = str(cell_value).strip()
    # 全角转半角
    v = v.replace("（", "(").replace("）", ")")

    # Step 1: 扫描所有 (URI)，收集到 uri_list（不在此处移除，保留在原字符串中）
    # 这样后续按分隔符拆分时，含 (URI) 的片段仍可被单独处理
    uri_list: List[str] = []
    for m in _URI_PATTERN.finditer(v):
        uri = m.group(1).strip()
        if uri and "." in uri and uri not in uri_list:
            uri_list.append(uri)

    results: List[str] = []
    seen: Set[str] = set()

    def _process_part(part: str) -> None:
        """
        内部处理每个拆分片段：
          - 含 (URI) 的片段：提取 label（移除 URI 部分），URI 已在 uri_list 中收集
          - 其他片段：去除括号内容 → 直接加入结果（【v11.2】不做 suffix 过滤，保留原始名称精准匹配）
        """
        part = part.strip()
        if not part:
            return
        # 【v36.x 修复】如果片段含 URI 模式（如 物料(pc.product.Product)），提取 label（移除 URI 部分）
        if _URI_PATTERN.search(part):
            # 移除 URI 部分，保留 label
            part = _URI_PATTERN.sub("", part).strip()
            if not part:
                return
        # 使用预编译的正则，避免重复编译
        part = _BRACKET_CONTENT_PATTERN.sub("", part).strip()
        part = _TRAILING_BRACKETS_PATTERN.sub("", part).strip()
        if not part:
            return
        if part not in seen:
            seen.add(part)
            results.append(part)

    # Step 2: `++` 组分隔符优先拆分
    if "++" in v:
        groups = v.split("++")
        for group in groups:
            if not group.strip():
                continue
            for inner in _DELIMITER_PATTERN.split(group):
                _process_part(inner)
        return results, uri_list

    # Step 3: 无 ++ 时，按标准分隔符拆分
    for part in _DELIMITER_PATTERN.split(v):
        _process_part(part)

    return results, uri_list


def _find_core_billname_col(headers: List[str]) -> int:
    """
    找到"核心业务对象"列的索引。
    优先精确匹配，其次包含匹配（排除"报表字段"等误匹配）。
    """
    # 精确匹配的关键词（高优先级）
    EXACT_KEYWORDS = {"核心业务对象", "业务对象名称", "业务对象", "报表对象", "单据名称"}

    for j, h in enumerate(headers):
        h = h.strip()
        if not h:
            continue
        # 优先精确匹配
        if h in EXACT_KEYWORDS:
            return j
    # 其次包含匹配（避免匹配到"报表字段"等误匹配）
    for j, h in enumerate(headers):
        h = h.strip()
        if not h:
            continue
        for kw in _CORE_BILLNAME_HEADERS:
            if kw in h:
                # 排除"报表"单独在末尾的误匹配（如"报表字段"）
                if kw == "报表" and h.endswith("报表"):
                    continue
                return j
    return -1


def _find_report_name_col(headers: List[str]) -> int:
    """找到"报表名称"/"报表编号"列的索引。"""
    report_keywords = {"报表名称", "报表名称", "报表", "编号"}
    for j, h in enumerate(headers):
        h = h.strip()
        if not h:
            continue
        if h in report_keywords or "报表" in h or "编号" in h:
            return j
    return -1


def _find_datasource_field_col(headers: List[str]) -> int:
    """
    找到"数据源字段"列的索引。
    搜索所有列头，匹配 _DATASOURCE_FIELD_HEADERS 中的关键词（精确或包含）。
    """
    for j, h in enumerate(headers):
        h = h.strip()
        if not h:
            continue
        for kw in _DATASOURCE_FIELD_HEADERS:
            if kw in h or h in kw:
                return j
    return -1


def _find_bill_objects_section(rows: List[List[str]]) -> Tuple[int, List[Dict[str, Any]]]:
    """
    找到"使用到的业务对象"区域，并提取业务对象列表和关系描述。

    返回: (start_row_index, bill_objects_with_relations)
      - start_row_index: 业务对象区域的起始行索引（-1 表示未找到）
      - bill_objects_with_relations: 业务对象列表（含关系描述）
    """
    bill_objects: List[Dict[str, Any]] = []
    start_idx = -1

    # 常见的业务对象名称后缀（用于识别真正的业务对象行）
    bill_suffixes = {"主表", "明细", "详情", "子表", "主数据", "单", "单据"}

    # 【v12.x 新增】已知不是独立业务对象的描述词（这些是操作名称，不是业务对象）
    _NOT_INDEPENDENT_BILL_NAMES = {
        "收票登记", "背书办理", "银行托收", "贴现办理",
        "收票", "背书", "托收", "贴现", "转让", "收款", "付款", "退款",
    }

    for row_idx, row in enumerate(rows):
        if not row:
            continue

        # 检查是否是业务对象区域标记行
        for cell in row[:3]:  # 只检查前3列
            cell_str = str(cell).strip()
            if cell_str and any(kw in cell_str for kw in _BILL_OBJECTS_SECTION_HEADERS):
                start_idx = row_idx + 1  # 下一行开始是业务对象
                logger.info(f"找到业务对象区域标记: '{cell_str}'，起始行: {start_idx}")
                break

        # 如果已找到起始行，开始提取业务对象
        if start_idx >= 0 and row_idx >= start_idx:
            # 【v22.x 修复】检查 A/B 两列（索引0和1），因为业务对象可能在任一列
            # 后续列从同一行获取 URI、表名、关系描述等信息
            if not row or len(row) == 0:
                continue

            # 优先取 A 列，如果为空则取 B 列
            bill_name_cell = str(row[0]).strip() if len(row) > 0 and row[0] else ""
            if not bill_name_cell and len(row) > 1:
                bill_name_cell = str(row[1]).strip() if row[1] else ""
            if not bill_name_cell:
                continue

            bill_name = bill_name_cell

            # 排除标题行
            if bill_name in ["关系描述"]:
                continue

            # 排除明显的非业务对象内容
            exclude_patterns = [
                "查询条件", "关系描述", "日期", "编号", "说明",
                "以", "通过", "关联", "销售组织", "=", "---",
                "使用到的业务对象", "从", "进行", "获取", "为准",
                "业务对象", "自定义项", "（业务对象"
            ]
            if any(pat in bill_name for pat in exclude_patterns):
                continue

            # 排除日期格式
            if len(bill_name) == 10 and ("/" in bill_name or "-" in bill_name):
                continue

            # 排除纯数字编号
            if bill_name.isdigit():
                continue

            # 排除纯字母数字组合（如 "drft_noteinformation"）
            if len(bill_name) > 3 and not any('一' <= c <= '鿿' for c in bill_name):
                continue

            # 检查是否是常见的业务对象后缀或包含关键词
            # 【v18.x 修复】单字符关键词必须用 endswith，避免误匹配
            _SUFFIX_SHORT = ("单", "票")  # 需要 endswith 匹配
            _IN_SHORT = ("出库", "物料", "组织", "客户", "供应商", "合同", "订单",
                         "发票", "发货", "应收", "应付", "核销", "借款单", "报销单",
                         "预付单", "借款核销", "报销核销", "工作台", "办理", "托收",
                         "贴现", "背书", "收票", "登记", "票据", "付款", "退款",
                         "收款", "转让", "产品", "额度", "释放", "信息", "执行", "付")
            is_business_object = (
                any(bill_name.endswith(suf) for suf in bill_suffixes) or
                any(bill_name.endswith(suf) for suf in _SUFFIX_SHORT) or
                any(suf in bill_name for suf in _IN_SHORT)
            )

            if is_business_object:
                # 【v12.x 修复】仅排除纯操作名称
                excluded_operations = {"收票登记"}
                if bill_name in excluded_operations:
                    continue

                # 【v18.x 修复】排除字段列表
                if ("、" in bill_name or "," in bill_name) and not any(
                    bill_name.endswith(suf) for suf in ("单", "表", "主表", "子表", "明细")
                ):
                    continue

                # 【v20.x 新增】排除括号说明（但保留包含"子表"的括号）
                if "（" in bill_name and "）" in bill_name:
                    # 保留包含"子表"的业务对象名
                    if "子表" not in bill_name:
                        continue

                # 【v19.x 修复】billName 应该是中文名称，不是 URI
                bill_name_cn = bill_name
                uri = ""
                table_name = ""
                related_fields = ""
                relation = ""

                # 【v22.x 修复】根据 bill_name 来源列确定后续列的起始索引
                # 如果 bill_name 来自 B 列（索引1），则 URI 等信息在 C 列（索引2）
                bill_col_idx = 0  # 默认 A 列
                if bill_name_cell == str(row[1]).strip() if len(row) > 1 else "":
                    bill_col_idx = 1  # B 列

                # 从同一行的后续列获取 URI、表名、关系描述、涉及字段
                for next_col_idx in range(bill_col_idx + 1, min(7, len(row))):
                    next_cell = str(row[next_col_idx]).strip() if next_col_idx < len(row) and row[next_col_idx] else ""

                    if not next_cell:
                        continue

                    # URI 格式：xxx.xxx.xxx
                    if "." in next_cell and len(next_cell) > 5 and " " not in next_cell and next_cell.count(".") >= 2:
                        if not uri:
                            uri = next_cell
                    # 表名：通常是下划线格式
                    elif "_" in next_cell and not any(c in next_cell for c in "、，,") and len(next_cell) > 3:
                        if not table_name:
                            table_name = next_cell
                    # 涉及到的字段：顿号分隔
                    elif "、" in next_cell or "," in next_cell:
                        if not related_fields:
                            related_fields = next_cell
                    # 关系描述
                    elif len(next_cell) > 5 and ("通过" in next_cell or "从" in next_cell or "关联" in next_cell):
                        if not relation:
                            relation = next_cell

                bill_objects.append({
                    "billName": bill_name_cn,
                    "uri": uri,
                    "tableName": table_name,
                    "relation": relation,
                    "relatedFields": related_fields
                })

    return start_idx, bill_objects


# ============================================================
# 查询条件解析
# ============================================================

# 用于识别"查询条件"区域的标记关键词
_QUERY_CONDITION_HEADERS: Set[str] = {
    "查询条件", "默认查询", "查询条件：", "默认查询条件",
    "筛选条件", "过滤条件", "条件",
}

# 用于识别"查询条件"字段的表头关键词
_QUERY_FIELD_HEADERS: Set[str] = {
    "查询字段", "条件字段", "查询参数", "筛选字段",
}

# 排除的字段（这些不是查询条件字段，需要精确匹配或完整包含才排除）
_EXCLUDE_QUERY_FIELDS: Set[str] = {
    "使用到的业务对象", "涉及的业务对象", "关联业务对象",
    "URI", "表名", "关系描述", "涉及的字段",
    "说明", "其他",
    # 注意：日期区间、单据类型等是查询条件的一部分，应该保留
}


def _find_query_condition_section(rows):
    """
    找到"查询条件"区域，提取查询条件和查询逻辑。

    【v41.x】
    输出 queryFieldsDetail: [{fieldName, billName, description, value, mappingType}]
    """
    query_info = {
        "defaultQuery": "",
        "queryFieldsDetail": [],
    }

    start_idx = -1
    in_default_query = False
    default_query_b_content = ""

    query_fields_detail = []
    seen_fields = set()

    # 处理每一行
    for row_idx, row in enumerate(rows):
        if not row or len(row) == 0:
            continue

        a_cell = str(row[0]).strip() if len(row) > 0 and row[0] else ""
        b_cell = str(row[1]).strip() if len(row) > 1 and row[1] else ""

        # 匹配"默认查询"标记
        if "默认查询" in a_cell or "默认查询" in b_cell:
            start_idx = row_idx
            in_default_query = True
            if b_cell and len(b_cell) > 20:
                default_query_b_content = b_cell
                query_info["defaultQuery"] = b_cell
            continue

        # 如果在默认查询区域内
        if in_default_query:
            # 检查是否到达"查询条件"字段行
            if "查询条件" in a_cell or "查询条件" in b_cell:
                in_default_query = False
                continue

            # 继续收集默认查询的说明文本（排除URI行和业务对象行）
            if b_cell and len(b_cell) > 20:
                # 排除URI格式的内容（点分格式如 st.storeprorecord.xxx）
                if "." in b_cell and b_cell.count(".") >= 2 and not any(c in b_cell for c in ["等于", "包含", "在", "产品", "材料", "部门"]):
                    continue
                if not default_query_b_content:
                    default_query_b_content = b_cell
                else:
                    default_query_b_content += "\n" + b_cell
                query_info["defaultQuery"] = default_query_b_content

        # 检查是否到达"使用到的业务对象"区域
        if "使用到的业务对象" in b_cell or "涉及" in b_cell:
            break

    # 解析 defaultQuery 文本，提取查询条件
    if default_query_b_content:
        # 简单的正则匹配
        patterns = [
            # xxx.字段="值"
            (r'([^\s]+?)\.([^\s=]+?)\s*=\s*"([^"]+)"', '等于', False),
            # xxx.字段='值'
            (r"([^\s]+?)\.([^\s=]+?)\s*=\s*'([^']+)'", '等于', False),
            # xxx.字段=值
            (r'([^\s]+?)\.([^\s=]+?)\s*=\s*([^\s，。；]+)', '等于', False),
            # xxx.字段在...中间
            (r'([^\s]+?)\.([^\s在]+?)\s*在(.+?)\s*中间', '在中间', True),
            # xxx.字段包含${参数}
            (r'([^\s]+?)\.([^\s包含]+?)\s*包含\s*\$\{([^}]+)\}', '包含', True),
            # xxx.字段包含查询条件中的${参数}
            (r'([^\s]+?)\.([^\s包含]+?)\s*包含\s*查询条件中的\s*\$\{([^}]+)\}', '包含', True),
        ]

        text = default_query_b_content
        for line in text.split("\n"):
            line = line.strip()
            if not line or not line[0].isdigit():
                continue
            # 去掉行号
            line = re.sub(r'^\d+[、.。]', '', line).strip()

            for pattern, desc, is_dynamic in patterns:
                for m in re.finditer(pattern, line):
                    bill_name = m.group(1).strip()
                    field_name = m.group(2).strip()
                    value_part = m.group(3).strip()

                    # 动态参数用 ${字段名} 格式
                    if is_dynamic or "${" in value_part:
                        mapping_type = "动态参数"
                        # 如果 value_part 不含 ${}，包装成 ${field_name} 格式
                        if "${" not in value_part:
                            value_part = "${" + field_name + "}"
                    else:
                        mapping_type = "固定参数"

                    if field_name not in seen_fields:
                        seen_fields.add(field_name)
                        query_fields_detail.append({
                            "fieldName": field_name,
                            "billName": bill_name,
                            "description": desc,
                            "value": value_part,
                            "mappingType": mapping_type
                        })

    # 处理"查询条件"行中的参数
    for row_idx, row in enumerate(rows):
        if not row or len(row) == 0:
            continue

        a_cell = str(row[0]).strip() if len(row) > 0 and row[0] else ""
        b_cell = str(row[1]).strip() if len(row) > 1 and row[1] else ""

        if a_cell == "查询条件" or b_cell == "查询条件":
            for col_idx in range(1, min(len(row), 6)):
                cell_val = str(row[col_idx]).strip() if row[col_idx] else ""
                if not cell_val or cell_val in ["查询条件", "查询字段", "说明"]:
                    continue

                field_name = cell_val
                field_desc = ""
                if "-" in cell_val:
                    parts = cell_val.split("-", 1)
                    field_name = parts[0].strip()
                    field_desc = parts[1].strip() if len(parts) > 1 else ""

                if field_name not in seen_fields:
                    seen_fields.add(field_name)
                    bill_name = ""
                    if field_name in ["库存组织", "审批日期", "计划批次号"]:
                        bill_name = "产品入库主表"
                    elif field_name in ["部门", "领用用途", "生产计划数量"]:
                        bill_name = "材料出库单主表"
                    elif field_name in ["物料", "批次号"]:
                        bill_name = "产品入库子表"

                    query_fields_detail.append({
                        "fieldName": field_name,
                        "billName": bill_name,
                        "description": field_desc,
                        "value": "${" + field_name + "}",
                        "mappingType": "动态参数"
                    })

    if query_fields_detail:
        query_info["queryFieldsDetail"] = query_fields_detail

    return start_idx, query_info


# ============================================================
# 表头字段映射解析（v10.0 新增）
# ============================================================

def _extract_column_field_mapping(
    rows: List[List[str]],
    columns: List[str],
    headers: List[str],
    bill_objects: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    提取 Excel 表头与实际字段的映射关系。

    【v42.x 新设计】
    - columnName: Excel表头名称
    - fieldPath: 字段路径（如 "产品入库子表.物料.物料名称"）
    - fieldLevel: 层级数（2=直接取值，3+=参照取值）
    - fieldType: 字段类型（参照取值/直接取值）
    - calculationRule: 计算逻辑（数量合计、平均值等）
    - steps: 解析后的步骤链
      - step: 步骤序号
      - billName: 业务对象名称
      - uri: 业务对象URI
      - tableName: 表名
      - fieldName: 字段名

    Args:
        rows: Excel 原始行数据
        columns: 表头列名列表
        headers: 解析后的表头
        bill_objects: 业务对象列表

    Returns:
        表头字段映射列表
    """
    if not rows:
        return []

    # 找到表头行索引
    header_row_idx = -1
    for i, row in enumerate(rows):
        non_empty = sum(1 for c in row if str(c).strip())
        if non_empty >= 3:
            header_row_idx = i
            break

    if header_row_idx < 0 or header_row_idx + 1 >= len(rows):
        return []

    # 表头行和数据源描述行（可能有多行计算规则）
    header_row = rows[header_row_idx]
    datasource_rows = rows[header_row_idx + 1:header_row_idx + 4]  # 取后续几行作为数据源和计算规则

    # 构建业务对象名称到URI/表名的映射
    bill_name_to_info = {}
    for bo in bill_objects:
        bn = bo.get("billName", "")
        if bn:
            bill_name_to_info[bn] = {
                "uri": bo.get("uri", ""),
                "tableName": bo.get("tableName", "")
            }

    mappings: List[Dict[str, Any]] = []
    seen_columns: Set[str] = set()

    # 业务对象名称后缀（用于识别业务对象名）
    _BILL_SUFFIXES = ("主表", "子表", "单据", "单", "明细")

    # 【v43.x 新增】从已知业务对象中查找最长前缀匹配
    def _find_longest_bill_match(text: str, bill_names: List[str]) -> Tuple[str, str]:
        """
        从已知业务对象列表中查找最长前缀匹配。

        Args:
            text: 待匹配的文本（如 "产品入库主表.物料" 中的 "产品入库主表"）
            bill_names: 已知业务对象名称列表

        Returns:
            (matched_bill_name, remaining_text)
            - matched_bill_name: 匹配到的业务对象名称（空表示未匹配）
            - remaining_text: 匹配后剩余的文本
        """
        best_match = ""
        best_remaining = ""
        for bn in bill_names:
            # 精确匹配
            if text == bn:
                return bn, ""
            # 前缀匹配（text 以 bn 开头）
            if text.startswith(bn) and len(bn) > len(best_match):
                remaining = text[len(bn):]
                # 剩余部分应该是字段路径的一部分（如 .物料）
                if remaining.startswith("."):
                    best_match = bn
                    best_remaining = remaining[1:]  # 去掉开头的 "."
                elif not remaining:
                    best_match = bn
                    best_remaining = ""
        return best_match, best_remaining

    # 聚合计算关键词（只匹配独立成词的）
    _AGGREGATE_KEYWORDS = {
        "合计": "SUM",
        "数量合计": "SUM",
        "累计": "SUM",
        "汇总": "SUM",
        "平均值": "AVG",
        "平均": "AVG",
        "最大值": "MAX",
        "最大": "MAX",
        "最小值": "MIN",
        "最小": "MIN",
        "计数": "COUNT"
    }

    def _parse_field_path(source_desc: str) -> Tuple[str, int, str, str, str, List[Dict[str, Any]]]:
        """
        解析字段路径，返回 (fieldPath, fieldLevel, fieldType, calculationRule, calculationRuleDescription, steps)

        格式示例：
        - "产品入库主表.计划批次号" -> 直接取值
        - "取产品入库子表中物料参照的名称" -> 参照取值
        - "材料出库单子表中物料数量合计 合计" -> 直接取值 + 数量合计
        - "产品入库子表.物料 数量" -> 直接取值（空格需移除）
        - "判断产品入库子表中物料数量在哪个范围内..." -> 计算字段
        """
        if not source_desc:
            return "", 0, "", "", "", []

        field_path = ""
        field_level = 0
        field_type = "直接取值"
        calculation_rule = ""
        calculation_rule_description = ""  # 原始计算规则说明
        steps = []

        # 原始描述
        original_desc = source_desc.strip()
        desc = original_desc

        # 【新逻辑】从描述中分离字段路径和计算逻辑
        # 格式1：产品入库子表.物料.物料名称 参照取值
        # 格式2：产品入库子表.物料 判断产品入库子表中物料数量合计在哪个范围内...
        # 格式3：材料出库单子表.物料 数量合计
        # 格式4：产出数量/投入数量 （表头字段除法计算）

        # 【新增】检测表头字段除法格式：如"产出数量/投入数量"
        if "/" in desc and not any(kw in desc for kw in ["产品入库", "材料出库", "物料"]):
            # 可能是表头字段的计算公式
            parts = desc.split("/")
            if len(parts) == 2:
                numerator = parts[0].strip()
                denominator = parts[1].strip()
                calculation_rule = "FORMULA"
                field_path = desc
                field_level = 1
                steps.append({
                    "step": 1,
                    "billName": "",
                    "uri": "",
                    "tableName": "",
                    "fieldName": numerator,
                    "operator": "/",
                    "operand": denominator
                })
                return field_path, field_level, field_type, calculation_rule, calculation_rule_description, steps

        # 先找到类型关键词的位置，分割字段路径和计算逻辑
        type_keywords = ["直接取值", "参照取值", "固定值", "计算字段"]
        calc_keywords = ["判断", "计算", "合计", "数量合计", "累计", "汇总", "平均值", "平均", "最大值", "最大", "最小值", "最小", "计数"]

        # 找到类型关键词位置
        type_kw_pos = -1
        type_kw_found = ""
        for kw in type_keywords:
            pos = desc.find(kw)
            if pos > 0:  # 确保不是开头
                if type_kw_pos < 0 or pos < type_kw_pos:
                    type_kw_pos = pos
                    type_kw_found = kw

        # 找到计算关键词位置
        calc_kw_pos = -1
        calc_kw_found = ""
        for kw in calc_keywords:
            pos = desc.find(kw)
            if pos >= 0:
                if calc_kw_pos < 0 or pos < calc_kw_pos:
                    calc_kw_pos = pos
                    calc_kw_found = kw

        # 确定字段路径和计算逻辑
        if type_kw_pos >= 0 and (calc_kw_pos < 0 or type_kw_pos < calc_kw_pos):
            # 类型关键词在前：字段路径 = 类型关键词之前，提取计算逻辑 = 类型关键词之后
            field_path_part = desc[:type_kw_pos].strip()
            calc_logic_part = desc[type_kw_pos + len(type_kw_found):].strip()
            if type_kw_found == "参照取值":
                field_type = "参照取值"
        elif calc_kw_pos >= 0:
            # 计算关键词在前：字段路径 = 计算关键词之前，提取计算逻辑 = 计算关键词开始
            field_path_part = desc[:calc_kw_pos].strip()
            calc_logic_part = desc[calc_kw_pos:].strip()
        else:
            field_path_part = desc
            calc_logic_part = ""

        # 保存原始计算规则说明
        calculation_rule_description = calc_logic_part

        # 分析计算逻辑
        if calc_logic_part:
            if "判断" in calc_logic_part or "计算" in calc_logic_part:
                calculation_rule = "CASE WHEN"
            elif "合计" in calc_logic_part or "数量合计" in calc_logic_part:
                calculation_rule = "SUM"
            elif "累计" in calc_logic_part or "汇总" in calc_logic_part:
                calculation_rule = "SUM"
            elif "平均" in calc_logic_part:
                calculation_rule = "AVG"
            elif "最大值" in calc_logic_part or "最大" in calc_logic_part:
                calculation_rule = "MAX"
            elif "最小值" in calc_logic_part or "最小" in calc_logic_part:
                calculation_rule = "MIN"
            elif "计数" in calc_logic_part:
                calculation_rule = "COUNT"

        # 使用字段路径部分进行解析
        desc = field_path_part

        # 处理"取XXX的名称"格式（参照取值）
        # 例如："取产品入库子表中物料参照的名称"
        if desc.startswith("取") and "的名称" in desc:
            field_type = "参照取值"
            # 提取中间部分：产品入库子表中物料参照
            inner = desc.replace("取", "").replace("的名称", "").strip()

            # 按业务对象后缀分割
            bill_name = ""
            remaining = inner
            for suffix in _BILL_SUFFIXES:
                if suffix in inner:
                    idx = inner.index(suffix)
                    bill_name = inner[:idx + len(suffix)]
                    remaining = inner[idx + len(suffix):].replace("中", "")
                    break

            if not bill_name:
                # 尝试按"中"分割
                parts = inner.split("中")
                if len(parts) >= 2:
                    bill_name = parts[0]
                    remaining = parts[1]

            # 获取业务对象信息
            bill_info = bill_name_to_info.get(bill_name, {})
            field_path = f"{bill_name}.{remaining}.名称" if remaining else f"{bill_name}.物料.名称"
            field_level = 3

            steps.append({
                "step": 1,
                "billName": bill_name,
                "uri": bill_info.get("uri", ""),
                "tableName": bill_info.get("tableName", ""),
                "fieldName": remaining if remaining else "物料"
            })
            # 参照到物料档案的名称字段
            steps.append({
                "step": 2,
                "billName": "物料档案",
                "uri": "",
                "tableName": "",
                "fieldName": "名称"
            })
            return field_path, field_level, field_type, calculation_rule, calculation_rule_description, steps

        # 处理 "XXX.YYY" 或 "XXX.YYY.ZZZ" 格式
        # 策略：先移除计算关键词，再按 "." 分隔符解析
        # 例如："材料出库单子表.物料 数量合计" -> 先提取 "数量合计"，再解析 "材料出库单子表.物料"
        desc_for_parse = desc
        # 如果 calc_logic_part 存在，从 desc 中移除它
        if calc_logic_part:
            # 移除计算关键词
            desc_for_parse = desc.replace(calc_logic_part, "").strip()

        # 按 "." 分隔符解析，将各种分隔符统一为 "."
        # 但空格只有在业务对象和字段之间才作为分隔符
        # 如果有 calc_logic_part，说明空格后是计算逻辑，空格不应该作为分隔符
        if calc_logic_part:
            # 只把逗号等中文分隔符转为 "."
            normalized = re.sub(r'[，,、；;]+', '.', desc_for_parse)
        else:
            normalized = re.sub(r'[，,、；;\s]+', '.', desc_for_parse)

        if "." in normalized:
            # 清理并分割
            parts = [p.strip() for p in normalized.split(".") if p.strip()]

            if len(parts) >= 2:
                # 【v43.x 改进】获取已知业务对象名称列表用于匹配
                known_bill_names = list(bill_name_to_info.keys())

                # 找到业务对象名
                bill_name = ""
                field_parts = []
                for i, part in enumerate(parts):
                    if i == 0:
                        # 【v43.x】策略1：精确匹配 parts[0]
                        if bill_name_to_info.get(part):
                            bill_name = part
                            continue
                        # 【v43.x】策略2：从已知业务对象中查找最长前缀匹配
                        matched_bill, remaining = _find_longest_bill_match(part, known_bill_names)
                        if matched_bill:
                            bill_name = matched_bill
                            if remaining:
                                remaining = re.sub(r'\s+', '', remaining)
                                if remaining:
                                    field_parts.append(remaining)
                            continue
                        # 未匹配，移除空格作为字段
                        cleaned = re.sub(r'\s+', '', part)
                        field_parts.append(cleaned)
                    else:
                        # 移除字段名中的空格
                        cleaned = re.sub(r'\s+', '', part)
                        field_parts.append(cleaned)

                # 【v43.x】如果还是没找到业务对象，尝试第一个parts的前缀匹配
                if not bill_name and parts:
                    matched_bill, remaining = _find_longest_bill_match(parts[0], known_bill_names)
                    if matched_bill:
                        bill_name = matched_bill
                        field_parts = []
                        if remaining:
                            remaining = re.sub(r'\s+', '', remaining)
                            if remaining:
                                field_parts.append(remaining)
                        # 添加剩余 parts
                        for p in parts[1:]:
                            cleaned = re.sub(r'\s+', '', p)
                            if cleaned:
                                field_parts.append(cleaned)
                    else:
                        # 【v43.x】兜底：parts[0] 作为字段
                        field_parts = [re.sub(r'\s+', '', parts[0])] + [re.sub(r'\s+', '', p) for p in parts[1:]]

                # 获取业务对象信息
                bill_info = bill_name_to_info.get(bill_name, {}) if bill_name else {}
                field_path = desc  # 使用清理后的字段路径
                field_level = len(parts)

                # 判断是直接取值还是参照取值
                if field_level >= 3:
                    field_type = "参照取值"
                    # 步骤1：业务对象.参照字段
                    steps.append({
                        "step": 1,
                        "billName": bill_name,
                        "uri": bill_info.get("uri", ""),
                        "tableName": bill_info.get("tableName", ""),
                        "fieldName": field_parts[0] if field_parts else ""
                    })
                    # 后续步骤
                    for i, field in enumerate(field_parts[1:], start=2):
                        steps.append({
                            "step": i,
                            "billName": field_parts[i-2] if i-2 < len(field_parts) else "",
                            "uri": "",
                            "tableName": "",
                            "fieldName": field
                        })
                else:
                    field_type = "直接取值"
                    steps.append({
                        "step": 1,
                        "billName": bill_name,
                        "uri": bill_info.get("uri", ""),
                        "tableName": bill_info.get("tableName", ""),
                        "fieldName": ".".join(field_parts) if field_parts else original_desc
                    })
            else:
                # 无法解析
                field_path = original_desc
                field_type = "直接取值"
                field_level = 1
                steps.append({
                    "step": 1,
                    "billName": "",
                    "uri": "",
                    "tableName": "",
                    "fieldName": original_desc
                })
        else:
            # 无法解析的情况
            field_path = original_desc
            field_type = "直接取值"
            field_level = 1
            steps.append({
                "step": 1,
                "billName": "",
                "uri": "",
                "tableName": "",
                "fieldName": original_desc
            })

        return field_path, field_level, field_type, calculation_rule, calculation_rule_description, steps

    # 遍历原始表头列
    for col_idx, col_name in enumerate(header_row):
        col_name = str(col_name).strip()
        if not col_name or col_name in seen_columns:
            continue
        seen_columns.add(col_name)

        # 获取数据源描述（第一行）
        source_desc = ""
        if datasource_rows and col_idx < len(datasource_rows[0]):
            source_desc = str(datasource_rows[0][col_idx]).strip()

        # 解析字段路径
        field_path, field_level, field_type, calculation_rule, calculation_rule_description, steps = _parse_field_path(source_desc)

        # 判断是否计算字段
        is_calculated = calculation_rule in ["SUM", "AVG", "MAX", "MIN", "COUNT", "CASE WHEN", "FORMULA", "合计", "数量合计", "累计", "汇总", "平均值", "平均", "最大值", "最大", "最小值", "最小", "计数", "数量"]

        mapping = {
            "columnName": col_name,
            "columnIndex": col_idx,
            "sourceDescription": source_desc,
            "fieldPath": field_path,
            "fieldLevel": field_level,
            "fieldType": field_type,
            "calculationRule": calculation_rule,
            "calculationRuleDescription": calculation_rule_description,
            "steps": steps,
            "isCalculated": is_calculated
        }
        mappings.append(mapping)

    return mappings


# ============================================================
# 补充 relatedFields 和 queryCondition 字段（v11.0 新增）
# ============================================================

def _supplement_related_and_query_fields(
    column_field_mapping: List[Dict[str, Any]],
    bill_objects: List[Dict[str, Any]],
    query_info: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    补充 relatedFields 和 queryCondition 涉及的字段到 columnFieldMapping。

    【v11.0 新设计】：
    - 从 businessObjects[].relatedFields 提取字段，追加到 columnFieldMapping
    - 从 queryCondition.queryFieldsDetail 提取字段，追加到 columnFieldMapping
    - queryCondition 信息更完整，优先处理
    - 根据 columnName 去重，保留首次出现的记录
    - 为所有字段添加 mappingType 标识

    Args:
        column_field_mapping: 现有的表头字段映射
        bill_objects: 业务对象列表
        query_info: 查询条件信息，包含 queryFieldsDetail

    Returns:
        补充后的字段映射列表
    """
    if not column_field_mapping:
        column_field_mapping = []

    # 1. 先为现有的 reportColumn 添加 mappingType 字段
    for mapping in column_field_mapping:
        if "mappingType" not in mapping:
            mapping["mappingType"] = "reportColumn"

    # 已存在的 sourceDescription 集合（用于去重，基于"业务对象.字段名"）
    # 注意：不同业务对象的相同字段名不视为重复
    seen_sources: Set[str] = set()
    for mapping in column_field_mapping:
        src = mapping.get("sourceDescription", "")
        if src:
            seen_sources.add(src)

    # 构建业务对象名称到信息的映射
    bill_name_to_info = {}
    for bo in bill_objects:
        bn = bo.get("billName", "")
        if bn:
            bill_name_to_info[bn] = {
                "uri": bo.get("uri", ""),
                "tableName": bo.get("tableName", "")
            }

    # 业务对象名称后缀
    _BILL_SUFFIXES = ("主表", "子表", "单据", "单", "明细")

    new_mappings: List[Dict[str, Any]] = []
    mapping_source_stats = {
        "reportColumns": sum(1 for m in column_field_mapping if m.get("mappingType") == "reportColumn"),
        "relatedFields": 0,
        "queryConditions": 0,
        "duplicates": 0
    }

    def _create_mapping(
        column_name: str,
        bill_name: str,
        source_type: str,
        query_detail_index: int = -1
    ) -> Dict[str, Any]:
        """创建字段映射记录"""
        # 拼接 sourceDescription
        source_desc = f"{bill_name}.{column_name}" if bill_name else column_name

        # 构建 fieldPath
        field_path = source_desc
        field_level = 2 if bill_name else 1

        # 获取业务对象信息
        bill_info = bill_name_to_info.get(bill_name, {}) if bill_name else {}

        # 构建 steps
        steps = []
        if bill_name:
            steps.append({
                "step": 1,
                "billName": bill_name,
                "uri": bill_info.get("uri", ""),
                "tableName": bill_info.get("tableName", ""),
                "fieldName": column_name
            })

        result = {
            "columnName": column_name,
            "columnIndex": -1,  # 非表头字段，无列索引
            "sourceDescription": source_desc,
            "fieldPath": field_path,
            "fieldLevel": field_level,
            "fieldType": "直接取值",
            "calculationRule": "",
            "calculationRuleDescription": "",
            "mappingType": source_type,  # relatedField / queryCondition
            "steps": steps,
            "isCalculated": False
        }

        # queryCondition 类型的字段记录 queryDetailIndex
        if source_type == "queryCondition" and query_detail_index >= 0:
            result["queryDetailIndex"] = query_detail_index

        return result

    # 2. 先处理 queryCondition（信息更完整，优先添加）
    query_fields_detail = query_info.get("queryFieldsDetail", [])
    for idx, qf in enumerate(query_fields_detail):
        field_name = qf.get("fieldName", "").strip()
        if not field_name:
            continue

        # 去除括号内的字段名
        clean_name = re.sub(r'[（(].*?[）)]', '', field_name).strip()
        if not clean_name:
            continue

        # billName 可能带序号前缀（如"2、材料出库单主表"），需要清理
        bill_name_raw = qf.get("billName", "")
        bill_name = re.sub(r'^\d+[、.。]\s*', '', bill_name_raw).strip()

        # 构建 sourceDescription 用于去重（业务对象.字段名）
        source_desc = f"{bill_name}.{clean_name}" if bill_name else clean_name

        if source_desc in seen_sources:
            mapping_source_stats["duplicates"] += 1
            continue

        seen_sources.add(source_desc)
        mapping = _create_mapping(clean_name, bill_name, "queryCondition", idx)
        new_mappings.append(mapping)
        mapping_source_stats["queryConditions"] += 1

    # 3. 再处理 relatedFields（跳过已在 queryCondition 中添加的字段）
    _RELATED_FIELDS_DELIMITERS = re.compile(r'[、,，；;]')
    for bo in bill_objects:
        bn = bo.get("billName", "")
        related = bo.get("relatedFields", "") or ""
        if not related:
            continue

        # 按分隔符拆分字段
        parts = _RELATED_FIELDS_DELIMITERS.split(related)
        for part in parts:
            part = part.strip()
            if not part:
                continue

            # 去除括号内的字段名（如"成本金额（natMoney）"）
            clean_name = re.sub(r'[（(].*?[）)]', '', part).strip()
            if not clean_name:
                continue

            # 构建 sourceDescription 用于去重（业务对象.字段名）
            source_desc = f"{bn}.{clean_name}" if bn else clean_name

            if source_desc in seen_sources:
                mapping_source_stats["duplicates"] += 1
                continue

            seen_sources.add(source_desc)
            mapping = _create_mapping(clean_name, bn, "relatedField")
            new_mappings.append(mapping)
            mapping_source_stats["relatedFields"] += 1

    # 合并到原有列表
    if new_mappings:
        column_field_mapping.extend(new_mappings)
        logger.info(
            f"【v11.0】补充字段完成: "
            f"报表列={mapping_source_stats['reportColumns']}, "
            f"queryConditions={mapping_source_stats['queryConditions']}, "
            f"relatedFields={mapping_source_stats['relatedFields']}, "
            f"去重={mapping_source_stats['duplicates']}, "
            f"总计={len(column_field_mapping)}"
        )

    return column_field_mapping


# ============================================================
# Excel 读取
# ============================================================

_MAX_DATA_ROWS = 500


def _read_xlsx(path: Path, sheet_index: int) -> Tuple[List[List[str]], str]:
    wb = openpyxl.load_workbook(path, data_only=True)
    sheets = wb.worksheets
    if sheet_index < 0 or sheet_index >= len(sheets):
        raise ValueError(f"Sheet index {sheet_index} out of range (0-{len(sheets) - 1})")
    ws = sheets[sheet_index]
    sheet_name = ws.title or f"Sheet{sheet_index}"
    rows: List[List[str]] = []
    for row in ws.iter_rows(values_only=True):
        row_vals = []
        for cell in row:
            if cell is not None:
                val = str(cell)
                # 移除换行符，用空格替换
                val = val.replace('\n', ' ').replace('\r', ' ')
                row_vals.append(val)
            else:
                row_vals.append("")
        rows.append(row_vals)
        if len(rows) >= _MAX_DATA_ROWS:
            break
    wb.close()
    return rows, sheet_name


def _read_xls(path: Path, sheet_index: int) -> Tuple[List[List[str]], str]:
    def _open_xls_with_encoding_fallback(p: Path):
        # xlrd 读取 xls 文件时，中文编码通常是 gbk 或 cp1252
        # 优先尝试默认方式，然后用编码回退
        encodings = ["cp1252", "gbk", "gb2312", "utf-8", "latin-1"]
        for enc in encodings:
            try:
                return xlrd.open_workbook(str(p), encoding_override=enc)
            except Exception:
                continue
        # 最后尝试不带编码参数（使用系统默认）
        try:
            return xlrd.open_workbook(str(p))
        except Exception:
            raise RuntimeError(f"无法使用 xlrd 打开文件: {p}")

    wb = _open_xls_with_encoding_fallback(path)
    sheets = wb.sheets()
    if sheet_index < 0 or sheet_index >= len(sheets):
        raise ValueError(f"Sheet index {sheet_index} out of range")
    ws = sheets[sheet_index]
    sheet_name = wb.sheet_names()[sheet_index]
    rows: List[List[str]] = []
    for i in range(ws.nrows):
        if len(rows) >= _MAX_DATA_ROWS:
            break
        row_vals = []
        for j in range(ws.ncols):
            val = str(ws.cell_value(i, j))
            # 移除换行符，用空格替换
            val = val.replace('\n', ' ').replace('\r', ' ')
            row_vals.append(val)
        rows.append(row_vals)
    return rows, sheet_name


def _detect_excel_format(path: Path) -> str:
    """通过魔数检测真实Excel格式"""
    with open(path, "rb") as f:
        header = f.read(8)
    # OLE2 Compound Document (老版 .xls 格式)
    if header[:8] == b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1":
        return ".xls"
    # ZIP-based (新版 .xlsx 格式)
    if header[:4] == b"PK\x03\x04":
        return ".xlsx"
    # 回退到扩展名
    return path.suffix.lower()


def _read_excel(path: Path, sheet_index: int) -> Tuple[List[List[str]], str]:
    # 【修复】优先通过魔数检测真实格式，避免.xls文件被错误识别为.xlsx
    ext = _detect_excel_format(path)
    if ext == ".xlsx":
        if not _HAS_OPENPYXL:
            raise ImportError("openpyxl 未安装，请运行: pip install openpyxl")
        return _read_xlsx(path, sheet_index)
    elif ext == ".xls":
        if not _HAS_XLRD:
            raise ImportError("xlrd 未安装，请运行: pip install xlrd")
        return _read_xls(path, sheet_index)
    else:
        raise ValueError(f"不支持的格式: {ext}，仅支持 .xlsx 和 .xls")


# ============================================================
# 主解析入口
# ============================================================

def _is_numeric_string(s: str) -> bool:
    """判断字符串是否为纯数字（整数或浮点数）"""
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def _is_header_row(row: List[str]) -> bool:
    """
    判断是否为表头行。

    表头行特征：
    1. 非空单元格 >= 3
    2. 大部分单元格内容较短（< 15字符）
    3. 不像描述（不以"取"、"查询条件"、"从表"等开头）
    4. 不像数据（不是纯数字）
    5. 【v36.x 新增】包含常见的表头字段关键词
    """
    non_empty = [c.strip() for c in row if c.strip()]
    if len(non_empty) < 5:
        return False

    # 【v36.x 新增】优先检查是否包含表头关键词
    header_keywords = {
        '部门编号', '部门名称', '用电数量', '用电金额', '用生水数量', '用生水金额',
        '用软水数量', '用软水金额', '用汽数量', '用汽金额', '污水金额',
        '合计数量', '合计金额', '序号', '报表字段', '数据源', '说明',
        '编码', '名称', '数量', '金额', '日期', '状态'
    }
    keyword_count = sum(1 for c in non_empty if c in header_keywords)
    if keyword_count >= 2:
        return True

    # 统计短文本（可能是表头字段）的比例
    # 【v36.x 修改】提高阈值，要求至少 70% 是短文本
    short_count = sum(1 for c in non_empty if len(c) < 15 and not _is_numeric_string(c))
    if short_count < len(non_empty) * 0.7:
        return False

    # 排除描述性行（包含"取"、"查询条件"等关键词）
    desc_keywords = ('取', '查询条件', '查询：', '从表', '从', '基于', '根据',
                    '取值：', '取值:', '显示', '详见', '公式')
    desc_count = sum(1 for c in non_empty if c.startswith(desc_keywords))
    if desc_count > 0:
        return False

    return True


def _extract_columns(rows: List[List[str]]) -> List[str]:
    """
    提取列名（智能识别表头行）。

    策略：
    1. 优先找符合表头特征的短文本行
    2. 回退到第一个非空行
    """
    if not rows:
        return []

    # 优先策略：找表头行
    for row in rows[:5]:
        if _is_header_row(row):
            return [c.strip() for c in row]

    # 回退策略：第一行
    if rows:
        return [c.strip() for c in rows[0]]
    return []


# ============================================================
# v17.x 新增：字段描述提取
# ============================================================

# 中文引号字符 → 统一替换成英文引号 "
_CHINESE_QUOTES = str.maketrans({
    '“': '"',   # "
    '”': '"',   # "
    '‘': '"',   # '
    '’': '"',   # '
    '「': '"',   # 「
    '」': '"',   # 」
})


def _normalize_quotes(text: str) -> str:
    """统一将所有中文引号替换成英文引号"""
    return text.translate(_CHINESE_QUOTES)


# 字段描述提取的正则模式（使用英文引号）
_PAT_BILL_OF_FIELDS = re.compile(
    r'([一-龥]{2,}?(?:合同|单|单据|办理|签发|登记|主表|子表|明细|详情|申请|审批|核销|结算|报销|付款|收款|开票|收票|托收|贴现|背书|转让))中'
    r'的'
    r'(.+?)(?:[，,]显示|$)'
)

_PAT_FETCH_QUOTE = re.compile(
    r'取([一-龥]{2,}?(?:合同|单|单据|办理|签发|登记|主表|子表|明细|详情|申请|审批|核销|结算|报销|付款|收款|开票|收票|托收|贴现|背书|转让))的'
    r'"([^"]+)"'
)

_PAT_COLON_FETCH = re.compile(
    r'([一-龥]{2,}?(?:合同|单|单据|办理|签发|登记|主表|子表|明细|详情|申请|审批|核销|结算|报销|付款|收款|开票|收票|托收|贴现|背书|转让))[：:]取'
    r'"([^"]+)"'
)

_PAT_FETCH_ONLY = re.compile(r'取"([^"]+)"')

# 【v19.x 新增】匹配 "取xxx的yyy字段" 格式（如 "取材料出库单子表qty字段"）
_PAT_FETCH_FIELD = re.compile(
    r'取[一-龥]*?(?:单|主表|子表|明细)[的]?'
    r'([a-zA-Z_][a-zA-Z0-9_]{1,30})'
    r'(?:字段)?'
)

# 【v19.x 新增】匹配 "物料表的xxx" 格式
_PAT_TABLE_FIELD = re.compile(
    r'(?:表|表名)[的]([a-zA-Z_][a-zA-Z0-9_]{1,30})'
)

_PAT_QUOTE_CONTENT = re.compile(r'"([^"]+)"')

_PAT_ENGLISH_FIELD = re.compile(r'\b([a-z][a-zA-Z]{2,}[a-zA-Z0-9]*)\b')

_FIELD_DELIMITERS = ['、', ',', '，', ';', '；']


def _extract_fields_from_description(description: str) -> List[str]:
    """
    从字段描述中提取可能的字段名称。

    支持格式：
    - 授信合同中的资金组织
    - 取授信合同的"授信机构"
    - 授信合同：取"授信总额度"
    - "剩余未使用授信额度"+"已存保证金额度"
    """
    if not description:
        return []

    # 统一引号
    desc = _normalize_quotes(str(description).strip())
    if not desc:
        return []

    fields = []
    seen = set()

    def add_field(name: str) -> None:
        name = name.strip().strip('"')
        if not name or len(name) < 2 or len(name) > 30:
            return
        if name.isdigit():
            return
        if name.startswith(('详见', '公式', '示例', '显示')):
            return
        # 处理中英文混合（如 "授信机构"creditOrganAcc）
        has_en = _PAT_ENGLISH_FIELD.search(name)
        has_cn = any('一' <= c <= '鿿' for c in name)
        if has_en and has_cn:
            # 分离中英文
            for part in _PAT_ENGLISH_FIELD.split(name):
                part = part.strip().strip('"')
                if part and len(part) >= 2:
                    add_field(part)
            return
        if name not in seen:
            seen.add(name)
            fields.append(name)

    # 模式1: {业务对象}中的{字段}
    m = _PAT_BILL_OF_FIELDS.search(desc)
    if m:
        fields_part = m.group(2).strip()
        for delim in _FIELD_DELIMITERS:
            if delim in fields_part:
                for f in fields_part.split(delim):
                    add_field(f.strip())
                break
        else:
            add_field(fields_part)

    # 模式2: 取{业务对象}的"{字段}"
    for m in _PAT_FETCH_QUOTE.finditer(desc):
        add_field(m.group(2).strip())

    # 模式3: {业务对象}：取"{字段}"
    for m in _PAT_COLON_FETCH.finditer(desc):
        add_field(m.group(2).strip())

    # 模式4: 取"{字段}"
    for m in _PAT_FETCH_ONLY.finditer(desc):
        add_field(m.group(1).strip())

    # 【v19.x 新增】模式4a: 取{业务对象}的{英文字段}字段
    for m in _PAT_FETCH_FIELD.finditer(desc):
        add_field(m.group(1).strip())

    # 【v19.x 新增】模式4b: {表}的{英文字段} (如 "物料表的xxx")
    for m in _PAT_TABLE_FIELD.finditer(desc):
        add_field(m.group(1).strip())

    # 【v19.x 新增】模式4c: 取{业务对象}的{中文字段} (如 "取材料出库单主表的部门字段")
    _PAT_FETCH_CN_FIELD = re.compile(r'取[一-龥]{1,30}?的([一-龥]{2,10}?)(?:字段)?')
    for m in _PAT_FETCH_CN_FIELD.finditer(desc):
        add_field(m.group(1).strip())

    # 模式5: 引号内的内容
    for m in _PAT_QUOTE_CONTENT.finditer(desc):
        prefix_start = max(0, m.start() - 15)
        prefix = desc[prefix_start:m.start()]
        if any(c in prefix for c in '取：:'):
            add_field(m.group(1).strip())

    # 模式6: 英文字段名
    for m in _PAT_ENGLISH_FIELD.finditer(desc):
        en_field = m.group(1)
        start = m.start()
        end = m.end()
        has_cn_before = start > 0 and any('一' <= c <= '鿿' for c in desc[max(0, start-3):start])
        has_cn_after = end < len(desc) and any('一' <= c <= '鿿' for c in desc[end:min(len(desc), end+3)])
        if has_cn_before or has_cn_after:
            add_field(en_field)

    return fields


def _extract_field_descriptions(rows: List[List[str]], headers: List[str]) -> List[Dict[str, Any]]:
    """提取字段描述行中的信息"""
    field_descriptions = []
    if len(rows) < 2:
        return field_descriptions

    desc_row = rows[1] if len(rows) > 1 else []
    for col_idx, cell in enumerate(desc_row):
        cell_str = str(cell).strip() if cell else ''
        if not cell_str:
            continue
        display_name = headers[col_idx] if col_idx < len(headers) else ''
        extracted = _extract_fields_from_description(cell_str)
        field_descriptions.append({
            "columnIndex": col_idx,
            "displayName": display_name,
            "rule": cell_str,
            "extractedFields": extracted,
        })
    return field_descriptions


def parse_excel(
    path: Path,
    sheet_index: int = 0,
    report_index: int = -1,  # 【v36.x 新增】-1 表示提取所有报表，0+ 表示只提取指定报表（从 0 开始）
) -> Dict[str, Any]:
    """
    解析 Excel 报表模板文件，提取所有业务对象名称。

    核心逻辑：
    1. 读取所有数据行
    2. 找到"核心业务对象"列 → 按分隔符拆分该列所有单元格值
    3. 优先提取括号中的点分 URI（如 pc.product.Product）
    4. 原样返回用户填写的业务对象名称，不做任何白名单校验
    5. 去重、输出唯一业务对象列表 + URI 列表
    6. 按报表行聚合 → 输出每个报表对应的业务对象

    Args:
        path: Excel 文件路径
        sheet_index: 工作表索引
        report_index: 【v36.x 新增】报表索引，-1 表示所有报表，0+ 表示只提取指定报表

    Returns:
        {
          "excelFile": "...",
          "sheetIndex": 0,
          "sheetName": "...",
          "columns": [...],
          "dataSource": {
            "type": "billNames",
            "billNames": [...],       // 所有去重的业务对象名称（原样）
            "uriList": [...],         // 提取到的 URI 列表（来自括号中的点分格式）
            "rawMappings": [...]      // 每个报表对应的业务对象（含 uris 字段）
          },
          "allCells": [...],
          "raw_first_row": [...]
        }
    """
    if not path.exists():
        raise FileNotFoundError(f"Excel 文件不存在: {path}")

    rows, sheet_name = _read_excel(path, sheet_index)

    if not rows:
        return {
            "excelFile": str(path),
            "sheetIndex": sheet_index,
            "sheetName": sheet_name,
            "columns": [],
            "dataSource": None,
            "allCells": [],
            "raw_first_row": [],
        }

    # 提取列头（直接去除空值）
    columns = [c.strip() for c in _extract_columns(rows) if c.strip()]
    raw_first_row = rows[0] if rows else []

    # 找到表头行
    headers: List[str] = []
    header_row_idx = 0
    for i, row in enumerate(rows):
        non_empty = sum(1 for c in row if c.strip())
        if non_empty >= 3:
            headers = [c.strip() for c in row]
            header_row_idx = i
            break

    if not headers:
        logger.warning(
            f"解析完成: {path} | sheet={sheet_name} | "
            f"未找到表头行，未提取到业务对象名称"
        )
        return {
            "excelFile": str(path),
            "sheetIndex": sheet_index,
            "sheetName": sheet_name,
            "columns": columns,
            "dataSource": {"type": "billNames", "billNames": [], "uriList": [], "dataSourceFields": [], "rawMappings": []},
            "allCells": rows[:_MAX_DATA_ROWS],
            "raw_first_row": raw_first_row,
        }

    # 确定哪些列需要排除（描述性列）
    exclude_cols: Set[int] = set()
    for j, h in enumerate(headers):
        h_lower = h.lower()
        for kw in _EXCLUDE_COL_HEADERS:
            if kw in h or h in kw:
                exclude_cols.add(j)
                break

    # 找关键列索引
    billname_col = _find_core_billname_col(headers)
    # 【v36.x 修复】兼容两种特殊结构：
    # 结构1：表头列0是"业务对象"标签，业务对象在列1（如天山纺织 Excel）
    # 结构2：表头列0是"业务对象"标签，但表头行没有"业务对象"列
    _found_bill_objects_row = -1  # 记录"业务对象"标签所在的行索引
    _bill_objects_row_idx = -1  # 记录业务对象内容所在的行索引
    _is_simple_format = False  # 【v36.x 新增】是否是简单格式（列头即字段，无业务对象列）

    if header_row_idx + 1 < len(rows):
        first_data_row = rows[header_row_idx + 1]
        if first_data_row and len(first_data_row) > 0:
            first_col_val = str(first_data_row[0]).strip() if first_data_row[0] else ""
            if first_col_val == "业务对象":
                _found_bill_objects_row = header_row_idx + 1
                _bill_objects_row_idx = header_row_idx + 1
                # 【v36.x 新增】扫描后续行，找到业务对象内容行（A列不是"业务对象"但B列有内容）
                for scan_idx in range(header_row_idx + 2, min(header_row_idx + 10, len(rows))):
                    scan_row = rows[scan_idx]
                    if scan_row and len(scan_row) > 0:
                        a_val = str(scan_row[0]).strip() if scan_row[0] else ""
                        # 如果A列不是"业务对象"且有其他内容，或者A列是数字（序号）
                        if a_val and a_val != "业务对象":
                            # 检查B列是否有业务对象内容
                            if len(scan_row) > 1 and scan_row[1]:
                                b_val = str(scan_row[1]).strip()
                                if b_val and any(sn in b_val for sn in ["主表", "子表", "单", "单据", "表"]):
                                    _bill_objects_row_idx = scan_idx
                                    logger.info(f"[v36.x] 扫描找到业务对象行 Row {scan_idx+1}: A='{a_val}', B='{b_val[:30]}...'")
                                    break
                # 确认表头列0不是"业务对象"
                if billname_col != 0 or headers[0] != "业务对象":
                    billname_col = 1
                    logger.info(f"[v36.x] 检测到特殊结构：'业务对象'标签在数据行，使用列1作为业务对象列")
            else:
                # 【v36.x 新增】简单格式检测：列头直接是字段名，Row+1 是数据源说明
                # 检测方法：列头中有"数量"、"金额"等常见字段名，且 Row+1 的内容包含"取XXX"
                has_field_headers = any(kw in str(h).lower() for h in headers for kw in ["数量", "金额", "编号", "名称", "日期"])
                has_datasource_hint = any("取" in str(cell) for cell in first_data_row if cell)
                if has_field_headers and has_datasource_hint and not first_col_val:
                    _is_simple_format = True
                    logger.info(f"[v36.x] 检测到简单格式：列头是字段名，数据源说明在第 {header_row_idx + 2} 行")

    # 【v36.x 新增】提前初始化变量，避免 UnboundLocalError
    # 有效字段列表（用于字段语义匹配）
    effective_fields: List[str] = []
    # 全局去重业务对象名称
    all_billnames: List[str] = []
    # 全局去重 URI 列表（来自括号中的点分格式）
    all_uris: List[str] = []
    # 【v5.1 新增】全局去重数据源字段
    all_data_source_fields: List[str] = []

    # 【v36.x 新增】如果检测到简单格式，从数据源说明行提取业务对象
    if _is_simple_format and header_row_idx + 1 < len(rows):
        datasource_row = rows[header_row_idx + 1]  # Row+1 是数据源说明行
        # 从每列的数据源说明中提取业务对象
        for col_idx, cell in enumerate(datasource_row):
            cell_str = str(cell).strip() if cell else ""
            if cell_str and "取" in cell_str:
                # 提取"取XXX"中的业务对象名称（贪婪匹配后截断到 suffix 边界）
                # 【v37.x 修复】完整匹配业务对象+suffix，然后截断suffix之后的内容
                m = re.search(r"取([一-龥]+(?:主表|子表|单据|单|表|档案)?)", cell_str)
                if m:
                    raw = m.group(1)
                    # 如果匹配到 suffix，截断到 suffix 之后（如 "材料出库单主表对应的..." → "材料出库单主表"）
                    billname = raw
                    for suffix in ["主表", "子表", "单据", "单", "表", "档案"]:
                        idx = raw.find(suffix)
                        if idx >= 0:
                            billname = raw[:idx + len(suffix)]
                            break
                    # 【v37.x 过滤】业务对象名必须包含 "单" 或 "档案" 等核心关键词
                    if (billname and billname not in all_billnames
                            and len(billname) >= 3
                            and any(kw in billname for kw in ["单", "档案"])):
                        all_billnames.append(billname)
                        logger.info(f"[v37.x] 从数据源说明提取业务对象: {billname}")
                # 添加数据源说明到 effectiveFields
                if cell_str and cell_str not in effective_fields:
                    effective_fields.append(cell_str)

    # 【v36.x 新增】扫描所有行，找到A列是"业务对象"的行（兼容不规则结构）
    if not _is_simple_format:
        for scan_row_idx, scan_row in enumerate(rows):
            if scan_row_idx <= header_row_idx:  # 跳过表头行之前的行
                continue
            if scan_row and len(scan_row) > 0:
                a_val = str(scan_row[0]).strip() if scan_row[0] else ""
                if a_val == "业务对象":
                    _found_bill_objects_row = scan_row_idx
                    # 检查B列是否有业务对象内容
                    if len(scan_row) > 1 and scan_row[1]:
                        b_val = str(scan_row[1]).strip()
                        if b_val and (any(sn in b_val for sn in ["主表", "子表", "单", "单据", "表", "档案", "记录"]) or "(" in b_val):
                            _bill_objects_row_idx = scan_row_idx
                            billname_col = 1
                            logger.info(f"[v36.x] 扫描全表找到业务对象行 Row {scan_row_idx+1}: '{b_val[:50]}...'")
                            break
    report_name_col = _find_report_name_col(headers)
    report_no_col = -1
    for j, h in enumerate(headers):
        h = h.strip()
        if h == "报表编号" or h == "编号":
            report_no_col = j
            break

    # 找"数据源字段"列（用于过滤参照元数据）
    datasource_field_col = _find_datasource_field_col(headers)

    # 提取所有数据行（表头行之后）
    data_rows = rows[header_row_idx + 1:]

    # 每行（报表）的业务对象
    raw_mappings: List[Dict[str, Any]] = []

    # 【v36.x 新增】找出"数据源"列的索引（用于提取字段引用）
    datasource_col = -1
    if columns:
        for j, col in enumerate(columns):
            col_str = str(col).strip()
            if col_str == "数据源":
                datasource_col = j
                break

    # 【v36.x 新增】检测是否使用了"业务对象"标签格式
    # 如果有"业务对象"标签行，说明是特殊格式，只需要从这些行提取，不需要扫描其他列
    has_business_object_label = False
    business_object_label_rows = []  # 记录"业务对象"标签行及后续关联行

    # 第一遍扫描：找出所有"业务对象"标签行及其关联行
    current_report_idx = 0  # 当前报表索引
    for row_idx, row in enumerate(data_rows):
        a_cell = str(row[0]).strip() if row and len(row) > 0 and row[0] else ""
        b_cell = str(row[1]).strip() if row and len(row) > 1 and row[1] else ""
        # 检测是否进入新报表（通过 A 列包含"报表"关键词）
        if "报表" in a_cell and "：" in a_cell or "报表" in a_cell and ":" in a_cell:
            current_report_idx += 1
        if a_cell == "业务对象" and b_cell:
            has_business_object_label = True
            # 收集该标签行及后续关联的业务对象行
            section_rows = [(row_idx, row, a_cell, b_cell, current_report_idx)]
            for next_row_idx in range(row_idx + 1, len(data_rows)):
                next_row = data_rows[next_row_idx]
                next_a = str(next_row[0]).strip() if next_row and len(next_row) > 0 and next_row[0] else ""
                next_b = str(next_row[1]).strip() if next_row and len(next_row) > 1 and next_row[1] else ""
                # 如果 A 列是数字（序号），说明进入了字段行，停止扫描
                if next_a and next_a.isdigit():
                    break
                # 如果 A 列是其他非空内容但 B 列有业务对象相关关键词，收集
                if next_b and any(sn in next_b for sn in ["主表", "子表", "单", "单据", "表", "档案", "记录", "余额"]):
                    section_rows.append((next_row_idx, next_row, next_a, next_b, current_report_idx))
                # 如果 A 列是空且 B 列没有业务对象关键词，继续（可能是跨行描述）
            business_object_label_rows.append(section_rows)
            logger.info(f"发现'业务对象'标签区域：报表{current_report_idx}，从第 {row_idx} 行开始，包含 {len(section_rows)} 行")

    # 【v36.x 新增】如果使用了"业务对象"标签格式，只提取这些行，不扫描其他列
    if has_business_object_label:
        for section in business_object_label_rows:
            # section 的元素格式: (row_idx, row, a_cell, b_cell, report_idx)
            # 获取该 section 对应的报表索引
            section_report_idx = section[0][4] if section else 0
            # 如果指定了 report_index，过滤掉不匹配的 section
            if report_index >= 0 and section_report_idx != report_index:
                continue
            for item in section:
                row_idx, row, a_cell, b_cell, _ = item
                if a_cell == "业务对象":  # 主标签行
                    bill_part, uri_part = _split_billnames_raw(b_cell)
                    for name in bill_part:
                        if name not in all_billnames:
                            all_billnames.append(name)
                    for uri in uri_part:
                        if uri not in all_uris:
                            all_uris.append(uri)
                else:  # 关联行
                    bill_part, uri_part = _split_billnames_raw(b_cell)
                    for name in bill_part:
                        if name not in all_billnames:
                            all_billnames.append(name)
                    for uri in uri_part:
                        if uri not in all_uris:
                            all_uris.append(uri)
            # 【v36.x 新增】从业务对象区域后面的数据行提取"数据源"列的字段引用
            # 找到业务对象区域后面的第一行（字段数据行）
            section_end_row = section[-1][0] if section else 0
            # 找到该 section 对应的报表范围（直到下一个报表行或空行）
            # 计算下一个报表的起始行
            next_report_start = len(data_rows)
            for check_idx in range(section_end_row + 1, len(data_rows)):
                check_row = data_rows[check_idx]
                if not check_row:
                    continue
                check_a = str(check_row[0]).strip() if check_row[0] else ""
                if "报表" in check_a and ("：" in check_a or ":" in check_a):
                    next_report_start = check_idx
                    break
            # 扫描字段行
            for next_row_idx in range(section_end_row + 1, next_report_start):
                next_row = data_rows[next_row_idx]
                if not next_row:
                    continue
                # 从"数据源"列提取字段引用
                if datasource_col >= 0 and datasource_col < len(next_row):
                    datasource_value = str(next_row[datasource_col]).strip()
                    if datasource_value:
                        # 直接添加原始字段引用
                        if datasource_value not in all_data_source_fields:
                            all_data_source_fields.append(datasource_value)
        # 完成提取，跳过后续的普通列扫描
        logger.info(f"特殊格式解析完成：共提取 {len(all_billnames)} 个业务对象（report_index={report_index}）")
        if all_data_source_fields:
            logger.info(f"从'数据源'列提取到 {len(all_data_source_fields)} 个字段引用")
        # 继续执行到 v2_result 构建部分
    else:
        # 普通格式：按原有逻辑扫描所有数据行
        for row_idx, row in enumerate(data_rows):
            row_billnames: Set[str] = set()
            row_uris: Set[str] = set()
            actual_row_idx = header_row_idx + 1 + row_idx  # 在原 rows 中的索引

            # 方法1: 优先从"核心业务对象"列提取（最高优先级）
            # 【v11.2 优化】直接使用解析结果，不做白名单过滤，保留用户原始填写名称
            if billname_col >= 0 and billname_col < len(row):
                cell = row[billname_col].strip()
                # 【v11.3 修复】特殊结构：列0是标签"业务对象"，列1才是业务对象名称
                if cell == "业务对象" and billname_col == 0 and billname_col + 1 < len(row):
                    cell = row[billname_col + 1].strip()
                if cell:
                    bill_part, uri_part = _split_billnames_raw(cell)
                    for name in bill_part:
                        row_billnames.add(name)
                    row_uris.update(uri_part)

            # 方法2: 扫描其他列（排除描述性列），识别可能的业务对象名称
            # 【v11.2 优化】直接使用解析结果，不做白名单过滤
            for j, cell in enumerate(row):
                if j in exclude_cols:
                    continue
                if j == billname_col:
                    continue
                cell = cell.strip()
                if not cell:
                    continue
                bill_part, uri_part = _split_billnames_raw(cell)
                for name in bill_part:
                    # 【v44.x 修复】排除纯下划线格式的表名（如 st_materialouts）
                    # 这类值来自"表名"列，不是业务对象名称
                    if name and "_" in name and not any('一' <= c <= '鿿' for c in name):
                        continue
                    row_billnames.add(name)
                row_uris.update(uri_part)

        # 【v5.1 新增】方法3: 从"数据源字段"列提取，按分隔符拆分并去重
        row_datasource_fields: Set[str] = set()
        if datasource_field_col >= 0 and datasource_field_col < len(row):
            cell = row[datasource_field_col].strip()
            if cell:
                bill_part, _ = _split_billnames_raw(cell)
                for name in bill_part:
                    name = name.strip()
                    if name and name not in row_datasource_fields:
                        row_datasource_fields.add(name)

        # 收集报表名称
        report_name = ""
        if report_name_col >= 0 and report_name_col < len(row):
            report_name = row[report_name_col].strip()

        report_no = ""
        if report_no_col >= 0 and report_no_col < len(row):
            report_no = row[report_no_col].strip()

        if row_billnames or row_uris:
            # 合并业务对象名到全局
            for bn in row_billnames:
                if bn not in all_billnames:
                    all_billnames.append(bn)
            # 合并 URI 到全局
            for uri in row_uris:
                if uri not in all_uris:
                    all_uris.append(uri)
            # 【v5.1 新增】合并数据源字段到全局
            for dsf in row_datasource_fields:
                if dsf not in all_data_source_fields:
                    all_data_source_fields.append(dsf)
            raw_mappings.append({
                "rowIndex": header_row_idx + row_idx + 1,
                "reportNo": report_no,
                "reportName": report_name,
                "billNames": sorted(row_billnames),
                "uris": sorted(row_uris),
                "dataSourceFields": sorted(row_datasource_fields),  # 【v5.1 新增】
            })

    # 构建 dataSource
    # 【v6.0 新增】支持"使用到的业务对象"格式
    section_idx, bill_objects = _find_bill_objects_section(rows)
    if bill_objects:
        for bo in bill_objects:
            bn = bo.get("billName", "")
            uri = bo.get("uri", "")
            if bn and bn not in all_billnames:
                all_billnames.append(bn)
            if uri and uri not in all_uris:
                all_uris.append(uri)
        logger.info(f"从'使用到的业务对象'区域提取到 {len(bill_objects)} 个业务对象: {[bo.get('billName') for bo in bill_objects]}, URI: {[bo.get('uri') for bo in bill_objects]}")

    # 【v9.0 新增】支持"查询条件"区域
    query_idx, query_info = _find_query_condition_section(rows)
    if query_info.get("queryFieldsDetail") or query_info.get("defaultQuery"):
        logger.info(f"从'查询条件'区域提取到 {len(query_info.get('queryFieldsDetail', []))} 个查询字段")

    # 【v10.0 新增】提取表头与字段的映射关系
    column_field_mapping = _extract_column_field_mapping(rows, columns, headers, bill_objects)
    if column_field_mapping:
        logger.info(f"提取到 {len(column_field_mapping)} 个表头字段映射")

        # 【v44.x 新增】从 column_field_mapping 的 steps 中提取业务对象（只取第一步的 xxx）
        # 因为 xxx.yyy.zzz 格式中 xxx 一定是业务对象，后续步骤是参照链字段
        for mapping in column_field_mapping:
            steps = mapping.get("steps", [])
            if steps and len(steps) > 0:
                # 只取第一步的 billName（xxx 部分）
                bill_name = steps[0].get("billName", "")
                if bill_name and bill_name not in all_billnames:
                    all_billnames.append(bill_name)
                    logger.info(f"[v44.x] 从字段路径提取业务对象: {bill_name}")

    # 【v11.0 新增】补充 relatedFields 和 queryCondition 字段到 columnFieldMapping
    column_field_mapping = _supplement_related_and_query_fields(
        column_field_mapping, bill_objects, query_info
    )

    # 【v8.3 新增】从"取值规则"段落提取业务对象
    rule_billnames = _extract_billnames_from_rules(rows)
    if rule_billnames:
        for bn in rule_billnames:
            if bn and bn not in all_billnames:
                all_billnames.append(bn)
        logger.info(f"从'取值规则'段落提取到 {len(rule_billnames)} 个业务对象: {rule_billnames}")

    data_source: Dict[str, Any] = {
        "type": "billNames",
        "billNames": all_billnames,
        "uriList": all_uris,
        "dataSourceFields": all_data_source_fields,  # 【v5.1 新增】用于过滤参照元数据
        "rawMappings": raw_mappings,
        "billObjects": bill_objects,  # 【v6.0 新增】含关系描述的业务对象列表
        "queryCondition": query_info,  # 【v9.0 新增】查询条件信息
    }

    # 日志输出
    if all_billnames or all_uris:
        logger.info(
            f"解析完成: {path} | sheet={sheet_name} | "
            f"提取到 {len(all_billnames)} 个业务对象: {all_billnames}"
        )
        if all_uris:
            logger.info(f"  提取到 {len(all_uris)} 个 URI: {all_uris}")
        if all_data_source_fields:
            logger.info(f"  【v5.1】提取到 {len(all_data_source_fields)} 个数据源字段: {all_data_source_fields}")
        if query_info.get("queryFields"):
            logger.info(f"  【v9.0】提取到 {len(query_info['queryFields'])} 个查询字段: {query_info['queryFields']}")
        if query_info.get("defaultQuery"):
            logger.info(f"  【v9.0】默认查询条件: {query_info['defaultQuery'][:100]}...")
        if raw_mappings:
            logger.info(f"  共 {len(raw_mappings)} 个报表，报表-业务对象映射: ")
            for mapping in raw_mappings:
                uri_info = f" | URIs={mapping['uris']}" if mapping.get("uris") else ""
                dsf_info = f" | 数据源字段={mapping['dataSourceFields']}" if mapping.get("dataSourceFields") else ""
                logger.info(
                    f"    [{mapping['reportNo']}] {mapping['reportName']} → {mapping['billNames']}{uri_info}{dsf_info}"
                )
    else:
        logger.warning(
            f"解析完成: {path} | sheet={sheet_name} | "
            f"未提取到业务对象名称，请检查 Excel 列头是否包含'核心业务对象'"
        )

    # allCells: 最多 500 行原始数据
    all_cells = rows[:_MAX_DATA_ROWS]

    # 【v17.x 新增】合并 effectiveFields（保持表头顺序）
    # 1. 从表头提取字段（保持顺序）
    effective_fields: List[str] = []
    for col in columns:
        col_str = str(col).strip()
        if col_str and col_str not in effective_fields:
            effective_fields.append(col_str)

    # 2. 从描述行（第2行）提取字段（追加到表头字段后面）
    if rows and len(rows) > 1:
        field_descriptions = _extract_field_descriptions(rows, headers)
        for fd in field_descriptions:
            for field_name in fd.get("extractedFields", []):
                if field_name and field_name not in effective_fields:
                    effective_fields.append(field_name)

    # 3. 从 bill_objects 的 relatedFields 提取字段（按中文分隔符拆分）
    _RELATED_FIELDS_DELIMITERS = re.compile(r'[、,，；;]')
    for bo in bill_objects:
        related = bo.get("relatedFields", "") or ""
        if related:
            parts = _RELATED_FIELDS_DELIMITERS.split(related)
            for part in parts:
                part = part.strip()
                if part and part not in effective_fields:
                    effective_fields.append(part)

    # 【v36.x 新增】4. 从"数据源"列提取的字段引用也添加到 effectiveFields
    if all_data_source_fields:
        for dsf in all_data_source_fields:
            if dsf and dsf not in effective_fields:
                effective_fields.append(dsf)

    # 【v44.x 新增】从 columnFieldMapping 提取所有 fieldName，用于 fetch_metadata.py 字段过滤
    # fieldName 在 steps 数组中每个 step 的属性里
    column_field_names: List[str] = []
    for mapping in column_field_mapping:
        steps = mapping.get("steps", [])
        for step in steps:
            field_name = step.get("fieldName", "")
            if field_name and field_name not in column_field_names:
                column_field_names.append(field_name)

    # 【v36.x 修复】构建 v2.0 格式输出（兼容 fetch_metadata.py 的 --excel-parsed 参数）
    # 同时包含 _find_bill_objects_section 结果和主解析循环提取的结果
    v2_business_objects = list(bill_objects)
    # 如果主循环提取到了业务对象但 _find_bill_objects_section 没找到，需要补充
    for bn in all_billnames:
        found = any(bo.get("billName") == bn for bo in v2_business_objects)
        if not found:
            v2_business_objects.append({"billName": bn, "source": "main_loop"})

    # 【v36.x 新增】构建 dataSource（包含从"数据源"列提取的字段引用）
    data_source: Dict[str, Any] = {
        "type": "billNames",
        "billNames": all_billnames,
        "uriList": all_uris,
        "dataSourceFields": all_data_source_fields,  # 从"数据源字段"列提取
        "queryCondition": query_info,  # 【v9.0 新增】查询条件信息
    }

    # 【v9.2 新增】将 Excel 内容转换成 Markdown 表格格式
    def _to_markdown_table(rows: List[List[str]]) -> str:
        """将 Excel 行数据转换为 Markdown 表格格式"""
        if not rows:
            return ""

        # 过滤空行
        data_rows = [row for row in rows if any(str(cell).strip() for cell in row)]
        if not data_rows:
            return ""

        # 计算每列的最大宽度
        col_widths = []
        for row in data_rows:
            for col_idx, cell in enumerate(row):
                cell_str = str(cell).strip()
                width = len(cell_str)
                if col_idx >= len(col_widths):
                    col_widths.append(width)
                else:
                    col_widths[col_idx] = max(col_widths[col_idx], width)

        # 生成 Markdown 表格
        lines = []
        for row_idx, row in enumerate(data_rows):
            # 处理单元格内容
            cells = []
            for col_idx, cell in enumerate(row):
                cell_str = str(cell).strip()
                width = col_widths[col_idx] if col_idx < len(col_widths) else len(cell_str)
                # 左对齐，总宽度包含实际内容
                cells.append(cell_str)

            # 第一行作为表头
            lines.append("| " + " | ".join(cells) + " |")

            # 第二行添加分隔符（仅当有数据时）
            if row_idx == 0:
                separators = []
                for col_idx in range(len(data_rows[0])):
                    width = col_widths[col_idx] if col_idx < len(col_widths) else 3
                    separators.append("-" * max(width, 3))
                lines.append("| " + " | ".join(separators) + " |")

        return "\n".join(lines)

    excel_all_data = _to_markdown_table(rows)

    v2_result = {
        "version": "2.0",
        "sheets": [{
            "sheetIndex": sheet_index,
            "sheetName": sheet_name,
            "columns": columns,
            "effectiveFields": effective_fields,
            "dataSource": data_source,  # 【v36.x 新增】用于 fetch_metadata.py 提取字段
            "sections": {
                "businessObjects": {
                    "items": v2_business_objects  # 包含两种来源的业务对象
                }
            },
            "columnFieldMapping": column_field_mapping,  # 【v10.0 新增】表头与字段映射
            "_columnFieldNames": column_field_names,  # 【v44.x 新增】所有字段名，用于 fetch 过滤
            "_columnFieldMappings": column_field_mapping,  # 【v45.x 新增】完整字段映射（含 steps），用于多级参照展开
            "excelAllData": excel_all_data,  # 【v9.2 新增】Excel全部文字内容
        }]
    }

    # 【v9.1 新增】构建包含Excel整体信息的外层结构
    excel_result = {
        "excelFile": str(path),                          # Excel文件完整路径
        "excelName": path.name,                          # Excel文件名
        "sheetCount": len(v2_result["sheets"]),         # 工作表数量
        "version": v2_result["version"],                 # JSON版本号
        "sheets": v2_result["sheets"],                  # 工作表数组
    }

    return excel_result


# ============================================================
# CLI
# ============================================================

def main() -> int:
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        description="解析 Excel 报表模板，提取列名和数据源信息（业务对象名称）"
    )
    parser.add_argument("excel_file", type=Path, help="Excel 报表模板路径")
    parser.add_argument(
        "--sheet-index", "-i", type=int, default=0, dest="sheet_index",
        help="工作表索引，从 0 开始（默认 0）",
    )
    parser.add_argument(
        "--output", "-o", choices={"json", "columns", "billnames"},
        default="json",
        help="输出格式：json（完整结果）、columns（仅列名）、billnames（仅业务对象列表）",
    )
    parser.add_argument(
        "--no-verbose", action="store_true",
        help="禁止详细日志",
    )
    parser.add_argument(
        "--report-index", "-r", type=int, default=-1, dest="report_index",
        help="【v36.x】报表索引，-1 表示所有报表，0+ 表示只提取指定报表（从 0 开始）",
    )
    parser.add_argument(
        "--require-billnames", action="store_true", dest="require_billnames",
        help="【v13.x】强制要求提取到业务对象，为空时抛出异常（不建议跳过，因为后续阶段依赖业务对象）",
    )
    parser.add_argument(
        "--output-json", action="store_true", dest="output_json",
        help="【v45.x】将解析结果输出到 output/ 目录，文件名格式: parse_ExcelName_Timestamp.json",
    )
    parser.add_argument(
        "--output-dir", type=str, default="output", dest="output_dir",
        help="【v45.x】输出目录路径（默认: output）",
    )
    args = parser.parse_args()

    try:
        result = parse_excel(args.excel_file, args.sheet_index, args.report_index)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except ImportError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    # 【v13.x 强制校验】业务对象必须有值，否则后续阶段无法执行
    if args.require_billnames:
        ds = result.get("dataSource") or {}
        bill_names = ds.get("billNames") or []
        uri_list = ds.get("uriList") or []
        if not bill_names and not uri_list:
            print(f"错误: Excel模板 '{args.excel_file}' 未提取到业务对象，无法继续执行！", file=sys.stderr)
            print(f"  可能原因：", file=sys.stderr)
            print(f"  1. Excel缺少'使用到的业务对象'区域", file=sys.stderr)
            print(f"  2. 列头不包含'核心业务对象'等关键词", file=sys.stderr)
            print(f"  3. 业务对象名称格式不符合解析规则", file=sys.stderr)
            print(f"  建议：请检查Excel模板格式，参考标准模板添加'使用到的业务对象'区域", file=sys.stderr)
            return 1

    if args.output == "columns":
        for col in result["columns"]:
            if col:
                print(col)
    elif args.output == "billnames":
        ds = result.get("dataSource")
        if ds and (ds.get("billNames") or ds.get("uriList")):
            print("业务对象列表:")
            for bn in ds.get("billNames", []):
                print(f"  - {bn}")
            if ds.get("uriList"):
                print("\nURI 列表（可直接用 --query-uri）:")
                for uri in ds["uriList"]:
                    print(f"  - {uri}")
            if ds.get("dataSourceFields"):
                print("\n数据源字段（用于过滤参照元数据，v5.1）:")
                for dsf in ds["dataSourceFields"]:
                    print(f"  - {dsf}")
            total_bills = len(ds.get("billNames", []))
            total_uris = len(ds.get("uriList", []))
            parts = []
            if total_bills:
                parts.append(f"{total_bills} 个业务对象")
            if total_uris:
                parts.append(f"{total_uris} 个 URI")
            print(f"\n共 {'，'.join(parts)}")
            if ds.get("rawMappings"):
                print(f"\n报表-业务对象映射（共 {len(ds['rawMappings'])} 个报表）:")
                for m in ds["rawMappings"]:
                    uri_part = f" | URIs={m['uris']}" if m.get("uris") else ""
                    dsf_part = f" | 数据源字段={m['dataSourceFields']}" if m.get("dataSourceFields") else ""
                    print(f"  [{m['reportNo']}] {m['reportName']} → {', '.join(m['billNames'])}{uri_part}{dsf_part}")
        else:
            print("未提取到业务对象")
    else:
        # 【v45.x 新增】输出到文件
        if args.output_json:
            output_dir = Path(args.output_dir)
            # 相对于当前工作目录解析路径
            if not output_dir.is_absolute():
                output_dir = Path.cwd() / output_dir

            # 创建输出目录
            output_dir.mkdir(parents=True, exist_ok=True)

            # 生成文件名：parse_{ExcelName}_{Timestamp}.json
            excel_name = args.excel_file.stem
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = output_dir / f"parse_{excel_name}_{timestamp}.json"

            # 写入文件
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            logger.info(f"【v45.x】解析结果已输出到: {output_file}")

            # stdout 仍然输出 JSON（保持原有行为）
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
