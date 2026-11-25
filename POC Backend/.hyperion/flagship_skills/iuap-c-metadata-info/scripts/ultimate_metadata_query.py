#!/usr/bin/env python3
"""
旗舰版元数据查询 — 对齐 UltimateV2Controller.getUltimateMetadataInfo /
BusinessObjectToolUtil.getBusinessObjectInfo。

用法见上级目录 SKILL.md；配置见 ../config.yaml。

新特性:
    - 支持 ${ENV_VAR} 和 ${ENV_VAR:-default} 语法读取环境变量
    - 支持限流配置
    - 支持环境区分（通过环境变量）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# 添加iuap_common目录到sys.path，导入共享模块
_scripts_dir = Path(__file__).resolve().parent
_common_dir = _scripts_dir / "iuap_common"
if str(_common_dir) not in sys.path:
    sys.path.insert(0, str(_common_dir))

import yaml

from iuap_common.console_utf8 import configure_stdio_utf8
from iuap_common.python_version_check import require_python_version
from iuap_common.utils import (
    load_dotenv, resolve_config, validate_api_config, ExitCode
)

from metadata_core import get_business_object_info


def _skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _get_cache_dir() -> Path:
    """获取缓存目录 - 直接放在 .hyperion/cache/ 下，不按会话分开"""
    skill_dir = _skill_dir()  # .../skills/iuap-c-metadata-info/scripts
    # skill_dir.parent = .../skills/iuap-c-metadata-info
    # skill_dir.parent.parent = .../skills
    # skill_dir.parent.parent.parent = 项目根目录 (包含 .claude/)
    # 正确的 cache 路径: 项目根/.claude/cache/
    project_root = skill_dir.parent.parent.parent
    return project_root / ".hyperion" / "cache"


def _safe_filename(name: str) -> str:
    """安全的文件名"""
    import re
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def _extract_metadata_json(out: str) -> Optional[Dict[str, Any]]:
    start_marker = "__METADATA_JSON_START__"
    end_marker = "__METADATA_JSON_END__"
    start = out.find(start_marker)
    if start < 0:
        return None
    start += len(start_marker)
    end = out.find(end_marker, start)
    if end < 0:
        return None
    try:
        data = json.loads(out[start:end].strip())
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _has_nonempty_entities(out: str) -> bool:
    data = _extract_metadata_json(out)
    return data is not None and isinstance(data.get("entities"), list) and bool(data["entities"])


def _save_to_session_cache(out: str, params: Dict[str, Any]) -> None:
    """保存查询结果到会话缓存目录"""
    if "__METADATA_JSON_START__" in out and not _has_nonempty_entities(out):
        print("[缓存] 查询结果没有实体，跳过缓存", file=sys.stderr)
        return

    try:
        # 解析 billname
        allbillname = params.get("allbillname", "")
        if not allbillname:
            return

        # 处理多个 billname（取第一个）
        billname = allbillname.split(",")[0].strip()

        # 提取 URI（传递 billname 用于识别主表）
        uri = _extract_uri(out, billname)

        # 获取缓存目录
        cache_dir = _get_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)

        # 生成文件名
        safe_name = _safe_filename(billname)
        cache_file = cache_dir / f"{safe_name}.txt"

        # 写入缓存（完整内容）
        enc = "utf-8-sig" if sys.platform == "win32" else "utf-8"
        with cache_file.open("w", encoding=enc) as f:
            f.write(out)

        # 更新索引
        _register_to_index(billname, cache_file, uri)

    except Exception as e:
        print(f"[缓存] 保存失败: {e}", file=sys.stderr)


def _delete_from_index(billname: str) -> bool:
    """从索引中精确删除指定的 billname 记录及对应缓存文件，返回是否成功删除"""
    try:
        import json

        cache_dir = _get_cache_dir()
        index_file = cache_dir / "metadata-cache-index.json"

        if not index_file.exists():
            return False

        with open(index_file, encoding="utf-8") as f:
            d = json.load(f)

        global_index = d.get("global", {}).get("index", {})
        if billname not in global_index:
            return False

        # 获取对应的缓存文件路径并删除
        cache_entry = global_index[billname]
        cache_file_path = cache_entry.get("absolutePath", "")
        if cache_file_path:
            cache_file = Path(cache_file_path)
            if cache_file.exists():
                cache_file.unlink()
                print(f"[缓存] 已删除文件: {cache_file.name}", file=sys.stderr)

        # 删除索引记录
        del global_index[billname]
        d["global"]["index"] = global_index

        # 写回索引文件
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

        print(f"[索引] 已删除记录: {billname}", file=sys.stderr)
        return True

    except Exception as e:
        print(f"[索引] 删除失败: {e}", file=sys.stderr)
        return False


def _register_to_index(billname: str, cache_file: Path, uri: str = "") -> None:
    """注册缓存到索引（全局索引，不按会话分开）"""
    try:
        import json
        from datetime import datetime

        cache_dir = _get_cache_dir()
        index_file = cache_dir / "metadata-cache-index.json"

        # 读取或创建索引
        if index_file.exists():
            with open(index_file, encoding="utf-8") as f:
                d = json.load(f)
        else:
            d = {"version": 3, "global": {"index": {}}}

        # 更新全局索引
        safe_name = _safe_filename(billname)
        d.setdefault("global", {}).setdefault("index", {})[billname] = {
            "uri": uri,
            "file": f"{safe_name}.txt",
            "absolutePath": str(cache_file),
            "created": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "version": 1
        }

        # 写入索引
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    except Exception as e:
        print(f"[索引] 更新失败: {e}", file=sys.stderr)


# 标准域名白名单（优先级从高到低）
_STANDARD_DOMAINS = [
    "productcenter",  # 基础数据-产品中心
    "scm",            # 供应链
    "udinghuo",        # 销售
    "voucher",        # 凭证
    "bd",             # 基础档案
    "pu",             # 采购
    "st",             # 库存
    "aa",             # 应收应付
    "earap",          # 应收应付
    "fi",             # 财务
    "mp",             # 生产
]

# 标准 URI 前缀（优先级从高到低）
_STANDARD_URI_PREFIXES = [
    "bd.", "scm.", "voucher.", "udinghuo.", "pu.", "st.",
    "aa.", "earap.", "fi.", "mp.", "pc.", "org.",
]


def _is_standard_entity(uri: str, domain: str) -> tuple[bool, int]:
    """
    判断是否为标准实体，返回 (是否标准, 优先级分数)
    分数越低优先级越高
    """
    # 检查 domain
    for i, std_domain in enumerate(_STANDARD_DOMAINS):
        if std_domain in domain.lower():
            return True, i
    # 检查 URI 前缀
    for i, prefix in enumerate(_STANDARD_URI_PREFIXES):
        if uri.startswith(prefix):
            return True, 100 + i
    return False, 999


def _is_likely_child_table_uri(uri: str) -> bool:
    """
    判断 URI 是否可能是子表/孙表
    子表 URI 通常包含：ApplyRange、Character、Detail、Line、Entry 等后缀或关键词
    """
    if not uri:
        return False
    child_indicators = [
        "ApplyRange", "Character", "Detail", "Line", "Entry",
        "_applyrange", "_character", "_detail", "_line", "_entry"
    ]
    return any(indicator in uri for indicator in child_indicators)


def _extract_uri(out: str, billname: str = "") -> str:
    """
    从输出中提取 URI，优先返回主表 URI
    支持两种格式：
    1. JSON 格式（含完整元数据）- 优先选择 billName 与关键词匹配的主表
    2. 候选列表格式（多义词选择）- 优先选择标准实体
    """
    import re
    import json

    # 尝试解析 JSON 格式
    if '"entities"' in out and '"uri"' in out:
        try:
            # 找到 JSON 开始位置
            start = out.find('{"entities"')
            if start >= 0:
                # 找到 JSON 结束位置（最后一个 }）
                json_str = out[start:]
                # 处理可能存在的尾随文本
                end_brace = json_str.rfind('}')
                if end_brace > 0:
                    json_str = json_str[:end_brace + 1]
                data = json.loads(json_str)
                entities = data.get("entities", [])

                # 策略1：找到 billName 与关键词完全匹配的实体（主表）
                if billname:
                    for entity in entities:
                        uri = entity.get("uri", "")
                        entity_bill_name = entity.get("billName", "")
                        if uri and entity_bill_name == billname:
                            return uri

                # 策略2：找到不含 _applyrange、_character 等后缀的表名对应的主表
                for entity in entities:
                    uri = entity.get("uri", "")
                    table_name = entity.get("tableName", "")
                    bill_name = entity.get("billName", "")
                    # 主表通常没有特殊后缀，且 billName 与关键词相关
                    if uri and bill_name:
                        # 排除子表特征：_applyrange, _character, _detail 等
                        if not any(suffix in table_name.lower() for suffix in ['_applyrange', '_character', '_detail', '_line', '_entry']):
                            return uri

                # 兜底：返回第一个实体的 URI
                if entities:
                    return entities[0].get("uri", "")
        except (json.JSONDecodeError, Exception):
            pass

    # 尝试解析候选列表格式
    # 格式: uri: pc.cls.ManagementClass\n     metadataName: 物料分类  bizName: 物料分类  domain: productcenter
    candidates = []
    lines = out.split('\n')
    for line in lines:
        uri_match = re.search(r'uri:\s*([a-zA-Z0-9._]+)', line)
        domain_match = re.search(r'domain:\s*(\S+)', line)
        name_match = re.search(r'metadataName:\s*([^\s]+)', line)

        if uri_match:
            uri = uri_match.group(1)
            domain = domain_match.group(1) if domain_match else ""
            metadata_name = name_match.group(1) if name_match else ""
            is_std, priority = _is_standard_entity(uri, domain)
            candidates.append({
                "uri": uri,
                "domain": domain,
                "metadata_name": metadata_name,
                "is_standard": is_std,
                "priority": priority
            })

    if not candidates:
        return ""

    # 按优先级排序：标准实体优先，然后按优先级分数
    candidates.sort(key=lambda x: (0 if x["is_standard"] else 1, x["priority"]))

    # 返回第一个（标准实体优先级最高）
    return candidates[0]["uri"]


def _get_candidates_from_output(out: str) -> list:
    """从输出中解析候选列表"""
    import re
    candidates = []

    lines = out.split('\n')
    for line in lines:
        uri_match = re.search(r'uri:\s*([a-zA-Z0-9._]+)', line)
        domain_match = re.search(r'domain:\s*(\S+)', line)
        name_match = re.search(r'metadataName:\s*([^\s]+)', line)
        table_match = re.search(r'tableName:\s*(\S+)', line)

        if uri_match:
            uri = uri_match.group(1)
            domain = domain_match.group(1) if domain_match else ""
            metadata_name = name_match.group(1) if name_match else ""
            table_name = table_match.group(1) if table_match else ""
            is_std, priority = _is_standard_entity(uri, domain)
            candidates.append({
                "uri": uri,
                "domain": domain,
                "metadata_name": metadata_name,
                "table_name": table_name,
                "is_standard": is_std,
                "priority": priority
            })

    return candidates


def _try_resolve_from_cache(params: Dict[str, Any], cfg: dict) -> Optional[str]:
    """
    从缓存中直接返回结果（索引命中时优先使用）。

    流程：
    1. 检查全局索引是否存在该 billname
    2. 如果 URI 已存在且缓存文件包含完整元数据（不含"候选"），直接返回缓存内容
    3. 如果缓存文件包含"候选"列表，解析并选择标准实体，更新索引后直接返回缓存内容
    4. 如果缓存文件不存在或需要刷新，调用 API 重新查询

    返回：完整元数据字符串，或 None（无法从缓存解析）
    """
    import json
    import re

    billname = params.get("allbillname", "")
    if not billname:
        return None

    # 检查是否已有确定的 URI（用户显式指定，跳过缓存）
    if params.get("queryUri"):
        return None

    # 检查是否需要强制刷新
    is_force_refresh = str(params.get("isForceRefresh", "")).lower() == "y"
    if is_force_refresh:
        return None

    try:
        # 获取缓存索引
        cache_dir = _get_cache_dir()
        index_file = cache_dir / "metadata-cache-index.json"

        if not index_file.exists():
            return None

        with open(index_file, encoding="utf-8") as f:
            index_data = json.load(f)

        global_index = index_data.get("global", {}).get("index", {})
        if billname not in global_index:
            return None

        cache_entry = global_index[billname]
        cached_uri = cache_entry.get("uri", "")

        # 获取缓存文件路径
        cache_file_path = cache_entry.get("absolutePath", "")
        if not cache_file_path:
            return None

        cache_file = Path(cache_file_path)
        if not cache_file.exists():
            return None

        # 读取缓存文件内容
        with open(cache_file, encoding="utf-8-sig") as f:
            cached_content = f.read()

        # 检查缓存文件是否包含有效完整元数据（不含"候选"字样）
        if "候选" not in cached_content and _has_nonempty_entities(cached_content):
            # 缓存命中，直接返回（无需调用 API）
            print(f"[缓存命中] 直接返回缓存内容: {cache_file_path}", file=sys.stderr)
            return cached_content
        if "__METADATA_JSON_START__" in cached_content:
            print(f"[缓存失效] 缓存中没有有效实体，将清除并重新查询: {cache_file_path}", file=sys.stderr)
            _delete_from_index(billname)
            return None

        # 缓存文件包含候选列表，尝试解析并选择标准实体
        candidates = _get_candidates_from_output(cached_content)
        if not candidates:
            # 无法解析候选，尝试用缓存的 URI 重新查询
            if cached_uri and cached_uri.strip() and not _is_likely_child_table_uri(cached_uri):
                print(f"[缓存续查] 使用缓存 URI 重新查询: {cached_uri}", file=sys.stderr)
                new_params = {**params, "queryUri": cached_uri}
                out = get_business_object_info(cfg, new_params)
                _save_to_session_cache(out, new_params)
                return out
            return None

        # 选择标准实体
        standard_candidates = [c for c in candidates if c["is_standard"]]
        if not standard_candidates:
            # 没有标准实体，不自动选择
            print(f"[缓存] 未找到标准实体，共 {len(candidates)} 个候选，请手动指定 --query-uri", file=sys.stderr)
            return None

        # 选择优先级最高的候选
        standard_candidates.sort(key=lambda x: x["priority"])
        chosen = standard_candidates[0]
        chosen_uri = chosen["uri"]

        print(f"[缓存] 命中候选列表，自动选择标准实体: {chosen_uri} ({chosen['domain']})", file=sys.stderr)

        # 更新缓存索引中的 URI
        _update_index_uri(billname, chosen_uri)

        # 使用标准实体 URI 重新查询
        new_params = {**params, "queryUri": chosen_uri}
        out = get_business_object_info(cfg, new_params)
        _save_to_session_cache(out, new_params)
        return out

    except Exception as e:
        print(f"[缓存解析] 失败: {e}", file=sys.stderr)
        return None


# 兼容旧函数名
def _try_auto_resolve_from_cache(params: Dict[str, Any], cfg: dict) -> Optional[str]:
    """兼容旧函数名，调用新实现"""
    return _try_resolve_from_cache(params, cfg)


def _update_index_uri(billname: str, uri: str) -> None:
    """更新索引中的 URI"""
    import json
    from datetime import datetime

    try:
        cache_dir = _get_cache_dir()
        index_file = cache_dir / "metadata-cache-index.json"

        if not index_file.exists():
            return

        with open(index_file, encoding="utf-8") as f:
            d = json.load(f)

        if billname in d.get("global", {}).get("index", {}):
            d["global"]["index"][billname]["uri"] = uri
            d["global"]["index"][billname]["version"] = d["global"]["index"][billname].get("version", 1) + 1

        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)

    except Exception as e:
        print(f"[索引] 更新 URI 失败: {e}", file=sys.stderr)


def _load_config(path: Path) -> dict:
    """加载配置（支持环境变量解析）"""
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _infer_is_include_sub(req: dict, overrides: Dict[str, Any]) -> str:
    """
    根据用户请求自动推断 isIncludeSub 值。

    判断逻辑（按优先级）：
    1. 如果命令行明确指定了 --is-include-sub，使用该值
    2. 如果 allbillname 中包含否定词，返回 N
    3. 如果 allbillname 中包含参照翻译关键词，返回 N（只查主表）
    4. 默认返回 Y（大多数场景需要完整的主表+子表信息）
    """
    # 优先级1：命令行明确指定
    if overrides.get("isIncludeSub") is not None:
        val = str(overrides["isIncludeSub"]).strip().upper()
        if val in ("Y", "N"):
            return val

    # 优先级2：检测否定词
    allbillname = req.get("allbillname") or ""
    # 合并命令行参数中的 allbillname
    if overrides.get("allbillname"):
        allbillname = str(overrides["allbillname"])

    if allbillname:
        name_lower = allbillname.lower()

        # 否定词列表：明确表示只查主表、不需要子表等
        negative_keywords = [
            "只查主表", "只要主表", "主表即可", "仅主表",
            "不要子表", "不需要子表", "不查子表", "不包含子表",
            "不含子表", "排除子表", "无子表",
            "仅查主表", "只查询主表", "只要主表字段",
        ]
        for kw in negative_keywords:
            if kw in name_lower:
                return "N"

        # 参照翻译关键词：只查主表用于 id 转 code
        ref_translate_keywords = [
            "参照翻译", "id转code", "id_to_code", "id2code",
            "翻译", "id转换", "id编码", "转code"
        ]
        for kw in ref_translate_keywords:
            if kw in name_lower:
                return "N"

    # 优先级3：默认返回 Y
    return "Y"


def _merge_params(cfg: dict, overrides: Dict[str, Any]) -> Dict[str, Any]:
    req = dict(cfg.get("request") or {})
    for k, v in overrides.items():
        if v is not None and v != "":
            req[k] = v

    # 自动推断 isIncludeSub 值
    is_include_sub = _infer_is_include_sub(req, overrides)

    return {
        "allbillname": req.get("allbillname"),
        "modulename": req.get("modulename"),
        "isIncludeSub": is_include_sub,
        "docFields": req.get("docFields"),
        "isDescField": req.get("isDescField", "N"),
        "isSQL": req.get("isSQL", "N"),
        "tableTemplate": req.get("tableTemplate"),
        "queryUri": req.get("queryUri"),
    }


def main() -> int:
    require_python_version()

    configure_stdio_utf8()
    ap = argparse.ArgumentParser(description="Ultimate metadata query (MDD 旗舰版元数据)")
    ap.add_argument(
        "--config",
        default=str(_skill_dir() / "config.yaml"),
        help="Path to config.yaml",
    )
    ap.add_argument("--allbillname", default=None, help="逗号分隔的单据/元数据中文名称")
    ap.add_argument("--modulename", default=None, help="模块名称或编码（预留，与 Java 一致当前不参与过滤）")
    ap.add_argument(
        "--is-include-sub",
        dest="isIncludeSub",
        default=None,
        help="Y/N 含子表时走 byboid 全量实体链；多义续查须配合 --query-uri 与 --allbillname",
    )
    ap.add_argument("--doc-fields", dest="docFields", default=None, help="逗号分隔字段显示名过滤")
    ap.add_argument("--is-desc-field", dest="isDescField", default=None, help="Y/N 是否单独描述了字段")
    ap.add_argument("--is-sql", dest="isSQL", default=None, help="Y/N 是否拉取参照 referenceStructure")
    ap.add_argument("--table-template", dest="tableTemplate", default=None, help="报表表头模板（参照属性扩展匹配）")
    ap.add_argument(
        "--query-uri",
        dest="queryUri",
        default=None,
        help="元数据 uri；索引多义时选定后重试。含子表（--is-include-sub Y）时建议与 --allbillname 同传",
    )
    ap.add_argument(
        "--validate",
        action="store_true",
        help="仅验证配置，不执行查询",
    )
    ap.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="输出详细日志",
    )
    ap.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file (default: ../.env)",
    )
    args = ap.parse_args()

    # 加载指定的 .env 文件（如果提供）
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

    # 先将命令行覆盖合并进 cfg['request']，再校验（避免插件传入 --allbillname 仍因 yaml 为空而失败）
    cli_overrides = {
        "allbillname": args.allbillname,
        "modulename": args.modulename,
        "isIncludeSub": args.isIncludeSub,
        "docFields": args.docFields,
        "isDescField": args.isDescField,
        "isSQL": args.isSQL,
        "tableTemplate": args.tableTemplate,
        "queryUri": args.queryUri,
    }
    req = dict(cfg.get("request") or {})
    for k, v in cli_overrides.items():
        if v is not None and str(v).strip() != "":
            req[k] = v
    cfg = {**cfg, "request": req}

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

    params = _merge_params(cfg, cli_overrides)

    # 尝试从缓存中自动续查
    out = _try_auto_resolve_from_cache(params, cfg)
    if out is None:
        # 缓存未命中或无法自动解析，执行正常查询
        out = get_business_object_info(cfg, params)
    print(out)

    if "__METADATA_JSON_START__" in out and not _has_nonempty_entities(out):
        print("错误: 元数据查询未返回任何实体，请检查 queryUri 或清除错误缓存后重试", file=sys.stderr)
        return ExitCode.VALIDATION_ERROR

    # 保存到会话 tool-results 目录
    _save_to_session_cache(out, params)

    outp = cfg.get("output") or {}
    if outp.get("write_result_json"):
        out_dir = Path(cfg.get("paths") or {}).get("output_dir") or "output"
        od = _skill_dir() / out_dir
        od.mkdir(parents=True, exist_ok=True)
        fn = outp.get("result_json_filename") or "ultimate_metadata_last.json"
        # 仅写入 JSON 主体（去掉说明前缀需解析；此处写完整文本）
        p = od / fn
        enc = "utf-8-sig" if sys.platform == "win32" else "utf-8"
        with p.open("w", encoding=enc) as f:
            f.write(out)

    return ExitCode.SUCCESS


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        raise SystemExit(ExitCode.UNKNOWN_ERROR)
