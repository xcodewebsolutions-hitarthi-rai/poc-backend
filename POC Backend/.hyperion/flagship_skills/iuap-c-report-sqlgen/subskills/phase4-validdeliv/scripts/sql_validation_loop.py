# -*- coding: utf-8 -*-
"""
SQL 校验循环模块

执行静态校验 + 数据库校验的循环迭代：
- 静态校验失败 → 自动修复 → 重试
- 数据库校验失败 → 修正 SQL → 重试
- 最多迭代 max_iterations 次
"""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from sql_static_validator import validate_sql_static, StaticValidationResult
except ImportError:
    StaticValidationResult = None
    validate_sql_static = None

try:
    from sql_self_healer import heal_sql
except ImportError:
    heal_sql = None


@dataclass
class ValidationStep:
    """校验步骤"""
    step: int
    ok: bool
    elapsed_ms: float
    error: Optional[str] = None
    fixes_applied: List[Dict[str, Any]] = field(default_factory=list)
    static_result: Optional[StaticValidationResult] = None


@dataclass
class ValidationLoopResult:
    """校验循环结果"""
    ok: bool = True
    sql: str = ""
    iterations: int = 0
    total_time_ms: float = 0.0
    error: Optional[str] = None
    steps: List[ValidationStep] = field(default_factory=list)


class SQLValidationLoop:
    """SQL 校验循环"""

    def __init__(
        self,
        db_validator_func: Callable[[str], Tuple[bool, Optional[str]]],
        entities_path: Optional[Path] = None,
        max_iterations: int = 10,
        static_validation_enabled: bool = True,
        self_heal_enabled: bool = True,
    ):
        """
        初始化校验循环

        Args:
            db_validator_func: 数据库校验回调函数，签名为 (sql: str) -> (ok: bool, error: Optional[str])
            entities_path: 元数据文件路径
            max_iterations: 最大迭代次数
            static_validation_enabled: 是否启用静态校验
            self_heal_enabled: 是否启用自动修复
        """
        self.db_validator = db_validator_func
        self.entities_path = entities_path
        self.max_iterations = max_iterations
        self.static_validation_enabled = static_validation_enabled
        self.self_heal_enabled = self_heal_enabled

    def run(self, sql: str) -> ValidationLoopResult:
        """
        执行校验循环

        Args:
            sql: 初始 SQL

        Returns:
            校验循环结果
        """
        result = ValidationLoopResult(sql=sql)
        current_sql = sql
        start_time = time.time()

        for iteration in range(1, self.max_iterations + 1):
            step = ValidationStep(step=iteration, ok=False, elapsed_ms=0)
            step_start = time.time()

            try:
                # 1. 静态校验
                if self.static_validation_enabled and validate_sql_static:
                    static_result = validate_sql_static(current_sql, self.entities_path)
                    step.static_result = static_result

                    if not static_result.ok:
                        step.ok = False
                        step.error = "静态校验失败: " + "; ".join(
                            e.get("message", "未知错误") for e in static_result.errors
                        )
                        step.elapsed_ms = (time.time() - step_start) * 1000
                        result.steps.append(step)
                        result.error = step.error
                        break

                    # 2. 自动修复
                    if self.self_heal_enabled and heal_sql and static_result.errors:
                        healed_sql, fixes = heal_sql(
                            current_sql,
                            static_result.errors,
                            self.entities_path
                        )
                        if healed_sql != current_sql:
                            step.fixes_applied.extend(fixes)
                            current_sql = healed_sql

                # 3. 数据库校验
                db_ok, db_error = self.db_validator(current_sql)

                if db_ok:
                    step.ok = True
                    step.elapsed_ms = (time.time() - step_start) * 1000
                    result.steps.append(step)
                    result.ok = True
                    result.sql = current_sql
                    break
                else:
                    step.ok = False
                    step.error = db_error
                    step.elapsed_ms = (time.time() - step_start) * 1000
                    result.steps.append(step)
                    result.error = db_error
                    # 数据库校验失败也停止
                    break

            except Exception as e:
                step.ok = False
                step.error = str(e)
                step.elapsed_ms = (time.time() - step_start) * 1000
                result.steps.append(step)
                result.error = str(e)
                break

        result.iterations = len(result.steps)
        result.total_time_ms = (time.time() - start_time) * 1000

        return result


def create_validation_loop(
    db_validator_func: Callable[[str], Tuple[bool, Optional[str]]],
    entities_path: Optional[Path] = None,
    max_iterations: int = 10,
    static_validation_enabled: bool = True,
    self_heal_enabled: bool = True,
) -> SQLValidationLoop:
    """
    创建校验循环实例（便捷入口）

    Args:
        db_validator_func: 数据库校验回调函数
        entities_path: 元数据文件路径
        max_iterations: 最大迭代次数
        static_validation_enabled: 是否启用静态校验
        self_heal_enabled: 是否启用自动修复

    Returns:
        SQLValidationLoop 实例
    """
    return SQLValidationLoop(
        db_validator_func=db_validator_func,
        entities_path=entities_path,
        max_iterations=max_iterations,
        static_validation_enabled=static_validation_enabled,
        self_heal_enabled=self_heal_enabled,
    )


def format_validation_result(result: ValidationLoopResult) -> str:
    """
    格式化校验结果为字符串

    Args:
        result: 校验结果

    Returns:
        格式化的字符串
    """
    lines = []
    lines.append(f"校验结果: {'[OK] 通过' if result.ok else '[FAIL] 失败'}")
    lines.append(f"迭代次数: {result.iterations}/{result.max_iterations if hasattr(result, 'max_iterations') else 'N'}")
    lines.append(f"总耗时: {result.total_time_ms:.2f}ms")
    lines.append("")

    for step in result.steps:
        status = "[OK]" if step.ok else "[FAIL]"
        lines.append(f"  步骤 {step.step}: {status} ({step.elapsed_ms:.2f}ms)")
        if step.error:
            lines.append(f"    错误: {step.error}")
        if step.fixes_applied:
            lines.append(f"    修复: {len(step.fixes_applied)} 项")

    return "\n".join(lines)
