---
name: iuap-c-report-phase3-sqlbuild
description: >
  旗舰版报表 SQL 生成 - 阶段三：SQL 构建。
  按顺序构建 JOIN（主表→特征表→平行表→参照表→子表），处理枚举字段转换。
  当完成字段分析后需要生成 SQL 时触发。
tags:
  - bip
  - report
  - sql
  - phase3
version: 1.0.0
---

# 阶段三：SQL 构建

> 本技能是报表 SQL 生成流程的第三阶段，负责生成完整的 SQL 语句。

## 阶段目标

- 按正确顺序构建 JOIN（主表 → 特征表 → 平行表 → 参照表 → 子表）
- 生成完整的 SELECT 列表
- 处理枚举字段的 CASE WHEN 转换

## 输入

| 输入项 | 来源 | 说明 |
|--------|------|------|
| 字段映射表 | 阶段二传递 | 完整的字段类型分析结果 |
| 元数据 | 阶段二传递 | entities_updated.json |
| 主表信息 | 阶段二传递 | schema、tableName、uri |

## 步骤 5：按顺序构建 JOIN

### JOIN 顺序（强制）

```
1. FROM 主表（第一个）
2. LEFT JOIN 特征表（按字段映射表顺序）
3. LEFT JOIN 平行表
4. LEFT JOIN 参照表（档案表）
5. LEFT JOIN 子表
6. LEFT JOIN 子表的子表（多级子表）
```

### 关联条件规则

| 关联类型 | JOIN 条件 |
|---------|----------|
| 主表 → 参照表 | `主表.外键字段 = 参照表.id` |
| 主表 → 子表 | `主表.id = 子表.外键字段` |
| 主表 → 平行表 | `主表.id = 平行表.id` |
| 主表 → 特征表 | `主表.特征组ID字段 = 特征表.id` |
| 子表 → 子表 | `下游子表.sourceautoid = 上游子表.id` |
| 跨表关联（档案引用） | `A表.档案字段 = B表.档案字段`（比较档案ID，非特征组ID） |

### 跨表档案关联规范

> ⚠️ **当两个表通过档案字段关联时，必须关联档案ID而非特征组ID**

```
错误示例（关联两个特征组ID）：
  LEFT JOIN st_materialout mo ON mo.materialOutDefineCharacter = spr.storeProRecordDefineCharacter
  原因：materialOutDefineCharacter 和 storeProRecordDefineCharacter 是不同特征组的ID

正确示例（关联档案ID）：
  -- 先JOIN各自的特征表
  LEFT JOIN st_storeprorecord_character_define_1 spr_ch ON spr_ch.id = spr.storeProRecordDefineCharacter
  LEFT JOIN st_materialout_character_define_1 mo_ch ON mo_ch.id = mo.materialOutDefineCharacter
  -- 再通过档案ID关联
  ON mo_ch.vcol2 = spr_ch.vcol1  -- vcol1/vcol2都是FZ02档案引用，比较档案ID
```

**判断依据**：检查字段的 `biztype` 是否为 `"quote"`，如果是则该字段存储的是档案ID。

### 【强制】特征表字段取值规则（SELECT字段来源）

> ⚠️ **当 SELECT 字段来自 `biztype=quote` 的特征表字段时，必须添加第二层 JOIN 到档案表！**

```
场景：报表需要展示"计划批次号"（FZ02档案引用）
错误：SELECT spr_ch.vcol1  -- vcol1 存储的是档案ID，不是名称！
正确：SELECT spr_bcd.name  -- 需要 JOIN 档案表，用 name 显示
```

**两层 JOIN 模板**：
```sql
-- 第一层：主表 → 特征表
LEFT JOIN ustock.storeprorecord_character_define_1 AS spr_ch
    ON spr.storeProRecordDefineCharacter = spr_ch.id

-- 第二层：特征表 → 档案表（biztype=quote 必需）
LEFT JOIN iuap_apdoc_basedoc.bd_cust_doc AS spr_bcd
    ON spr_ch.vcol1 = spr_bcd.id  -- vcol1 存储的是档案ID

-- SELECT 使用档案表字段
SELECT spr_bcd.name AS "批次号"
```

**biztype 与 SQL 字段来源对应**：

| biztype | SELECT 字段来源 | 示例 |
|---------|----------------|------|
| `quote` | **档案表.name/code** | `bd_cust_doc.name` |
| `number` | 特征表字段 | `spr_sub_char.ncol49` |
| `text` | 特征表字段 | `spr_ch.vcol55` |

### FROM/JOIN 示例

```sql
FROM scm_purchase.po_order AS po_order
LEFT JOIN scm_purchase.po_order_character_define_1 AS po_order_char1
    ON po_order.iPurchaseCharacterId = po_order_char1.id
LEFT JOIN iuap_apdoc_coredoc.merchant AS merchant
    ON po_order.iCustomerId = merchant.id
LEFT JOIN scm_purchase.po_order_detail AS detail
    ON po_order.id = detail.iOrderId
```

### Schema/表名规则

| SQL 位置 | schema 来源 | tableName 来源 |
|---------|------------|---------------|
| FROM 主表 | `entities[].schema` | `entities[].tableName` |
| JOIN 参照表 | `referenceStructure.scheme` | `referenceStructure.tableName` |
| JOIN 子表 | 子表实体的 `schema` | 子表实体的 `tableName` |
| JOIN 特征表 | 主表的 `schema` | **必须从 `characterFields[].allTables` 获取** |
| JOIN 平行表 | 主表的 `schema` | 字段自身的 `tableName`（含 `parallel`） |

> ⚠️ **【强制】特征表表名必须从元数据获取，禁止猜测！**
> - 获取路径：`entities[].attributes[].characterFields[].allTables`
> - 示例：`st_storeprorecord.storeProRecordDefineCharacter.characterFields[].allTables` 返回 `["storeprorecord_character_define_1"]`
> - ❌ **禁止凭经验猜测**：看到 type 是 `st.xxx.XXX` 就猜测表名是 `xxx_character_define_1`
> - ❌ **禁止猜测表名前缀**：必须用 `allTables` 返回的完整表名（含 `st_` 前缀等）

## 步骤 6：枚举字段转换

### 检查与转换逻辑

1. 遍历「字段映射表」每个字段
2. 检查字段是否存在 `enums` 数组
3. **如果有枚举**：生成 `CASE WHEN` 语句

```sql
CASE po_order.bill_status
  WHEN '0' THEN '新建'
  WHEN '1' THEN '已审核'
  WHEN '2' THEN '已完成'
  ELSE po_order.bill_status
END AS "订单状态"
```

4. **如果没有枚举**：直接使用

```sql
表名.dbColumnName AS "显示名"
```

### SELECT 列表示例

```sql
SELECT
  po_order.bill_no AS "单据编号",
  po_order.vouchdate AS "单据日期",
  CASE po_order.bill_status
    WHEN '0' THEN '新建'
    WHEN '1' THEN '已审核'
    WHEN '2' THEN '已完成'
    ELSE po_order.bill_status
  END AS "订单状态",
  merchant.cCode AS "客户编码",
  merchant.cName AS "客户名称",
  detail.material_code AS "物料编码",
  detail.material_name AS "物料名称",
  detail.qty AS "数量"
```

## 步骤 7：生成 WHERE 子句

### WHERE 条件规范

| 条件 | 何时添加 | 值 |
|------|---------|---|
| **租户过滤** | **默认必带** | `ytenant_id = 'var$(租户id)'` |
| 删除标记 | 用户明确要求时 | `IFNULL(dr, 0) = 0` |
| 启用状态 | 用户明确要求时 | `bEnable = 1` |

> **重要**：校验时 `var$(租户id)` 由 `db_query.py` 自动替换为配置中的 `YONBIP_TENANT_ID`。

### 禁止行为

- ❌ 根据字段名猜测添加条件
- ❌ 除租户过滤外添加未明确要求的条件

## 完整 SQL 示例

```sql
SELECT
  po_order.bill_no AS "单据编号",
  po_order.vouchdate AS "单据日期",
  CASE po_order.bill_status
    WHEN '0' THEN '新建'
    WHEN '1' THEN '已审核'
    WHEN '2' THEN '已完成'
    ELSE po_order.bill_status
  END AS "订单状态",
  merchant.cCode AS "客户编码",
  merchant.cName AS "客户名称",
  detail.material_code AS "物料编码",
  detail.material_name AS "物料名称",
  detail.qty AS "数量"
FROM scm_purchase.po_order AS po_order
LEFT JOIN iuap_apdoc_coredoc.merchant AS merchant
    ON po_order.iCustomerId = merchant.id
LEFT JOIN scm_purchase.po_order_detail AS detail
    ON po_order.id = detail.iOrderId
WHERE po_order.ytenant_id = 'var$(租户id)'
  AND IFNULL(po_order.dr, 0) = 0
ORDER BY po_order.vouchdate DESC
```

## 步骤 8：反向生成 Excel 确认单（可选）

根据字段映射表反向生成 Excel 文件，供用户确认字段映射关系。

### 执行脚本

```bash
python scripts/field_mapping_to_excel.py output/字段映射表.md -o output/确认单.xlsx
```

### 输出内容

| Sheet | 内容 |
|-------|------|
| 字段映射 | 报表字段与数据库字段对应关系 |
| 业务对象 | 所有涉及的表信息 |
| SQL参考 | JOIN 语句模板 |

## 输出产物

| 文件 | 说明 |
|------|------|
| `report_sql_output/{报表名}_{时间戳}.sql` | 未校验的完整 SQL |
| `report_sql_output/{报表名}_说明_{时间戳}.md` | 说明文档骨架 |
| `output/{报表名}_确认单_{时间戳}.xlsx` | （可选）字段映射确认单 |

## ⚠️ 阶段三完成前必须确认（门禁检查）

> **【强制】完成 SQL 生成后，必须逐项检查以下内容：**

| 序号 | 检查项 | 要求 |
|:---:|--------|------|
| 1 | **输出目录** | 文件必须写入 `report_sql_output/` 目录，**禁止写入 `output/`** |
| 2 | **文件名时间戳** | 必须包含 `_YYYYMMDD_HHMMSS` 时间戳 |
| 3 | 租户过滤 | 所有表都包含 `ytenant_id = 'var$(租户id)'` |
| 4 | schema 前缀 | 所有表名必须带 schema（如 `ustock.xxx`） |
| 5 | 字段映射 | 所有字段必须来自元数据，禁止臆造 |
| 6 | **WHERE条件参照表** | 所有WHERE条件中的参照字段（如部门、库存组织）必须先JOIN对应档案表 |

> ❌ **常见错误**：输出到 `output/` 目录且不带时间戳

## 禁止事项

- ❌ 表名缺 schema（前缀必须完整）
- ❌ 特征表用错表名（**必须从 `characterFields[].allTables` 获取，禁止猜测表名**）
- ❌ 枚举值直接显示 code 未转换
- ❌ 臆造不存在的字段
- ❌ 凭 type 字符串（如 `st.materialout.XXX`）猜测表名
- ❌ 跨表关联时直接关联两个特征组ID（如 `A.character = B.character`），必须通过档案ID关联
- ❌ WHERE条件中的参照字段未JOIN档案表（如直接用 `department = 'xxx'` 而未JOIN部门表）
- ❌ **SELECT 字段直接取特征表 vcol 字段**：当 `biztype=quote` 时，`vcol` 字段存储的是档案ID，必须再 JOIN 档案表获取 name/code

## 下一步

将以下信息传递给 **阶段四（iuap-c-report-phase4-validdeliv）**：
- SQL 文件路径
- 字段映射表
- 说明文档骨架
