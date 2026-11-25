---
name: iuap-c-report-phase2-fieldana
description: >
  旗舰版报表 SQL 生成 - 阶段二：字段分析与预处理。
  逐个分析字段来源（主表/参照表/子表/特征表/平行表），执行特征表校验。
  当用户提供元数据后需要分析字段映射时触发。
tags:
  - bip
  - report
  - sql
  - metadata
  - phase2
version: 1.0.0
---

# 阶段二：字段分析与预处理

> 本技能是报表 SQL 生成流程的第二阶段，负责分析每个字段的来源和类型。

---

## ⚠️ 常见错误清单（执行前必检）

> **每条都要逐项检查通过后才能进入下一步！**

| # | 错误类型 | 错误示例 | 正确做法 |
|---|---------|---------|---------|
| 1 | **参照表 schema 用错** | `productcenter.product` | 必须是 `referenceStructure.scheme + tableName` → `iuap_apdoc_coredoc.product` |
| 2 | **参照表用了 uri** | `aa.merchant.Merchant.merchant` | 禁止用 `uri`，要用 `scheme.tableName` |
| 3 | **特征表表名用错** | `st_storeprorecord.character_define` 或 `materialout_character_define_1` | 用 `characterFields[].allTables[0]` → `st_storeprorecords_character_define_1`（含 `st_` 前缀） |
| 4 | **猜测 dbColumnName** | 自创 `materialName` | 必须用元数据中的 `dbColumnName` |
| 5 | **凭 type 猜测特征表名** | `type=st.materialout.XXX` → 猜测表名是 `materialout_character_define_1` | 必须从 `allTables` 获取完整表名 `st_materialout_character_define_1` |
| 6 | **跨表关联条件错误** | 两个不同特征组的ID相等 | 特征组ID不能直接关联，需通过档案表ID关联 |
| 7 | **WHERE条件遗漏参照表** | 直接用 `department = 'xxx'` | 参照字段必须先JOIN档案表，再用 `code` 筛选 |

---



## 阶段目标

- 建立完整的「字段映射表」
- 识别每个字段属于哪种表类型（主表/参照表/子表/特征表/平行表）
- **提取表关联关系**：主子表关联、跨表关联、参照表关联、特征表关联
- **提取计算规则**：聚合计算（GROUP BY）、CASE WHEN 条件
- 执行特征表校验（当存在 character_define 时）
- **字段预检验**：基于元数据校验映射表中的每个字段是否真实存在
- **WHERE 字段校验**：确保筛选条件字段出现在 SELECT 中

## 输入

| 输入项 | 来源 | 说明 |
|--------|------|------|
| 元数据 JSON | output/entities.json | **阶段一原始输出**，包含完整业务对象元数据 |
| 解析结果 JSON | output/parse_copy.json | **阶段一原始输出**，包含 columnFieldMapping |

### ⚠️ 强制规范：必须从原始文件读取数据

> **禁止**手动输入或复制字段数据！必须从上述两个 JSON 文件中读取。

#### entities.json 读取规范

```json
{
  "entities": [
    {
      "tableName": "st_storeprorecords",      // 表名
      "billName": "产品入库子表",                // 业务对象名称
      "uri": "st.storeprorecord.StoreProRecords", // URI
      "schema": "ustock",                       // 数据库 schema
      "attributes": [...],                       // 字段列表
      "characterFields": [...]                  // 特征表字段（嵌套在 attributes 中）
    }
  ]
}
```

#### parse_copy.json 读取规范

```json
{
  "sheets": [{
    "columnFieldMapping": [
      {
        "columnName": "物料名称",              // 报表列名
        "mappingType": "reportColumn",         // 字段来源类型
        "sourceDescription": "产品入库子表.物料.物料名称",  // 原始描述
        "fieldPath": "产品入库子表.物料.物料名称",          // 字段路径
        "steps": [
          {
            "step": 1,
            "billName": "产品入库子表",       // 业务对象名称
            "tableName": "st_storeprorecords", // 数据库表名
            "fieldName": "物料"                // 字段名称
          }
        ]
      }
    ]
  }]
}
```

#### mappingType 字段来源类型

| mappingType | 说明 | 来源 |
|------------|------|------|
| `reportColumn` | 报表展示列 | Excel 表头 |
| `relatedField` | 业务对象关联字段 | businessObjects.relatedFields |
| `queryCondition` | 查询筛选条件 | queryCondition.queryFieldsDetail |

#### 字段读取优先级

1. **从 parse_copy.json 的 `columnFieldMapping[].steps[0]` 读取**：
   - `steps[0].billName` → 业务对象名称
   - `steps[0].tableName` → 数据库表名
   - `steps[0].fieldName` → 字段名称

2. **从 entities.json 验证字段是否存在**：
   - 先搜索 `attributes[]` 中的 `displayName`
   - 找不到时搜索 `characterFields[]` 中的 `displayName`

## 步骤 3：逐个字段来源分析

> ⚠️ **强制要求**：字段分析必须依赖元数据，禁止猜测！

对**每个**报表字段逐个执行以下分析：

### 分析动作

| 动作 | 操作 |
|------|------|
| 1 | 在元数据 `entities[].attributes[]` 中通过 `displayName` **精确匹配或模糊匹配**该字段 |
| 2 | **必须使用匹配到的字段对象中实际返回的 `dbColumnName`**，不能猜测、推断、拼接驼峰命名 |
| 3 | 记录匹配结果的：`displayName`、`tableName`、`schema`、`dbColumnName`、`referenceStructure`、`enums` |
| 4 | 根据 `tableName` 判断字段类型 |
| 5 | 记入「字段映射表」 |

### 字段匹配规范（强制）

```
【禁止】AI 猜测 dbColumnName
  ✗ 看到"资金组织" → 猜测字段名是 source_org
  ✗ 看到"协议号" → 猜测字段名是 protocolnumber
  ✗ 看到"物料编码" → 猜测字段名是 material_code
  ✗ 根据中文含义自行推断驼峰命名

【必须】使用元数据返回的实际值
  ✓ 在 entities.json 中找到 displayName="资金组织id" 的字段
  ✓ 取其 dbColumnName = "accentity"
  ✓ 在字段映射表中记录: 资金组织id → accentity

【必须】参照字段需要 JOIN 参照表
  ✗ st_materialouts.material_code (不存在!)
  ✓ st_materialouts.iProductid (物料ID) → JOIN bd_material 获取物料编码
```

### ⚠️ 常见错误：直接使用参照字段

很多字段（如物料编码、部门名称）不是主表/子表的直接字段，而是**参照字段**：

| 场景 | 错误做法 | 正确做法 |
|-----|---------|---------|
| 物料编码 | `st_materialouts.material_code` | `JOIN bd_material ON ...` |
| 部门名称 | `st_materialout.dept_name` | `JOIN org_orgs ON ...` |
| 客户名称 | `po_order.customer_name` | `JOIN merchant ON ...` |

**处理方式**：在字段映射表中标注为「参照字段」，需要单独 JOIN 参照表。

### 多字段匹配时的处理

当 `displayName` 模糊匹配到多个字段时：
1. 优先选择 `tableName` 等于主表或子表名的字段
2. 优先选择 `dbColumnName` 非空的字段
3. 如果仍有歧义，在字段映射表中标注「待确认」并列出所有候选

### 特征表字段搜索规则（字段在主表/子表中找不到时）

> ⚠️ **强制要求**：当字段在主表/子表的 `attributes` 中找不到时，必须自动搜索 `characterFields`（特征表）

当 `steps[0].tableName` 指定的表中找不到 `steps[0].fieldName` 时：

#### 搜索步骤

1. **定位实体**：在 entities.json 中找到对应实体（通过 billName 或 uri）
2. **搜索特征表**：在实体的 `attributes[].characterFields` 中搜索
   - 匹配 `characterFields[].displayName` 与 `fieldName`
   - 或匹配 `characterFields[].name` 与特征组代码
3. **提取特征表信息**：
   - `characterFields[].fieldName` → dbColumnName（如 `vcol1`）
   - `characterFields[].allTables[0]` → 特征表名（如 `storeprorecord_character_define_1`）
   - `characterFields[].biztype` → 判断是否需要 JOIN 档案表

#### 匹配示例

| fieldPath | 主表 attributes 搜索结果 | characterFields 搜索结果 | 最终映射 |
|-----------|------------------------|------------------------|---------|
| `产品入库主表.计划批次号` | ❌ 找不到 | ✅ `GYL_KC_01.displayName="计划批次号"` → `vcol1` | `storeprorecord_character_define_1.vcol1` |
| `产品入库子表.折算量` | ❌ 找不到 | ✅ `GYL_KC_06.displayName="折算量"` → `ncol49` | `storeprorecords_character_define_1.ncol49` |

#### 字段映射表标注格式

```markdown
| 序号 | 报表字段 | tableName | dbColumnName | 字段类型 | 状态 | 说明 |
|------|---------|-----------|-------------|---------|------|------|
| 3 | 批次号 | storeprorecord_character_define_1 | vcol1 | 特征表字段 | ✅ 已匹配 | 特征组 GYL_KC_01，FZ02档案引用 |
| 7 | 折算数量 | storeprorecords_character_define_1 | ncol49 | 特征表字段 | ✅ 已匹配 | 特征组 GYL_KC_06 |
```

#### biztype 判断规则

| biztype | 说明 | SQL 处理 |
|---------|------|---------|
| `quote` | 档案引用 | 需要两层 JOIN：主表→特征表→档案表 |
| `number` | 数值类型 | 直接取 vcolX 字段 |
| `text` | 文本类型 | 直接取 vcolX 字段 |

### 字段推断规则（匹配失败时的兜底策略）

> ⚠️ **适用场景**：字段在 entities.json 中完全匹配不到时，使用本规则推断

当 parse_copy.json 的 `columnFieldMapping[].fieldPath` 在 entities.json 中无法精确匹配时：

#### 推断步骤

1. **提取表名**：从 `fieldPath` 第一段获取业务对象名称（如"产品入库子表"）
2. **定位实体**：在 entities.json 中找到对应的实体（通过 billName 或 uri 匹配）
3. **推断字段**：基于字段语义在实体 attributes 中推断最可能的字段
   - 数量类字段 → 搜索 `qty`、`num`、`astn`、`amount` 等
   - 金额类字段 → 搜索 `mny`、`money`、`amount` 等
   - 日期类字段 → 搜索 `date`、`time`、`audit_date` 等
   - 批次类字段 → 搜索 `batch`、`batchno`、`sBatchNo` 等
4. **标注状态**：在字段映射表中标记为「⚠️ 待确定」，说明推断依据

#### 推断示例

| fieldPath | entities.json 搜索结果 | 推断结果 |
|-----------|----------------------|---------|
| `产品入库子表.物料 数量合计` | 无精确匹配 | `st_storeprorecords.qty` ⚠️ 待确定 |
| `材料出库单子表.主计量数量` | 无精确匹配 | `st_materialouts.qty` ⚠️ 待确定 |

#### 字段映射表标注格式

```markdown
| 序号 | 报表字段 | tableName | dbColumnName | 状态 | 推断依据 |
|------|---------|-----------|--------------|------|---------|
| 5 | 投入数量 | st_materialouts | qty | ⚠️ 待确定 | 基于"数量合计"语义推断，实际字段待校验 |
```

> **注意**：推断结果必须标注「⚠️ 待确定」，阶段四校验时必须验证推断是否正确

### 字段类型判定规则

| 判定条件 | 字段类型 | 处理方式 |
|---------|---------|---------|
| `tableName` === 主表 `tableName` | 主表字段 | 直接取 `dbColumnName` |
| `tableName` 包含 `character_define` | 特征表字段 | 必须 JOIN 对应特征表 |
| `tableName` 包含 `parallel` | 平行表字段 | JOIN 平行表，关联 `主表.id = 平行表.id` |
| `referenceStructure` 不为空 | **参照表字段** | **必须用** `referenceStructure.scheme.tableName`，禁止用 `uri` |
| `tableName` 在其他 `entities[].tableName` 中存在 | 子表字段 | JOIN 子表，关联 `主表.id = 子表.外键字段` |

### 跨表关联关系理解规范

> ⚠️ **强制要求**：分析关联关系必须严格遵循以下步骤

#### 1. 从Excel/需求中获取关联描述

Excel的"使用到的业务对象"区域会明确写出表间关系：
```
产品入库主表的计划批次号和材料出库单主表的计划批次号关联
```

#### 2. 定位关联字段

根据描述，在entities.json中找到对应的字段：
- 产品入库主表.计划批次号 → `storeprorecord_character_define_1.vcol1`（FZ02档案引用）
- 材料出库单主表.计划批次号 → `st_materialout_character_define_1.vcol2`（FZ02档案引用）

#### 3. 确定关联条件类型

| 字段类型 | 关联条件 |
|---------|---------|
| 两个档案引用字段 | **档案ID相等**：`A.vcol = B.vcol` |
| 档案引用 + 普通外键 | **外键 = 档案ID**：`A外键 = B.vcol` |
| 普通外键字段 | **外键相等**：`A.外键 = B.id` |

#### 4. 错误示例

```sql
-- ❌ 错误：关联两个不同特征组的ID
ON mo_main.materialOutDefineCharacter = spr_main.storeProRecordDefineCharacter
-- 原因：这是两个不同特征组表的ID，不应直接相等

-- ✅ 正确：通过档案ID关联
ON mo_ch.vcol2 = spr_ch.vcol1
-- 原因：两个特征表都引用FZ02档案，当档案ID相等时表示计划批次号相同
```

#### 5. 判断依据

在entities.json中检查 `biztype`：
- `biztype: "quote"` 表示该字段是档案引用，存储的是档案表ID
- 关联时应比较**档案ID**，而不是特征组ID

### 字段映射表模板

```markdown
| 序号 | 报表字段 | displayName匹配 | tableName | schema | dbColumnName | 字段类型 | 参照信息 |
|------|---------|----------------|-----------|--------|-------------|---------|---------|
| 1 | 单据编号 | 单据编号 | po_order | scm_purchase | bill_no | 主表字段 | - |
| 2 | 客户名称 | 客户 | iCustomerId | iuap_apdoc_coredoc | merchant.name | 参照字段 | **scheme.tableName** = iuap_apdoc_coredoc.merchant |
| 3 | 物料编码 | 物料编码 | po_order_detail | scm_purchase | material_code | 子表字段 | - |
| 4 | 采购特征 | 采购特征 | po_order_character_define_1 | scm_purchase | cDefineAtt14 | 特征表字段 | - |
```

> ⚠️ **参照表 schema 取值**：必须用 `referenceStructure.scheme`，不是 `referenceStructure.domain`！
> - `referenceStructure.scheme` = 数据库 schema（如 `iuap_apdoc_coredoc`）
> - `referenceStructure.domain` = BIP 领域标识（如 `productcenter`），不是数据库 schema！

## 步骤 3.2：提取表关联关系

> ⚠️ **强制要求**：阶段二必须提取并输出所有表间关联关系，供阶段三生成 JOIN 语句使用

### 数据来源

从 `parse_copy.json` 的 `sections.businessObjects` 中提取关联描述：

```json
{
  "sections": {
    "businessObjects": {
      "items": [
        {
          "billName": "产品入库子表",
          "tableName": "st_storeprorecords",
          "relation": "产品入库子表中的mainid和产品入库主表的id关联",
          "relatedFields": "物料、折算量"
        },
        {
          "billName": "材料出库单主表",
          "tableName": "st_materialout",
          "relation": "产品入库主表的计划批次号和材料出库单主表的计划批次号关联",
          "relatedFields": "部门、领用用途、生产计划数量、计划批次号"
        }
      ]
    }
  }
}
```

### 3.2.1 主子表关联

从 `relation` 字段中提取主子表关系：

```markdown
## 主子表关联
| 子表 | 子表外键 | 主表主键 | 关联描述 |
|------|---------|---------|---------|
| st_storeprorecords | mainid | st_storeprorecord.id | 产品入库子表中的mainid和产品入库主表的id关联 |
| st_materialouts | mainid | st_materialout.id | 材料出库单子表中的mainid和材料出库单主表中的id关联 |
```

**识别规则**：
- 子表 `relation` 描述格式：`{子表}.{外键字段}和{主表}.{主键字段}关联`
- 外键字段通常是 `mainid`（主子表标准外键）
- 主键字段通常是 `id`

### 3.2.2 跨业务对象关联

当 `relation` 描述涉及两个不同业务对象时，需要提取跨表关联信息：

```markdown
## 跨表关联
| 表A | 表B | 关联描述 | 关联类型 | 关联条件 |
|-----|-----|---------|---------|---------|
| st_storeprorecord | st_materialout | 产品入库主表的计划批次号和材料出库单主表的计划批次号关联 | 档案引用关联 | spr_ch.vcol1 = mo_ch.vcol2 |
```

**识别规则**：
- `relation` 描述格式：`{主表A}.{字段}和{主表B}.{字段}关联`
- 需要分析两个字段的类型（普通字段 or 档案引用）
- 如果都是档案引用（`biztype="quote"`），则通过档案ID关联

### 3.2.3 参照表关联

从 `entities.json` 的 `foreignKeys` 或 `attributes[].referenceStructure` 中提取：

```markdown
## 参照表关联
| 报表字段 | 主表外键字段 | 参照表 | schema | 参照表主键 | 显示字段 |
|---------|------------|--------|--------|----------|---------|
| 库存组织 | iOrgid | org_inventory | iuap_apdoc_basedoc | id | name |
| 部门 | department | org_orgs | iuap_apdoc_basedoc | id | name |
```

**识别规则**：
- 参照字段的 `dbColumnName` 即为主表外键字段
- `referenceStructure.tableName` 即为参照表名
- `referenceStructure.scheme` 即为 schema
- 参照表主键通常是 `id`
- 显示字段通常是 `name` 或 `code`

### 3.2.4 特征表关联

从 `entities.json` 的 `attributes[].characterFields` 中提取：

```markdown
## 特征表关联
| 主表字段 | 特征组ID字段 | 特征表 | 特征表字段 | 档案表 | 关联路径 | biztype |
|---------|------------|--------|---------|--------|---------|---------|
| 计划批次号 | storeProRecordDefineCharacter | storeprorecord_character_define_1 | vcol1 | bd_cust_doc | 主表→特征表→档案表 | quote |
```

**识别规则**：
- 特征组ID字段：`attributes[].dbColumnName`（如 `storeProRecordDefineCharacter`）
- 特征表名：`characterFields[].allTables[0]`
- 特征表字段：`characterFields[].fieldName`（如 `vcol1`）
- 当 `biztype="quote"` 时，需要两层 JOIN：
  - `主表.特征组ID字段 = 特征表.id`
  - `特征表.特征表字段 = 档案表.id`
- 当 `biztype` 为 `number`/`text` 时，直接取特征表字段值

## 步骤 4：特征表校验（条件执行）

**仅当存在特征表字段时执行**（`tableName` 包含 `character_define`）

### 校验流程

1. 查找特征组虚拟表名（从元数据 `entities` 中）
2. 生成特征表校验 SQL
3. 执行 `db_query.py` 获取 `real_table`/`real_column`
4. 更新「字段映射表」使用真实表名

### 校验 SQL 模板

```sql
SELECT field.real_table, field.real_column, field.field_name, field.comment
FROM {schema}.elastic_object obj
LEFT JOIN {schema}.elastic_field field ON obj.id = field.object_id
WHERE obj.table_name = '{特征组虚拟表名}'
  AND field.ytenant_id = '{YONBIP_TENANT_ID}'
```

### 特征表关联规则（从 `entities[].attributes[].characterFields` 获取）

#### 1. 特征单选（单选参照）

特征单选字段关联到 `iuap_apdoc_basedoc.bd_cust_doc` 档案表，需要 **两层 JOIN**：

```
主表.特征组ID字段 = 特征表.id
特征表.具体特征字段 = bd_cust_doc.id
```

**示例**（材料出库单主表的 `materialOutDefineCharacter` 字段）：
```sql
-- 特征组表（特征表）
LEFT JOIN ustock.st_materialout_character_define_1 main_out_ch
    ON main_out.materialOutDefineCharacter = main_out_ch.id

-- 档案表（bd_cust_doc）
LEFT JOIN iuap_apdoc_basedoc.bd_cust_doc main_out_bcd
    ON main_out_ch.vcol1 = main_out_bcd.id
```

**数据来源**：
- 特征组ID字段：`attributes[].dbColumnName`（如 `materialOutDefineCharacter`）
- 特征表名：`characterFields[].allTables[0]`（如 `st_materialout_character_define_1`）
- 具体特征字段：`characterFields[].fieldName`（如 `vcol1`）

**参照字段判断**：`characterFields[].biztype = "quote"` 时需要关联 `bd_cust_doc` 表

> **注意**：特征表名使用 `characterFields[].allTables[0]`，不是特征组虚拟表名

```
正确: characterFields[].allTables = "st_materialout_character_define_1"
错误: 使用主表字段的 tableName = "st_materialout"
```

#### 2. 特征多选（多选参照）

> 待完善

## 步骤 3.3：提取计算规则

> ⚠️ **强制要求**：阶段二必须提取并输出所有计算规则，供阶段三生成 SQL 使用

### 数据来源

从 `parse_copy.json` 的 `columnFieldMapping` 中提取计算规则：

```json
{
  "columnFieldMapping": [
    {
      "columnName": "投入数量",
      "calculationRule": "SUM",
      "calculationRuleDescription": "数量合计",
      "fieldPath": "材料出库单子表.主计量数量"
    },
    {
      "columnName": "区间数量",
      "calculationRule": "CASE WHEN",
      "calculationRuleDescription": "判断产品入库子表中主计量数量合计在哪个范围内取哪个范围，1、X≤50KG以下；2、50KG<X≤100KG；3、100KG<X≤300KG；4、300KG<X≤500KG；"
    }
  ]
}
```

### 3.3.1 聚合计算规则

从 `calculationRule` 中识别聚合类型：

```markdown
## 聚合计算规则
| 报表字段 | 计算类型 | 被聚合字段 | 分组字段 | 说明 |
|---------|---------|-----------|---------|------|
| 投入数量 | SUM | 材料出库单子表.主计量数量 | 计划批次号 | 按批次号汇总出库数量 |
| 产出数量 | SUM | 产品入库子表.折算量 | 计划批次号 | 按批次号汇总入库数量 |
| 折算数量 | SUM | 产品入库子表.折算量 | 计划批次号 | 按批次号汇总折算数量 |
```

**识别规则**：
- `calculationRule = "SUM"` → 聚合函数为 `SUM`
- `calculationRule = "COUNT"` → 聚合函数为 `COUNT`
- `calculationRule = "AVG"` → 聚合函数为 `AVG`
- `calculationRule = "MAX"` → 聚合函数为 `MAX`
- `calculationRule = "MIN"` → 聚合函数为 `MIN`
- 分组字段通常是关联键（如批次号）或业务主维度

**分组字段确定方式**：
1. 从报表业务逻辑推断（通常按批次号或物料分组）
2. 从 `effectiveFields` 中识别主维度字段
3. 标注「⚠️ 待确定」，由用户确认

### 3.3.2 CASE WHEN 条件

从 `calculationRule = "CASE WHEN"` 的 `calculationRuleDescription` 中提取条件分支：

```markdown
## CASE WHEN 条件
| 报表字段 | 被判断字段 | 条件分支 |
|---------|-----------|---------|
| 区间数量 | SUM(主计量数量) | WHEN x <= 50 THEN '1、X≤50KG以下' WHEN x <= 100 THEN '2、50KG<X≤100KG' WHEN x <= 300 THEN '3、100KG<X≤300KG' WHEN x <= 500 THEN '4、300KG<X≤500KG' |
```

**识别规则**：
1. 从 `calculationRuleDescription` 中提取数值范围
2. 提取条件分支的中文描述
3. 转换为 SQL 的 CASE WHEN 语法

**转换示例**：
```
Excel描述: 1、X≤50KG以下；2、50KG<X≤100KG；3、100KG<X≤300KG；4、300KG<X≤500KG；

SQL输出:
CASE
  WHEN SUM(主计量数量) <= 50 THEN '1、X≤50KG以下'
  WHEN SUM(主计量数量) <= 100 THEN '2、50KG<X≤100KG'
  WHEN SUM(主计量数量) <= 300 THEN '3、100KG<X≤300KG'
  WHEN SUM(主计量数量) <= 500 THEN '4、300KG<X≤500KG'
  ELSE '5、X>500KG'
END AS "区间数量"
```

### 3.3.3 公式计算

从 `calculationRule = "FORMULA"` 中识别公式字段：

```markdown
## 公式计算规则
| 报表字段 | 公式 | 说明 |
|---------|------|------|
| 制成率 | 产出数量/投入数量 | 产出/投入比例 |
```

**识别规则**：
- `calculationRule = "FORMULA"` → 公式计算
- `steps[].operator` → 运算符
- `steps[].operand` → 操作数

## 步骤 3.5：字段预检验

> ⚡ **强制要求**：字段映射表生成后，必须基于业务对象元数据进行校验

### 校验脚本

使用 `field_verify.py`（合并 phase1 + phase2 能力）执行校验：

```bash
# 方式1：校验映射表中的字段是否在元数据中存在
python scripts/field_verify.py --entities output/entities.json --mapping output/字段映射表.md

# 方式2：同时执行 WHERE 字段 → SELECT 校验
python scripts/field_verify.py --entities output/entities.json --mapping output/字段映射表.md --check-where

# 方式3：从 Excel 提取字段并验证元数据（字段发现）
python scripts/field_verify.py --entities output/entities.json --excel 报表模板.xlsx

# 方式4：完整校验（字段发现 + 映射表 + WHERE）
python scripts/field_verify.py --entities output/entities.json --mapping output/字段映射表.md --check-where --excel 报表模板.xlsx
```

### 校验逻辑

1. **元数据字段校验**：遍历字段映射表的每一行，验证 `tableName.dbColumnName` 是否在 `entities.json` 中真实存在
2. **模糊匹配推荐**：字段不存在时，使用 Levenshtein 编辑距离（≤2）推荐相似字段
3. **WHERE 字段校验**：提取「筛选条件」区域的字段，检查是否出现在 SELECT 报表字段中
4. **隐含字段跳过**：`dr`、`ytenant_id`、`ts` 等平台隐含字段不参与 WHERE 校验

### 校验伪代码

```
对于 字段映射表中 每行 (报表字段, tableName, dbColumnName):
    在 entities.json 中找到 tableName 对应的实体
    在实体的 attributes 中查找 dbColumnName
    如果找到了 → 标记为 ✅ 已匹配
    如果没找到 → 使用 Levenshtein 推荐相似字段，标记为 ❌ 未匹配
```

### 校验失败示例

```
❌ 发现 2 个未匹配的字段:

  ❌ 字段: mainid
     表: st_storeprorecords
     原因: 在元数据 attributes 中未找到 dbColumnName='mainid'
     提示: 实际的子表关联字段是 'iMainId'（带前缀 i）
     操作: 请在元数据中重新搜索正确的字段

  ❌ 字段: period
     表: st_storeprorecord
     原因: 在元数据 attributes 中未找到 period 字段
     提示: 实际的日期字段是 'vouchdate'
     操作: 请在元数据中重新搜索正确的字段
```

### 校验通过后才进入阶段3

- ✅ 所有字段都已在元数据中匹配到 → 进入阶段3
- ❌ 有未匹配字段 → 修正字段映射表后重新校验

## 步骤 3.6：WHERE 字段 → SELECT 校验

> ⚡ **强制要求**：字段映射表中的「筛选条件」字段必须出现在SELECT报表字段中

### 校验背景

SQL语义要求：WHERE条件引用的字段必须在SELECT中出现（非聚合场景）。此校验在字段分析阶段提前发现问题，避免到阶段三构建SQL后才暴露问题。

### 校验规则

| WHERE字段情况 | 处理方式 |
|--------------|---------|
| 在SELECT报表字段中 | ✅ 通过 |
| 是聚合函数内部字段（如SUM的字段） | ✅ 通过（不显式出现在SELECT也可） |
| 既不在SELECT也不在聚合中 | ❌ 报错，提示添加到SELECT或修改WHERE |

### 校验方式

```bash
# 单独执行 WHERE 字段校验
python scripts/field_verify.py --mapping output/字段映射表.md --check-where

# 同时执行元数据校验 + WHERE 字段校验
python scripts/field_verify.py --entities output/entities.json --mapping output/字段映射表.md --check-where
```

### 校验流程

1. 从字段映射表「筛选条件」区域提取所有字段引用（如 `hdr.dr`, `ast.customer`）
2. 从字段映射表「字段映射」区域提取所有报表字段
3. 检查每个WHERE字段是否在报表字段中出现
4. 如有缺失，输出缺失字段及修复建议

---

## 输出产物

| 文件 | 说明 |
|------|------|
| `字段映射表.md` | 完整的字段类型分析结果（含字段映射、表关联、计算规则） |
| `entities_updated.json` | 特征表真实表名已更新的元数据 |

### 字段映射表完整结构

```markdown
# 报表名称：投入产出批量消耗明细

## 一、字段映射表
| 序号 | 报表字段 | displayName匹配 | tableName | schema | dbColumnName | 字段类型 | 参照信息 |
|------|---------|----------------|-----------|--------|-------------|---------|---------|

## 二、表关联关系
### 2.1 主子表关联
| 子表 | 子表外键 | 主表主键 | 关联描述 |
|-----|---------|---------|---------|

### 2.2 跨表关联
| 表A | 表B | 关联描述 | 关联类型 | 关联条件 |
|-----|-----|---------|---------|---------|

### 2.3 参照表关联
| 报表字段 | 主表外键字段 | 参照表 | schema | 参照表主键 | 显示字段 |
|---------|------------|--------|--------|----------|---------|

### 2.4 特征表关联
| 主表字段 | 特征组ID字段 | 特征表 | 特征表字段 | 档案表 | 关联路径 | biztype |
|---------|------------|--------|---------|--------|---------|---------|

## 三、计算规则
### 3.1 聚合计算规则
| 报表字段 | 计算类型 | 被聚合字段 | 分组字段 | 说明 |
|---------|---------|-----------|---------|------|

### 3.2 CASE WHEN 条件
| 报表字段 | 被判断字段 | 条件分支 |
|---------|-----------|---------|

### 3.3 公式计算规则
| 报表字段 | 公式 | 说明 |
|---------|------|------|
```

## 字段类型速查

| 字段类型 | 识别方式 | 处理规则 |
|---------|---------|---------|
| **主表字段** | `tableName` = 主表 `tableName` | 直接取 `dbColumnName` |
| **参照表字段** | 存在 `referenceStructure` | **必须用** `referenceStructure.scheme + referenceStructure.tableName` 构建 JOIN 表名，禁止使用 `uri` 字段作为数据库表名 |
| **枚举字段** | 存在 `enums` 数组 | 必须用 `CASE WHEN` 将 code 转换为 name |
| **特征表字段** | `tableName` 含 `character_define` | 必须 JOIN 对应的特征表 |
| **平行表字段** | `tableName` 含 `parallel` | JOIN 平行表，关联 `主表.id = 平行表.id` |
| **子表字段** | 其他 `tableName`，且在 entities 中存在独立子表实体 | JOIN 子表，关联 `主表.id = 子表.外键字段` |

### 参照字段 JOIN 表名构建示例

```
正确：
  referenceStructure.scheme = "iuap_apdoc_coredoc"
  referenceStructure.tableName = "merchant"
  → JOIN 表名：iuap_apdoc_coredoc.merchant

错误（禁止）：
  uri = "aa.merchant.Merchant"  ← 这是BIP平台URI格式，不是数据库表名！
  → 错误写法：aa.merchant.Merchant.merchant
```

## 禁止事项

- ❌ **猜测 `dbColumnName`**：看到中文字段名后自行推断英文驼峰命名，必须使用元数据返回的实际值
- ❌ 臆造不存在的字段或 `dbColumnName`
- ❌ 跳过特征表校验（当存在 character_define 时）
- ❌ 使用特征组虚拟表名而非字段自身的 tableName
- ❌ **使用 `uri` 字段作为数据库表名**：`uri` 是 BIP 平台接口格式（如 `aa.merchant.Merchant`），必须使用 `referenceStructure.scheme + referenceStructure.tableName`（如 `iuap_apdoc_coredoc.merchant`）
- ❌ **凭 type 字符串猜测特征表名**：看到 `type=st.materialout.XXX` 就猜测表名是 `materialout_character_define_1`，必须从 `characterFields[].allTables` 获取完整表名（含 `st_` 前缀等）
- ❌ **跨表关联条件错误**：不能直接关联两个不同特征组的ID（如 `A.character = B.character`），必须通过档案ID关联（如 `A.vcol = B.vcol`）
- ❌ **WHERE条件遗漏参照表**：筛选条件中涉及参照字段（如部门、库存组织）时，必须在字段映射表中记录需要JOIN的档案表，不能直接用参照字段ID比较
- ❌ **遗漏主子表关联**：阶段二必须从 `parse_copy.json` 提取主子表关联关系，不能由阶段三自行推断
- ❌ **遗漏跨表关联**：当 Excel 中明确写了跨业务对象关联时，必须在字段映射表中记录，不能遗漏
- ❌ **遗漏聚合分组字段**：聚合计算必须标注分组字段（GROUP BY 依据），不能只写聚合函数不写分组
- ❌ **遗漏 CASE WHEN 条件**：当字段有 `calculationRule = "CASE WHEN"` 时，必须提取完整条件分支

## 下一步

将以下信息传递给 **阶段三（iuap-c-report-phase3-sqlbuild）**：
- 字段映射表（含字段映射、表关联、计算规则）
- 更新的元数据（entities_updated.json）
- 主表信息（schema、tableName、uri）

### 阶段二 → 阶段三信息传递清单

| 信息项 | 用途 | 来源 |
|--------|------|------|
| 字段映射表 | SELECT 列表生成 | 步骤3输出 |
| 主子表关联 | JOIN 子表 | 步骤3.2.1输出 |
| 跨表关联 | JOIN 关联表 | 步骤3.2.2输出 |
| 参照表关联 | JOIN 档案表 | 步骤3.2.3输出 |
| 特征表关联 | JOIN 特征表+档案表 | 步骤3.2.4输出 |
| 聚合计算规则 | GROUP BY + 聚合函数 | 步骤3.3.1输出 |
| CASE WHEN条件 | CASE WHEN语句 | 步骤3.3.2输出 |
| 公式计算规则 | 公式字段 | 步骤3.3.3输出 |
