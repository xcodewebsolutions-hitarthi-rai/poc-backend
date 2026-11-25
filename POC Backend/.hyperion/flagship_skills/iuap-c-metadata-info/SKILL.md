---
name: iuap-c-metadata-info
description: >-
  查询旗舰版业务对象元数据：根据中文单据名称解析业务对象，返回 schema、表名、数据库列名、字段属性、枚举值、参照结构和子表信息。这是生成代码场景的核心依赖工具，适用于获取元数据、单据字段、表结构、实体属性、参照结构，子表、特征组等场景。
  【严禁触发】当用户话术中包含「报表」「报表SQL」「生成报表」「报表开发」「统计报表」「汇总表」「日报」「月报」「台账报表」等任何与报表相关的关键词时，绝对禁止使用本技能，必须使用 iuap-c-report-sqlgen 技能。本技能仅用于代码生成（客开、集成）场景的元数据查询，与报表无关。
# 调用本工具时自动执行（参数名会转为 CLI 的 --kebab-case）
auto-run-script:
  script: scripts/run_query.sh
  args:
    - --allbillname
    - "${allbillname}"
  include_tool_params: true
parameters:
  type: object
  properties:
    allbillname:
      type: string
      description: 中文单据/元数据名称，多个用英文逗号分隔（可与 config.yaml 的 request 叠加）
    modulename:
      type: string
      description: 模块名称或编码（可选）
    isIncludeSub:
      type: string
      description: >-
        Y/N，是否包含子实体/子表。【默认值：Y】
        - 默认 Y：自动获取主表+子表+特征表完整结构
        - 设置 N：仅返回主表
        - 注意：当关键词存在多义时，需配合 queryUri 使用两步查询模式
    docFields:
      type: string
      description: 字段显示名过滤，逗号分隔（可选）
    isDescField:
      type: string
      description: Y/N（可选）
    isSQL:
      type: string
      description: Y/N，为 Y 时对参照类属性拉取 referenceStructure（可选）
    tableTemplate:
      type: string
      description: 报表表头模板字符串（可选）
    queryUri:
      type: string
      description: >-
        元数据实体 uri。索引多义时用户选定后写入此项并再次调用。
        isIncludeSub=N 时可仅填 queryUri（单次 queryByUri 直查主实体）。
        isIncludeSub=Y 时必须与 allbillname 同传（或 queryByUri 响应能解析到 boId）：脚本用所选 uri 锚定业务对象并走 byboid 拉取主表+子表全量实体。
        ⚠️ 当关键词存在多义时，isIncludeSub=Y 会导致错误实体（详见「含子表查询」两步查询模式）。
---

# 旗舰版元数据查询（iuap-c-metadata-info）

## 输出规范（强制要求）

### 必须输出完整层级结构

查询结果必须包含**主表 + 子表 + 孙表所有层级**，禁止遗漏任何层级：

```
主表（层级1）
├── 子表（层级2）
│   ├── 孙表（层级3）
│   └── 孙表（层级3）
└── 子表（层级2）
    └── 孙表（层级3）
```

## 缓存机制

### 核心架构

采用**全局索引 + 自动续查**模式，所有查询结果都会缓存以提升性能。

```
查询请求 → 检查全局索引 → 命中完整缓存？→ 直接返回
                              ↓否
                        命中候选列表？→ 自动选择标准实体 → 续查 → 更新索引
                              ↓否
                          API查询 → 保存缓存 → 返回结果
```

### 缓存文件位置

| 类型 | 路径 | 说明 |
|------|------|------|
| **缓存数据** | `项目根/.hyperion/cache/{billname}.txt` | 查询结果完整内容 |
| **索引文件** | `项目根/.hyperion/cache/metadata-cache-index.json` | billname → 缓存文件映射 |

**环境变量覆盖**：`METADATA_INDEX_PATH` 可覆盖索引文件路径。

### 索引结构

```json
{
  "version": 3,
  "global": {
    "index": {
      "付款单": {
        "uri": "earap.payment.PaymentHeader",
        "file": "付款单.txt",
        "absolutePath": "项目根/.hyperion/cache/付款单.txt",
        "created": "2026-05-15T03:22:51Z",
        "version": 1
      }
    }
  }
}
```

### 缓存命中规则

| 场景 | 缓存文件内容 | 处理方式 |
|------|-------------|---------|
| 完整缓存 | 含 `__METADATA_JSON_START__` 且不含"候选" | **直接返回**，无需 API 调用 |
| 候选列表 | 包含多个候选实体 | 自动选择标准实体，续查并更新索引 |
| 未命中 | 索引中不存在 | API 查询，结果存入缓存 |

### 缓存失效触发

当查询结果与预期不符时，需清除缓存后重新查询：

| 场景 | 命令 | 说明 |
|------|------|------|
| 用户说"不对"、"错了" | `cleanup_cache.sh precise-delete <billname>` | 清除指定 billname 的缓存 |
| 强制刷新 | 传入参数 `isForceRefresh=Y` | 跳过缓存，直接查询 |
| 清除所有缓存 | 删除 `.hyperion/cache/` 目录 | 重置所有缓存 |

### 缓存管理命令速查

```bash
# 使用项目根目录的包装脚本，自动兼容 Windows/Mac/Linux
# 从任意目录调用，无需指定绝对路径

# 检查缓存状态（返回 SESSION_HIT / INDEX_HIT / NOT_FOUND）
bash scripts/cleanup-cache.sh check "付款单"

# 精准删除（否定词触发后必须执行）
bash scripts/cleanup-cache.sh precise-delete "付款单"

# 注册新缓存（查询成功后自动执行）
bash scripts/cleanup-cache.sh register "付款单" "earap.payment.PaymentHeader"

# 列出全局索引
bash scripts/cleanup-cache.sh list-all

# 列出当前会话缓存
bash scripts/cleanup-cache.sh list-session
```

### 常见缓存问题排查

| 问题 | 可能原因 | 解决方案 |
|------|---------|---------|
| 返回的结果不正确 | 缓存了错误的实体 | `precise-delete` 后重新查询 |
| 查询结果为空 | 索引中有脏数据 | 删除索引文件，重新查询 |
| 每次都重新查询 | 缓存文件被误删 | 正常现象，缓存会自动重建 |

## 何时使用

- 用户需要**单据/实体的字段结构、表名、schema、数据库列名、枚举、参照**，且**与报表无关**。
- 典型话术：「销售订单有哪些字段」「这个单据主表叫什么」「元数据里客户字段对应哪一列」等。
- **参照翻译场景（强制只查主表）**：当需要根据参照字段的 id 翻译为 code 或 name 时，必须只查询主表，不查询子表。
- **区分**：查业务**接口 URL/入参**请用 `iuap-c-openapi-integration`。

## 严禁触发场景

**以下任何场景，必须使用 `iuap-c-report-sqlgen` 技能：**

- 用户话术包含「报表」「报表SQL」「生成报表」「报表开发」「统计报表」「汇总表」「日报」「月报」「年报」「台账报表」「查询报表」「数据报表」等关键词
- 用户要求「写SQL」「生成SQL」「SQL查询」且上下文与报表/统计/汇总相关

## 关键词优先级

**⚠️ 用户提供的关键词 = 最高优先级，直接使用。**

| 优先级 | 操作 | 示例 |
|--------|------|------|
| 1（最高） | 直接使用关键词 | 用户说「付款单」→ `--allbillname "付款单"` |
| 2 | 失败后附加"档案" | 「客户」失败 → 尝试「客户档案」 |
| 3 | 最后考虑变体 | 只有前两步都失败才尝试 |

## 参数详解

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `allbillname` | 中文单据名称，**必填** | - |
| `queryUri` | 元数据实体 URI，多义词选定后填写 | 空 |
| `isIncludeSub` | 是否包含子表：`Y`=完整结构，`N`=仅主表 | `Y` |
| `docFields` | 字段显示名过滤，逗号分隔 | 空 |
| `isSQL` | `Y` 时拉取参照 referenceStructure | `N` |
| `modulename` | 模块名称（预留） | 空 |

## 含子表查询

### 两步查询模式（多义词必须遵循）

当关键词存在多义风险时，禁止直接带上 `--is-include-sub Y`：

```bash
# Step 1：确认主实体 URI（不加子表参数）
bash scripts/metadata-query.sh --allbillname "付款单"

# Step 2：使用确认的 URI 查询（含子表）
bash scripts/metadata-query.sh --allbillname "付款单" --query-uri "earap.payment.PaymentHeader" --is-include-sub Y
```

### 何时可直接含子表查询

只有当关键词**无多义**时，才可以直接带 `--is-include-sub Y`：

```bash
# ✅ 销售订单无多义，可直接查询（含子表）
bash scripts/metadata-query.sh --allbillname "销售订单" --is-include-sub Y

# ✅ 已确认唯一 URI，直接含子表查询
bash scripts/metadata-query.sh --allbillname "员工" --query-uri "bd.staff.Staff" --is-include-sub Y
```

## 执行指南

### 推荐方式：run_query.sh

```bash
# 使用项目根目录的包装脚本，从任意目录调用
# Windows/Mac/Linux 通用

# 基本查询
bash scripts/metadata-query.sh --allbillname "销售订单"

# 含子表
bash scripts/metadata-query.sh --allbillname "销售订单" --is-include-sub Y

# 多义词指定 URI
bash scripts/metadata-query.sh --allbillname "客户档案" --query-uri "aa.merchant.Merchant"

# 查看帮助
bash scripts/metadata-query.sh --help
```

### 备选方式：直接调用 skill 脚本

```bash
# 从项目根目录或任意子目录调用（旗舰版用 flagship_skills，高级版用 premium_skills）
bash .hyperion/flagship_skills/iuap-c-metadata-info/scripts/run_query.sh --allbillname "销售订单"
```

## 多义处理

### 必须让用户确认

当查询命中多条候选时，必须列出候选让用户选择，**禁止擅自选择**。

### 响应模板

```
【需要确认】
查询「{关键词}」命中 {N} 个同名业务对象，请确认使用哪个：

| 序号 | code | 名称 | 来源 |
|------|------|------|------|
| 1 | xxx | xxx | xxx |
| 2 | xxx | xxx | xxx |

请提供对应的 code 或序号（如输入 "1"）。
```

### 超时自动选择

超过10分钟未确认，自动选择第一个标准实体（非客开自定义）。

## 常见业务对象参考

| 业务对象 | allbillname | URI | Domain |
|----------|-------------|-----|--------|
| 销售订单 | `销售订单` | `voucher.order.Order` | `udinghuo` |
| 采购订单 | `采购订单` | `scm.order.PurchaseOrder` | `scm` |
| 客户档案 | `客户档案` | `aa.merchant.Merchant` | `productcenter` |
| 付款单 | `付款单` | `earap.payment.PaymentHeader` | `earap` |

## 配置

### 配置文件

```
./
└── .hyperion/
    └── skills/
        └── iuap-c-metadata-info/
            ├── config.yaml          # API 配置
            └── scripts/            # 执行脚本
```

### 环境变量

```bash
# .env 文件
API_BASE_URL=<API地址>
API_APP_KEY=<应用Key>
API_APP_SECRET=<应用密钥>
YONBIP_TENANT_ID=<租户ID>
```

### config.yaml 示例

```yaml
api:
  base_url: "${API_BASE_URL}"
  app_key: "${API_APP_KEY}"
  app_secret: "${API_APP_SECRET}"
  success_code: "00000"

metadata_api:
  query_by_uri_path: "/iuap-api-gateway/${YONBIP_TENANT_ID}/yonbip/digitalModel/meta/queryByUri"
  search_by_name_path: "/iuap-api-gateway/${YONBIP_TENANT_ID}/yonbip/digitalModel/meta/searchByName"
  entity_list_by_bo_id_path: "/iuap-api-gateway/${YONBIP_TENANT_ID}/yonbip/digitalModel/meta/entityListByBoid"
```

## 常见问题

| 问题 | 解决方案 |
|------|---------|
| PEP 668 错误 | 使用 `pip_install.sh` 或手动激活虚拟环境 |
| Token 获取失败 | 检查 `.env` 配置和网络连接 |
| 中文乱码 | 设置 `PYTHONIOENCODING=utf-8` |
| 路径找不到 | 使用 `run_query.sh`（自动定位） |

## 脚本说明

### 用户入口（项目根目录）

| 脚本 | 作用 |
|------|------|
| `scripts/metadata-query.sh` | **推荐入口**，元数据查询（自动定位，跨平台） |
| `scripts/cleanup-cache.sh` | 缓存清理与状态检查（自动定位，跨平台） |

### Skill 内部脚本

| 脚本 | 作用 |
|------|------|
| `scripts/run_query.sh` | 自动处理路径和 venv |
| `scripts/cleanup_cache.sh` | 缓存清理核心脚本 |
| `scripts/ultimate_metadata_query.py` | 主查询脚本 |
| `scripts/metadata_core.py` | HTTP 调用核心模块 |
| `scripts/metadata_fast_lookup.py` | 本地快速索引 |
| `scripts/bip_auth.py` | BIP 鉴权模块 |
