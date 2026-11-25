# 旗舰版元数据层级检测设计文档

## 1. 概述

本文档描述 `iuap-c-metadata-info` 技能中元数据查询的层级检测机制。该机制能够根据用户查询的实体名称，智能识别查询的是主表、子表、孙表或关联关系表，并返回正确范围的实体集合。

### 1.1 设计目标

| 查询类型 | 用户意图 | 返回结果 |
|---------|---------|---------|
| 主表查询 | 查询完整单据 | 主表 + 所有子表 + 孙表 + 曾孙表 |
| 子表查询 | 查询某个子表 | 子表 + 其孙表 + 曾孙表 |
| 孙表查询 | 查询某个孙表 | 仅孙表本身 |
| 关联表查询 | 查询关联关系表 | 仅关联关系表本身 |

### 1.2 核心概念

#### 实体层级体系

元数据中的实体按层级关系组织：

```
主表 (root=true, pcr=1)
├── 子表 (pcr=2)
│   ├── 孙表 (pcr=3)
│   │   └── 曾孙表 (pcr=4)
│   └── 孙表 (pcr=3)
└── 子表 (pcr=2)
    └── 孙表 (pcr=3)
```

- **pcr (parentChildRelationship)**: 父级关系标识
  - `1`: 主表
  - `2`: 子表
  - `3`: 孙表
  - `4`: 曾孙表（更深层级也用 `4` 表示）

#### 两种查询流程

| 流程 | 触发条件 | 特点 |
|------|---------|------|
| **Flow 1** | 用户传入 `--query-uri` 参数 | 直接锚定实体，跳过 searchByName |
| **Flow 2** | 仅传入 `--allbillname` | 依赖 API 匹配 + 本地索引 |

---

## 2. 关键数据结构

### 2.1 EntityDetailResult

```python
@dataclass
class EntityDetailResult:
    entity_id: Optional[str] = None          # 实体ID
    uri: Optional[str] = None                # 实体URI
    bo_id: Optional[str] = None             # 业务对象ID
    business_object_code: Optional[str] = None
    parent_child_relationship: Optional[int] = None  # 层级关系 (1/2/3/4)
    parent_uri: Optional[str] = None         # 父实体URI
    is_descendant_of_sub: bool = False      # 是否为子表后代
```

**设计要点**：
- `parent_child_relationship`: 用于判断实体层级
- `parent_uri`: 用于构建父子关系链，支持子表查询时标记所有后代
- `is_descendant_of_sub`: 子表查询专用标记，用于包含子表的所有深层后代

### 2.2 查询模式标记

```python
is_sub_table_query: bool        # 子表查询：返回子表 + 孙表 + 曾孙表
is_descendant_only_query: bool  # 孙表+查询：仅返回查询对象本身
is_relation_table_query: bool    # 关联表查询：仅返回查询对象本身
relation_table_uris: Set[str]   # 关联关系表URI集合
main_uri: str                   # 主表URI
```

---

## 3. 层级检测机制

### 3.1 核心逻辑：`_collect_entity_details`

该函数从 `getEntityListByBOId` API 响应中递归收集实体信息：

```python
def _collect_entity_details(
    entities_array: Any,
    out: List[EntityDetailResult],
    depth: int = 0,
    parent_uri: Optional[str] = None  # 新增：追踪父实体
) -> None:
```

**关键行为**：
1. 递归遍历 `entities_array`（包含 `children` 数组）
2. 从每个实体节点提取 `parentChildRelationship` 和 `uri`
3. 通过 `parent_uri` 参数追踪父子关系链
4. 将所有收集到的实体信息存入 `EntityDetailResult` 列表

### 3.2 层级判断规则

```python
# 从 details 中获取查询对象的 pcr
if details and queried_uri_for_level:
    for d in details:
        if d.uri == queried_uri_for_level:
            pcr = d.parent_child_relationship
            if pcr == 2:
                is_sub_table_query = True       # 子表
            elif pcr >= 3:
                is_descendant_only_query = True  # 孙表+
```

### 3.3 子表后代标记

对于子表查询，需要标记所有后代实体（孙表、曾孙表等）：

```python
if is_sub_table_query and query_uri and details:
    for d in details:
        if d.parent_uri == query_uri:
            # 直接子实体（孙表）
            d.is_descendant_of_sub = True
            relation_table_uris.add(d.uri)
        elif d.parent_uri:
            # 检查更深层级后代（通过追溯 parent_uri 链）
            # ...
```

---

## 4. 过滤逻辑

### 4.1 `append_from_details` 函数

该函数负责根据查询模式过滤实体：

```python
def append_from_details(
    details: List[EntityDetailResult],
    billname: str,
    anchor_uri: str = "",
    is_sub_table_query: bool = False,
    relation_table_uris: Set[str] = None,
    is_descendant_only_query: bool = False,
    # ...
) -> None:
```

### 4.2 过滤规则

| 查询模式 | 过滤条件 |
|---------|---------|
| **孙表+** | `is_anchor_entity` 必须为 True |
| **子表** | `is_anchor_entity` OR `is_relation_table` OR `is_descendant_of_sub` |
| **主表** | `billname` 包含于实体名称 OR 是锚点 OR 是关联关系表 |

```python
if is_descendant_only_query:
    # 孙表+：只返回锚点实体本身
    if not is_anchor_entity:
        continue
elif is_sub_table_query:
    # 子表：返回锚点实体或子表后代
    if not is_anchor_entity and not is_relation_table and not entity_result.is_descendant_of_sub:
        continue
else:
    # 主表：名称匹配或锚点或关联关系表
    if billname not in gn and gn not in billname and not is_relation_table and not is_anchor_entity and not is_descendant_entity:
        continue
```

### 4.3 域名校验

```python
def _entity_domain_matches(entity_db_json: str, ref_domain: str) -> bool:
    """
    判断实体的 domain 是否与参考 domain 匹配。
    用于过滤掉共享同一 boId 但属于不同 domain 的客开扩展实体。
    """
```

---

## 5. Flow 1 详解（带 query-uri）

### 5.1 流程图

```
┌─────────────────────────────────────┐
│  用户传入 --query-uri              │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Step1: fetch_business_object_by_uri │
│  (searchByName + uri)              │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Step2: getEntityListByBOId        │
│  获取完整实体树                      │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Step3: 层级检测                    │
│  从 details 中查找 query_uri 的 pcr │
└──────────────┬──────────────────────┘
               │
       ┌───────┴───────┐
       │               │
   pcr >= 3        pcr == 2
       │               │
       ▼               ▼
┌──────────┐    ┌──────────────┐
│孙表+查询 │    │  子表查询    │
│仅返回自身 │    │返回子表+后代 │
└──────────┘    └──────────────┘
```

### 5.2 关键代码

```python
# 判断查询对象的层级
is_descendant_only_in_uri = False
is_sub_table_level_query = False
if details:
    for d in details:
        if d.uri == query_uri_param:
            query_pcr = d.parent_child_relationship
            if query_pcr is not None:
                if query_pcr >= 3:
                    is_descendant_only_in_uri = True
                elif query_pcr == 2:
                    is_sub_table_level_query = True
            break

# 根据层级处理
if is_descendant_only_in_uri:
    # 孙表+：只返回查询对象本身
    append_from_details(..., is_descendant_only_query=True)
elif is_sub_table_level_query:
    # 子表：返回子表 + its children
    relation_table_uris = {d.uri for d in details if d.parent_uri == query_uri_param}
    append_from_details(..., is_sub_table_query=True, relation_table_uris=relation_table_uris)
```

---

## 6. Flow 2 详解（仅 billname）

### 6.1 流程图

```
┌─────────────────────────────────────┐
│  用户仅传入 --allbillname          │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Step1: fetch_business_object_by_name│
│  searchByName API                  │
└──────────────┬──────────────────────┘
               │
        ┌──────┴──────┐
        │             │
    多义词        唯一匹配
        │             │
        ▼             ▼
┌─────────────┐  ┌─────────────────────┐
│多义词选择   │  │ Step2: getEntityListByBOId │
│等待用户确认 │  └──────────┬──────────┘
└─────────────┘             │
                           ▼
               ┌─────────────────────────┐
               │ Step3: 层级检测          │
               │ 从 details 中匹配 billname │
               └──────────┬──────────────┘
                          │
              ┌───────────┼───────────┐
              │           │           │
          主表查询     子表查询     孙表+查询
              │           │           │
              ▼           ▼           ▼
        返回所有     返回子表+    仅返回
        层级实体     子表后代     查询对象
```

### 6.2 层级检测逻辑

```python
# 从 details 中找到匹配 billname 的实体
queried_uri_for_level = ""
for d in details:
    d_json = uri_fetch(d.uri)
    bill_node, _ = _find_best_bill_node(d_json)
    bill_name = bill_node.displayName or bill_node.title
    if bill_name == billname:
        queried_uri_for_level = d.uri
        break

# 根据 pcr 判断查询类型
if details and queried_uri_for_level:
    for d in details:
        if d.uri == queried_uri_for_level:
            pcr = d.parent_child_relationship
            if pcr == 2:
                is_sub_table_query = True
            elif pcr >= 3:
                is_descendant_only_query = True
```

### 6.3 子表后代标记（Flow 2 特有）

```python
if is_sub_table_query and query_uri and details:
    relation_table_uris = set()
    for d in details:
        if d.parent_uri == query_uri:
            d.is_descendant_of_sub = True
            relation_table_uris.add(d.uri)
        elif d.parent_uri:
            # 追溯 parent_uri 链，标记更深层级后代
            parent_uri = d.parent_uri
            visited = set()
            while parent_uri:
                if parent_uri in visited:
                    break
                visited.add(parent_uri)
                for parent_d in details:
                    if parent_d.uri == parent_uri and parent_d.parent_uri == query_uri:
                        d.is_descendant_of_sub = True
                        relation_table_uris.add(d.uri)
                        break
                # 继续向上追溯
                parent_uri = parent_d.parent_uri
```

---

## 7. composition_child_uris_from_entity_db_json

该函数从实体 JSON 中解析组合子实体 URI：

```python
def composition_child_uris_from_entity_db_json(json_str: str) -> List[str]:
    """
    支持两种来源：
    1. associationAttributes 中的 composition 类型
    2. children 数组中的子实体
    """
```

---

## 8. 使用示例

### 8.1 主表查询

```bash
# 查询付款单（主表），返回所有层级实体
bash run_query.sh --allbillname "付款单" --query-uri "earap.payment.PaymentHeader" --is-include-sub Y
# 结果：7个实体（主表+3个子表+2个孙表+1个曾孙表）
```

### 8.2 子表查询

```bash
# 查询付款单明细信息（子表），返回子表及其孙表
bash run_query.sh --allbillname "付款单明细信息" --is-include-sub Y
# 结果：4个实体（子表 PaymentBody + 2个孙表 + 1个曾孙表）
```

### 8.3 孙表查询

```bash
# 查询付款单代扣税信息（孙表），仅返回自身
bash run_query.sh --allbillname "付款单代扣税信息" --is-include-sub Y
# 结果：1个实体（孙表 PaymentWithHoldingTax）
```

### 8.4 独立孙表查询

```bash
# 查询计税结果（税务引擎中的孙表）
bash run_query.sh --allbillname "计税结果" --is-include-sub Y
# 结果：2个实体（孙表 TaxCalculationResult + 曾孙表 TaxCalculationResultDetail）
```

---

## 9. 注意事项

### 9.1 queryByUri 响应不包含层级信息

`queryByUri` API 返回的 JSON 中 **没有** `parentChildRelationship` 字段。层级信息只能从 `getEntityListByBOId` 的响应中获取。

### 9.2 主表关联表过滤

某些关联关系表（如"资金付款单与第三方关联关系表"）通过外键引用主表。子表查询时应排除这类实体：

```python
if main_uri and is_sub_table_query:
    is_main_table_relation_entity = is_main_table_relation(entity_db_json, main_uri)
```

### 9.3 域名校验

共享同一 `boId` 的实体可能属于不同 domain。层级检测时会使用锚点实体的 domain 进行过滤，排除客开扩展实体。

### 9.4 多义词处理

当 `searchByName` 返回多个候选时，Flow 2 会返回多义词列表让用户选择。用户需指定 `--query-uri` 重新查询。

---

## 10. 附录：API 响应结构

### 10.1 getEntityListByBOId 响应

```json
{
  "data": {
    "entities": [
      {
        "uri": "earap.payment.PaymentHeader",
        "root": true,
        "children": [
          {
            "uri": "earap.payment.PaymentBody",
            "parentChildRelationship": 2,
            "children": [
              {
                "uri": "earap.payment.PaymentWithHoldingTax",
                "parentChildRelationship": 3
              }
            ]
          }
        ]
      }
    ]
  }
}
```

### 10.2 关键字段说明

| 字段 | 含义 |
|------|------|
| `uri` | 实体URI |
| `root` | 是否为主表 |
| `parentChildRelationship` | 层级关系 (1/2/3/4) |
| `children` | 子实体数组 |
| `parent_uri` | 父实体URI（代码中追踪） |
