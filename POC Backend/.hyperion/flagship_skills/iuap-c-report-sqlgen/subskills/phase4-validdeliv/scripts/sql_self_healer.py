# -*- coding: utf-8 -*-
"""
SQL 自修复模块

根据静态校验错误自动修复 SQL：
- 租户过滤缺失 → 自动添加 WHERE ytenant_id = 'var$(租户id)'
- 参数格式错误 → 自动修复
- 括号不匹配 → 自动修复
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def heal_sql(
    sql: str,
    errors: List[Dict[str, Any]],
    entities_path: Optional[Path] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    根据错误自动修复 SQL

    Args:
        sql: 原始 SQL
        errors: 错误列表（来自 StaticValidationResult.errors）
        entities_path: 元数据文件路径

    Returns:
        (修复后的 SQL, 修复记录列表)
    """
    fixed_sql = sql
    fixes: List[Dict[str, Any]] = []

    for error in errors:
        error_type = error.get("type", "")

        if error_type == "missing_tenant_filter":
            fixed_sql, fix = _heal_missing_tenant_filter(fixed_sql)
            if fix:
                fixes.append(fix)

        elif error_type == "invalid_param_format":
            fixed_sql, fix = _heal_invalid_param_format(fixed_sql, error)
            if fix:
                fixes.append(fix)

        elif error_type == "unbalanced_parentheses":
            fixed_sql, fix = _heal_unbalanced_parentheses(fixed_sql)
            if fix:
                fixes.append(fix)

        elif error_type == "missing_semicolon":
            fixed_sql, fix = _heal_missing_semicolon(fixed_sql)
            if fix:
                fixes.append(fix)

    return fixed_sql, fixes


def _heal_missing_tenant_filter(sql: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """修复缺失的租户过滤条件"""
    # 检查是否已有 WHERE 子句
    where_pattern = re.compile(r'\bWHERE\b', re.IGNORECASE)

    tenant_condition = "ytenant_id = 'var$(租户id)'"

    if where_pattern.search(sql):
        # 已有 WHERE，在末尾添加 AND
        # 查找最后一个 WHERE 或 AND 的位置
        where_match = where_pattern.search(sql)
        if where_match:
            # 在 WHERE 后添加条件
            # 简单策略：在 WHERE 后的第一个 AND/ORDER/GROUP/LIMIT 之前添加
            insert_pos = len(sql)
            for keyword in ['ORDER', 'GROUP', 'LIMIT', 'HAVING', 'UNION']:
                pattern = re.compile(rf'\b{keyword}\b', re.IGNORECASE)
                match = pattern.search(sql[where_match.end():])
                if match:
                    insert_pos = min(insert_pos, where_match.end() + match.start())

            before = sql[:insert_pos].rstrip()
            after = sql[insert_pos:]
            fixed_sql = f"{before} AND {tenant_condition} {after}"

            return fixed_sql, {
                "type": "add_tenant_filter",
                "description": f"添加租户过滤: {tenant_condition}",
            }
    else:
        # 没有 WHERE，在 FROM 后添加 WHERE
        from_pattern = re.compile(r'\bFROM\b', re.IGNORECASE)
        from_match = from_pattern.search(sql)
        if from_match:
            # 在 FROM 后的表名之后添加 WHERE
            # 简单策略：在第一个 JOIN 或 ON 之前添加 WHERE
            insert_pos = len(sql)
            for keyword in ['JOIN', 'WHERE', 'ORDER', 'GROUP', 'LIMIT']:
                pattern = re.compile(rf'\b{keyword}\b', re.IGNORECASE)
                match = pattern.search(sql[from_match.end():])
                if match:
                    insert_pos = min(insert_pos, from_match.end() + match.start())

            before = sql[:insert_pos].rstrip()
            after = sql[insert_pos:]
            fixed_sql = f"{before} WHERE {tenant_condition} {after}"

            return fixed_sql, {
                "type": "add_tenant_filter",
                "description": f"添加租户过滤: {tenant_condition}",
            }

    return sql, None


def _heal_invalid_param_format(sql: str, error: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """修复参数格式错误"""
    param = error.get("param", "")

    if not param:
        return sql, None

    # param$xxx → param$('xxx')
    if not param.startswith("param$('"):
        # 提取参数名
        match = re.search(r"param\$\(([^)]+)\)", param)
        if match:
            param_name = match.group(1)
            old = f"param$({param_name})"
            new = f"param$('{param_name}')"
            if old in sql:
                fixed_sql = sql.replace(old, new)
                return fixed_sql, {
                    "type": "fix_param_format",
                    "description": f"修复参数格式: {old} → {new}",
                }

    return sql, None


def _heal_unbalanced_parentheses(sql: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """修复括号不匹配"""
    open_count = sql.count('(')
    close_count = sql.count(')')

    if open_count > close_count:
        # 需要添加闭合括号
        missing = open_count - close_count
        fixed_sql = sql + ')' * missing
        return fixed_sql, {
            "type": "fix_unbalanced_parentheses",
            "description": f"添加 {missing} 个闭合括号",
        }
    elif close_count > open_count:
        # 需要添加开括号（罕见情况）
        missing = close_count - open_count
        fixed_sql = '(' * missing + sql
        return fixed_sql, {
            "type": "fix_unbalanced_parentheses",
            "description": f"添加 {missing} 个开括号",
        }

    return sql, None


def _heal_missing_semicolon(sql: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """修复缺失的分号"""
    if not sql.strip().endswith(';'):
        fixed_sql = sql.rstrip() + ';'
        return fixed_sql, {
            "type": "add_semicolon",
            "description": "添加分号结尾",
        }

    return sql, None


class SQLSelfHealer:
    """SQL 自修复器"""

    def __init__(self, entities_path: Optional[Path] = None):
        """
        初始化自修复器

        Args:
            entities_path: 元数据文件路径
        """
        self.entities_path = entities_path

    def heal(self, sql: str, errors: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
        """
        修复 SQL

        Args:
            sql: 原始 SQL
            errors: 错误列表

        Returns:
            (修复后的 SQL, 修复记录列表)
        """
        return heal_sql(sql, errors, self.entities_path)
