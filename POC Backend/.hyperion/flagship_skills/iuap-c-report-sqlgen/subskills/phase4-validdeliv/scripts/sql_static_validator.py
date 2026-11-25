# -*- coding: utf-8 -*-
"""
SQL 静态规则校验模块

在数据库校验之前，对 SQL 进行静态规则校验：
- 业务对象（主表）校验：schema.tableName
- 参照实体校验：schema.tableName
- 特征实体校验：直接表名（无schema）
- 子表校验：schema.tableName
- 字段名存在性检查（基于元数据）
- 语法规则检查
- 业务规则检查

无需数据库连接，快速定位简单错误。
"""

import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field

# ============================================================
# 正则表达式预编译（提升性能）
# ============================================================
_FROM_TABLE_PATTERN = re.compile(r'\bFROM\s+([a-zA-Z_][a-zA-Z0-9_.]*)', re.IGNORECASE)
_JOIN_TABLE_PATTERN = re.compile(r'\bJOIN\s+([a-zA-Z_][a-zA-Z0-9_.]*)', re.IGNORECASE)
_SELECT_CLAUSE_PATTERN = re.compile(r'\bSELECT\s+(.*?)\s+FROM', re.IGNORECASE | re.DOTALL)
_CONDITION_FIELD_PATTERN = re.compile(r'\b(?:WHERE|ON|AND|OR)\s+([a-zA-Z_][a-zA-Z0-9_.]*)\s*[=<>!]', re.IGNORECASE)
_CTE_PATTERN = re.compile(r'\bWITH\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+AS', re.IGNORECASE)
_MULTI_CTE_PATTERN = re.compile(r',\s*([a-zA-Z_][a-zA-Z0-9_]*)\s+AS', re.IGNORECASE)
_TENANT_PATTERN = re.compile(r"ytenant_id\s*=\s*'[^v][^a][^r]([^']+)'", re.IGNORECASE)
_PARAM_PATTERN = re.compile(r"param\$\([^)]+\)")
_VAR_PATTERN = re.compile(r"var\$\([^)]+\)")
# 匹配 schema.table 或纯 tableName
_FULL_TABLE_PATTERN = re.compile(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\b')


@dataclass
class StaticValidationResult:
    """静态校验结果"""
    ok: bool = True
    errors: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[Dict[str, Any]] = field(default_factory=list)
    fixes_applied: List[Dict[str, Any]] = field(default_factory=list)
    fixed_sql: Optional[str] = None


@dataclass
class ValidationRule:
    """校验规则定义"""
    name: str
    pattern: str
    check_type: str  # 'table', 'field', 'syntax', 'business'
    fix_action: Optional[str] = None
    fix_value: Optional[str] = None
    severity: str = "error"  # 'error', 'warning'


# ============================================================
# 元数据加载（从 entities.json）
# ============================================================

_entities_cache: Optional[Dict[str, Any]] = None
_entities_cache_path: Optional[Path] = None


def load_entities_metadata(entities_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    加载元数据 entities.json

    Args:
        entities_path: entities.json 文件路径

    Returns:
        元数据字典，包含所有业务对象的表名和字段信息
    """
    global _entities_cache, _entities_cache_path

    if entities_path is None:
        # 默认路径
        entities_path = Path(__file__).parent.parent.parent.parent / "output" / "entities.json"

    if _entities_cache is not None and _entities_cache_path == entities_path:
        return _entities_cache

    if not entities_path.exists():
        return {}

    try:
        with open(entities_path, "r", encoding="utf-8") as f:
            _entities_cache = json.load(f)
            _entities_cache_path = entities_path
            return _entities_cache
    except Exception:
        return {}


@dataclass
class TableMetadata:
    """表元数据"""
    table_name: str  # 纯表名
    schema: str  # schema 名
    table_type: str  # 'business', 'reference', 'character', 'detail'
    bill_name: str  # 业务对象名称
    uri: str  # URI
    fields: List[str]  # 字段列表


def extract_tables_from_entities(entities: Dict[str, Any]) -> Dict[str, TableMetadata]:
    """
    从 entities 提取所有表的元数据

    Returns:
        {full_table_key: TableMetadata}
        - full_table_key: 业务表/参照表使用 "schema.tableName"，特征表使用 "tableName"
    """
    tables: Dict[str, TableMetadata] = {}
    all_fields: Dict[str, List[str]] = {}  # 用于收集字段

    if not entities:
        return tables

    # 第一遍：提取主表和字段
    entities_list = entities.get("entities") or []
    if isinstance(entities_list, list):
        for entity in entities_list:
            if not isinstance(entity, dict):
                continue

            table_name = entity.get("tableName") or ""
            schema = entity.get("schema") or ""
            bill_name = entity.get("billName") or ""
            uri = entity.get("uri") or ""
            relation_type = entity.get("relation_type", "main")

            if not table_name:
                continue

            # 确定表类型
            if relation_type == "detail":
                table_type = "detail"
            else:
                table_type = "business"

            # 字段收集
            fields: List[str] = []
            attributes = entity.get("attributes") or []
            if isinstance(attributes, list):
                for attr in attributes:
                    if isinstance(attr, dict):
                        # dbColumnName 是数据库字段名
                        field_name = attr.get("dbColumnName") or ""
                        if field_name:
                            fields.append(field_name.lower())

                        # 提取参照实体的表信息
                        ref_struct = attr.get("referenceStructure")
                        if ref_struct and isinstance(ref_struct, dict):
                            ref_table = ref_struct.get("tableName") or ""
                            ref_schema = ref_struct.get("schema") or ""
                            ref_bill_name = ref_struct.get("billName") or ""
                            if ref_table and ref_schema:
                                key = f"{ref_schema}.{ref_table}"
                                if key not in tables:
                                    tables[key] = TableMetadata(
                                        table_name=ref_table,
                                        schema=ref_schema,
                                        table_type="reference",
                                        bill_name=ref_bill_name,
                                        uri="",
                                        fields=[]
                                    )

                        # 提取特征表信息
                        if attr.get("isCharacterRef") or attr.get("characterFields"):
                            char_fields = attr.get("characterFields") or []
                            if isinstance(char_fields, list):
                                for cf in char_fields:
                                    if isinstance(cf, dict):
                                        all_tables = cf.get("allTables") or []
                                        for char_table in all_tables:
                                            if char_table and char_table not in tables:
                                                tables[char_table] = TableMetadata(
                                                    table_name=char_table,
                                                    schema="",  # 特征表无 schema
                                                    table_type="character",
                                                    bill_name=f"{bill_name}特征表",
                                                    uri="",
                                                    fields=[]
                                                )

            # 添加主表
            if schema:
                key = f"{schema}.{table_name}"
            else:
                key = table_name

            if key not in tables:
                tables[key] = TableMetadata(
                    table_name=table_name,
                    schema=schema,
                    table_type=table_type,
                    bill_name=bill_name,
                    uri=uri,
                    fields=fields
                )
            else:
                # 合并字段
                existing = tables[key]
                for f in fields:
                    if f not in existing.fields:
                        existing.fields.append(f)

    return tables


def extract_tables_from_sql(sql: str) -> List[Tuple[str, str, str]]:
    """
    从 SQL 提取表名（仅 FROM/JOIN 后面的表）

    Args:
        sql: SQL 语句

    Returns:
        [(schema, table_name, original_ref)] 列表
        - schema: schema 名（可能为空）
        - table_name: 表名
        - original_ref: SQL 中的原始引用（如 schema.table 或纯 table）
    """
    tables: List[Tuple[str, str, str]] = []
    seen: Set[str] = set()

    # 匹配各种 JOIN 变体 + 表引用
    # 匹配模式：(JOIN关键词) + (可选 schema.table 或 table) + (可选 AS alias)
    _FROM_JOIN_PATTERN = re.compile(
        r'\b(FROM|JOIN|LEFT\s+JOIN|RIGHT\s+JOIN|INNER\s+JOIN|OUTER\s+JOIN|FULL\s+JOIN)\s+'
        r'([a-zA-Z_][a-zA-Z0-9_]*'      # 表名或schema名第一部分
        r'(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)',  # 可选的 .table 或更多层级
        re.IGNORECASE
    )

    for match in _FROM_JOIN_PATTERN.finditer(sql):
        table_ref = match.group(2).strip()

        # 查找 ON 关键字或 WHERE/LIMIT 等子句边界
        match_start = match.start(2)
        rest = sql[match.end():]
        on_match = re.search(r'\bON\b', rest, re.IGNORECASE)
        clause_match = re.search(r'\b(WHERE|LIMIT|GROUP|ORDER|HAVING|UNION)\b', rest, re.IGNORECASE)

        end_pos = match.end()
        if on_match:
            end_pos = match.start() + match.end(2) + on_match.start()
        elif clause_match:
            end_pos = match.start() + match.end(2) + clause_match.start()

        original = sql[match.start():end_pos].strip()

        # 检查是否带 schema
        if '.' in table_ref:
            parts = table_ref.split('.', 1)  # 只分割第一个点
            schema = parts[0]
            table = parts[1]
        else:
            schema = ''
            table = table_ref

        # 去重（使用表引用作为 key）
        key = f"{schema}.{table}" if schema else table
        if key not in seen:
            seen.add(key)
            tables.append((schema, table, original))

    return tables


def extract_fields_from_sql(sql: str) -> List[Tuple[str, str]]:
    """
    从 SQL 提取字段引用

    Args:
        sql: SQL 语句

    Returns:
        [(字段名, 表别名)] 列表
    """
    fields: List[Tuple[str, str]] = []

    # 匹配 SELECT field 或 table.field（使用预编译正则）
    select_match = _SELECT_CLAUSE_PATTERN.search(sql)
    if select_match:
        select_clause = select_match.group(1)
        # 简单提取字段（不处理复杂表达式）
        for part in select_clause.split(','):
            part = part.strip()
            # 处理 table.field 格式
            if '.' in part and not part.startswith('('):
                parts = part.split('.')
                if len(parts) == 2:
                    table_alias = parts[0].strip().lower()
                    field_name = parts[1].strip().lower()
                    # 去除 AS alias
                    if ' as ' in field_name:
                        field_name = field_name.split(' as ')[0].strip()
                    fields.append((field_name, table_alias))
            else:
                # 单独字段
                field_name = part.lower()
                if ' as ' in field_name:
                    field_name = field_name.split(' as ')[0].strip()
                # 排除 * 和函数调用
                if field_name != '*' and '(' not in field_name:
                    fields.append((field_name, ''))

    # 匹配 WHERE/ON 条件中的字段（使用预编译正则）
    for match in _CONDITION_FIELD_PATTERN.finditer(sql):
        field_ref = match.group(1).lower()
        if '.' in field_ref:
            parts = field_ref.split('.')
            if len(parts) == 2:
                fields.append((parts[1], parts[0]))
        else:
            fields.append((field_ref, ''))

    return fields


def extract_cte_names(sql: str) -> List[str]:
    """
    提取 CTE 名称

    Args:
        sql: SQL 语句

    Returns:
        CTE 名称列表
    """
    cte_names: List[str] = []

    # 匹配 WITH cte_name AS（使用预编译正则）
    for match in _CTE_PATTERN.finditer(sql):
        cte_names.append(match.group(1).lower())

    # 匹配逗号分隔的多个 CTE（使用预编译正则）
    for match in _MULTI_CTE_PATTERN.finditer(sql):
        cte_names.append(match.group(1).lower())

    return cte_names


# ============================================================
# 静态校验规则
# ============================================================

class SQLStaticValidator:
    """SQL 静态规则校验器"""

    def __init__(self, entities_path: Optional[Path] = None):
        """
        初始化校验器

        Args:
            entities_path: 元数据文件路径
        """
        self.entities_raw = load_entities_metadata(entities_path)
        # 解析表元数据
        self.tables_meta: Dict[str, TableMetadata] = extract_tables_from_entities(self.entities_raw)
        # 构建各种查找表
        self.business_tables: Set[str] = set()  # schema.tableName
        self.reference_tables: Set[str] = set()  # schema.tableName
        self.character_tables: Set[str] = set()  # tableName (无 schema)
        self.all_table_keys: Set[str] = set()  # 所有表键

        for key, meta in self.tables_meta.items():
            if meta.table_type == "business":
                self.business_tables.add(key)
            elif meta.table_type == "reference":
                self.reference_tables.add(key)
            elif meta.table_type == "character":
                self.character_tables.add(key)
            self.all_table_keys.add(key)

    def validate(self, sql: str) -> StaticValidationResult:
        """
        执行静态校验

        Args:
            sql: SQL 语句

        Returns:
            校验结果
        """
        result = StaticValidationResult(ok=True)

        # 1. 表名存在性校验（按类型）
        self._check_tables_by_type(sql, result)

        # 2. 租户过滤校验
        self._check_tenant_filter(sql, result)

        # 3. 参数格式校验
        self._check_param_format(sql, result)

        # 4. CTE 引用校验
        self._check_cte_references(sql, result)

        # 5. 语法基本检查
        self._check_basic_syntax(sql, result)

        # 最终结果
        if result.errors:
            result.ok = False

        return result

    def _check_tables_by_type(self, sql: str, result: StaticValidationResult) -> None:
        """
        按类型检查表名是否存在

        业务对象（主表）：必须使用 schema.tableName 格式
        参照实体：必须使用 schema.tableName 格式
        特征实体：直接使用表名（无 schema）
        子表：必须使用 schema.tableName 格式
        """
        tables_in_sql = extract_tables_from_sql(sql)
        cte_names = extract_cte_names(sql)

        for schema, table_name, original_ref in tables_in_sql:
            # 跳过 CTE 表名
            if table_name.lower() in cte_names:
                continue

            # 构建可能的键
            if schema:
                key = f"{schema}.{table_name}"
            else:
                key = table_name

            # 检查表是否存在
            table_found = False
            table_type = None
            expected_format = None

            # 1. 先检查是否有 schema（主表、参照、子表）
            if schema:
                if key in self.business_tables:
                    table_found = True
                    table_type = "业务对象（主表）"
                    expected_format = f"{schema}.{table_name}"
                elif key in self.reference_tables:
                    table_found = True
                    table_type = "参照实体"
                    expected_format = f"{schema}.{table_name}"
                elif key in self.all_table_keys:
                    meta = self.tables_meta.get(key)
                    if meta:
                        table_type = meta.table_type
                        table_found = True
                        expected_format = f"{schema}.{table_name}"
            else:
                # 2. 无 schema，检查是否是特征表
                if table_name in self.character_tables:
                    table_found = True
                    table_type = "特征实体"
                    expected_format = table_name
                elif table_name in self.all_table_keys:
                    meta = self.tables_meta.get(table_name)
                    if meta:
                        table_type = meta.table_type
                        table_found = True
                        if meta.schema:
                            expected_format = f"{meta.schema}.{table_name}"
                        else:
                            expected_format = table_name

            if not table_found:
                # 提供详细的错误信息
                suggestion = ""
                if schema:
                    # 检查是否是特征表被错误写成了 schema.table 格式
                    if table_name in self.character_tables:
                        suggestion = f"注意：'{table_name}' 是特征表，不应带 schema 前缀"
                        result.warnings.append({
                            "type": "invalid_table_format",
                            "table": key,
                            "message": f"表 '{key}' 是特征表，不应使用 schema.table 格式，应直接使用表名 '{table_name}'",
                            "severity": "warning",
                            "suggestion": suggestion
                        })
                    else:
                        result.errors.append({
                            "type": "table_not_found",
                            "table": key,
                            "table_type": table_type,
                            "message": f"表 '{key}' 未在元数据中找到",
                            "severity": "error"
                        })
                else:
                    # 检查是否应该是 schema.table 格式
                    matching_tables = [k for k in self.all_table_keys if k.endswith(f".{table_name}")]
                    if matching_tables:
                        result.warnings.append({
                            "type": "missing_schema_prefix",
                            "table": table_name,
                            "message": f"表 '{table_name}' 需要带 schema 前缀，建议使用 '{matching_tables[0]}'",
                            "severity": "warning",
                            "suggestion": f"可能需要使用: {matching_tables[0]}"
                        })
                    else:
                        result.errors.append({
                            "type": "table_not_found",
                            "table": table_name,
                            "message": f"表 '{table_name}' 未在元数据中找到",
                            "severity": "error"
                        })

    def _check_tenant_filter(self, sql: str, result: StaticValidationResult) -> None:
        """检查租户过滤条件"""
        sql_lower = sql.lower()

        # 检查是否包含 ytenant_id
        if 'ytenant_id' not in sql_lower:
            result.errors.append({
                "type": "missing_tenant_filter",
                "message": "SQL 缺少租户过滤条件 ytenant_id，请添加 WHERE ytenant_id = 'var$(租户id)'",
                "fix_action": "add_tenant_filter",
                "severity": "error"
            })

        # 检查租户变量格式
        if 'ytenant_id' in sql_lower:
            # 检查是否使用 var$(租户id) 格式
            if "'var$(租户id)'" not in sql and '"var$(租户id)"' not in sql:
                # 检查是否有硬编码租户ID（警告）（使用预编译正则）
                if _TENANT_PATTERN.search(sql):
                    result.warnings.append({
                        "type": "hardcoded_tenant_id",
                        "message": "租户ID使用了硬编码值，建议使用 'var$(租户id)' 格式",
                        "severity": "warning"
                    })

    def _check_param_format(self, sql: str, result: StaticValidationResult) -> None:
        """检查参数格式"""
        # 检查 param$ 格式（使用预编译正则）
        params = _PARAM_PATTERN.findall(sql)

        for param in params:
            # 检查是否使用单引号
            if "'" not in param:
                result.errors.append({
                    "type": "invalid_param_format",
                    "param": param,
                    "message": f"参数格式错误: {param}，应使用 param$('参数名')",
                    "fix_action": "fix_param_format",
                    "severity": "error"
                })

        # 检查 var$ 格式（使用预编译正则）
        vars_found = _VAR_PATTERN.findall(sql)

        for var in vars_found:
            # 检查是否使用单引号（租户ID不需要引号）
            if "'" in var and "租户id" in var.lower():
                # 这是正确的格式
                pass
            elif "'" not in var and "租户id" not in var.lower():
                result.warnings.append({
                    "type": "invalid_var_format",
                    "var": var,
                    "message": f"变量格式可能错误: {var}",
                    "severity": "warning"
                })

    def _check_cte_references(self, sql: str, result: StaticValidationResult) -> None:
        """检查 CTE 引用是否正确"""
        cte_names = extract_cte_names(sql)

        if not cte_names:
            return

        # 提取 FROM/JOIN 中的表引用
        tables_in_sql = extract_tables_from_sql(sql)

        # 检查引用的 CTE 是否定义
        for schema, table_name, original_ref in tables_in_sql:
            if table_name.lower() in cte_names:
                continue  # CTE 跳过

            # 如果不是 CTE，检查是否是已知表
            key = f"{schema}.{table_name}" if schema else table_name
            is_real_table = key in self.all_table_keys or table_name in self.all_table_keys

            # 如果既不是 CTE 也不是已知表，给出警告
            if not is_real_table:
                result.warnings.append({
                    "type": "unknown_table_reference",
                    "table": key,
                    "message": f"引用的表/CTE '{key}' 未在元数据中找到",
                    "severity": "warning"
                })

    def _check_basic_syntax(self, sql: str, result: StaticValidationResult) -> None:
        """检查基本语法"""
        # 检查 SELECT 和 FROM 是否存在
        sql_upper = sql.upper()
        if 'SELECT' not in sql_upper:
            result.errors.append({
                "type": "missing_select",
                "message": "SQL 缺少 SELECT 语句",
                "severity": "error"
            })

        # 检查分号结尾
        if not sql.strip().rstrip().endswith(';'):
            result.warnings.append({
                "type": "missing_semicolon",
                "message": "SQL 未以分号结尾",
                "severity": "warning"
            })

        # 检查括号匹配
        open_count = sql.count('(')
        close_count = sql.count(')')
        if open_count != close_count:
            result.errors.append({
                "type": "unbalanced_parentheses",
                "message": f"括号不匹配: 左括号 {open_count} 个，右括号 {close_count} 个",
                "severity": "error"
            })


def validate_sql_static(
    sql: str,
    entities_path: Optional[Path] = None
) -> StaticValidationResult:
    """
    静态校验 SQL（便捷入口）

    Args:
        sql: SQL 语句
        entities_path: 元数据文件路径

    Returns:
        校验结果
    """
    validator = SQLStaticValidator(entities_path)
    return validator.validate(sql)
