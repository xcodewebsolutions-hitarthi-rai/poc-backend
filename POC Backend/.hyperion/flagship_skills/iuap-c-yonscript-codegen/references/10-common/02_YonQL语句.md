# YonSQL语句

> YonSQL 是 BIP 的查询语言，类似 MySQL 语法；平台执行方法名仍为 `ObjectStore.queryByYonQL`。
> 写 YonSQL 前先判断 `uri`、`domain`、字段、查询条件是否都已明确；任一项不明确时才调用 `iuap-c-metadata-info`。
>
> **⚠️ 元数据+YonSQL 关联**（参数来源、必填规则、三场景对比）已统一在 [../40-guides/01_metadata-yonsql-guide.md](../40-guides/01_metadata-yonsql-guide.md) 中维护。
> **⚠️ 技术规范**（URI 反引号、字段 name 属性、Boolean 1/0、查询形态、LEFT JOIN）见 [../00-governance/01_技术规范.md](../00-governance/01_技术规范.md)，**不在此重复**。
>
> 本文档**仅保留 YonSQL 语法本身**（模糊/分页/多表/聚合等），不重复元数据来源和取值规范。

---

## 目录

- [简单查询](#简单查询)
- [生产查询契约](#生产查询契约)
- [YonSQL语法](#yonsql语法)
- [嵌套子查询](#嵌套子查询)
- [特征字段查询](#特征字段查询)
- [常见问题](#常见问题)

---

## 简单查询

### ObjectStore.queryByYonQL

```javascript
// 本领域查询（domainKey可省略）
var res = ObjectStore.queryByYonQL('select name, code from pc.cls.ManagementClass where dr = 0');

// 跨领域查询（必须传入正确的domainKey）
var res = ObjectStore.queryByYonQL('select name from pu.purchaseorder.PurchaseOrder', 'upu');
```

---

## 生产查询契约

YonSQL 面向元数据实体，不面向数据库表。生产代码必须先满足以下契约，再拼装语句：

- `from` 后必须是元数据实体 URI，不支持视图；原厂单据必须传 `domainKey`。
- 生产环境禁止 `select *`，必须明确列举返回字段；`*` 只允许本地临时排查。
- 字段必须使用元数据 `attributes[].name`，禁止使用 `dbColumnName`。
- 元数据返回后必须先确认字段类型，再决定写法；基本类型、参照、特征、子表、平行表、多选引用、业务外键关联不能混用同一种字段路径。
- 查询字段、分组字段、排序字段不允许直接写穿透字段；需要穿透时优先使用嵌套子查询。
- `where` 右值不支持实体字段或普通函数作为右值；需要函数结果时优先在 JS 侧计算后用字面量，或使用已验证的子查询模式。
- DateTime 字面量必须加引号，例如 `pubts > '2025-06-23 19:19:30'`。
- `like`、`leftlike`、`rightlike` 后不要手写 `%`，平台会自动转换。
- `order by` 只能使用当前实体字段，不允许表达式或穿透字段。
- `group by` 字段必须是当前实体字段，不允许穿透字段、函数或表达式；查询中的非聚合列必须出现在 `group by` 中。
- YonSQL 当前定位为查询，不用于 `insert`、`update`、`delete`；实体增删改走 `ObjectStore` 实体操作能力。

### 元数据类型到写法

| 元数据类型 | 必须确认 | 默认写法 |
|---|---|---|
| 基本类型 | `attributes[].name`、数据类型 | 当前实体直接 `select field` |
| 参照 | 字段 `name`、`refUri`、目标实体字段 | 外层显式选参照字段，嵌套 `select id/code/name from refField` |
| 特征组 | 特征组字段、特征编码、特征类型 | 外层显式选特征组，内部按需嵌套 |
| 子表 | 主实体 `id`、子表运行时关联字段、子表字段 | 外层选 `id`，子表字段放内部 `select` |
| 平行表 | 关联键、平行表 URI/domain/字段 | 分步查询后在脚本合并；不默认 JOIN |
| 多选引用 | 运行时关联字段、实际存储档案字段、档案 `refUri` | 外层选 `id`，内部先选多选字段再嵌套档案字段 |
| 业务外键关联 | 存储目标实体 ID，但无 `refUri`、无原生关系 | 比较源/目标 `domain` 后自动分流 |

---

## YonSQL语法

### 模糊查询

| 类型 | 关键字 | 说明 |
|------|--------|------|
| 全模糊 | `like` | 自动加 %，`like '山'` 等同于 `like '%山%'` |
| 左模糊 | `leftlike` | `leftlike '河'` 等同于 `like '河%'` |
| 右模糊 | `rightlike` | `rightlike '东'` 等同于 `like '%东'` |

### 分页查询

```sql
select name, code from GT22161AT175.GT22161AT175.province
where dr = 0 order by cityCode asc limit 0, 10
```

`limit page, size` — 第一参数是页码（从0开始），第二参数是每页条数。

### 多表查询

前提：实体之间须有关联关系（主子表或单选引用关系），不需要写 `on` 关联条件。

> 架构建议：优先使用嵌套子查询表达参照穿透、主子表、多选引用。JOIN 只用于平台已验证的关联场景，不作为默认生成策略。

业务外键关联例外：如果字段只保存目标单据 ID，但元数据没有 `refUri`、主子、特征等原生关系，则必须按元数据 domain 自动分流。

- 先查源实体元数据和目标实体元数据，取 `_entityInfo.domain`。
- 同一 domain：可使用 LEFT JOIN，主表必须返回 `id` 且不设别名，从表必须设别名，结果为扁平结构。
- 不同 domain：禁止跨域 LEFT JOIN；先查本域主子数据并收集外键 ID，再用 `ObjectStore.queryByYonQL(sql, targetDomain)` 批量查目标域，最后脚本组装。
- 禁止凭 URI 前缀、字段名、单据名称判断是否跨域。

```sql
select son_id.main_id.mainCode as mainCode, son_id.mainName as mainName,
       son_id.sonCode, son_id.sonName
from GT22161AT175.GT22161AT175.grandson
left join son_id son on son_id = son.id
left join son_id.main_id main
```

> 结果集不能使用 `*`，必须写清楚属性名。
> LEFT JOIN 语法规范（主表不加别名、从表加别名）见 [../00-governance/01_技术规范.md 规范 6](../00-governance/01_技术规范.md#规范6yonsql-查询形态与-left-join-语法)。

### 示例

```javascript
// ✅ 正确：使用元数据中的字段
var sql = 'select code, name from pc.cls.ManagementClass where code = \'001\'';
var res = ObjectStore.queryByYonQL(sql, 'productcenter');

// ❌ 错误：臆想添加 dr 或 iDeleted
var sql = 'select code, name from pc.cls.ManagementClass where code = \'001\' and dr = 0';
```

### DateTime 条件

YonSQL DateTime 字段不要直接与 `now()` 比较。`now()` 会被平台当作字符串，可能报“不能转换为 DateTime 类型”。

```javascript
var nowDate = getCurrentDateTime(); // yyyy-MM-dd HH:mm:ss
var sql = 'select id from cpubase.supplycategory.Supplycategory where validtime is null or validtime > ' + quoteYonQL(nowDate);
```

涉及 API 脚本查询时，完整写法见 [14_API脚本查询与日志模式.md](14_API脚本查询与日志模式.md)。

### where 右值限制

YonSQL `where` 条件右值不要生成实体字段或普通函数。

```sql
-- ❌ 错误：右值是字段
select name from pc.product.Product where weight = netWeight

-- ❌ 错误：右值是函数，平台兼容性不稳定
select id from st.salesout.SalesOuts where year(createDate) = year(curdate())

-- ✅ 推荐：JS 侧算好字面量后拼入，或使用平台已验证的子查询模式
select id from st.salesout.SalesOuts where year(createDate) in (select year(curdate()) from st.salesout.SalesOuts)
```

---

## 嵌套子查询

嵌套子查询是公有云 YonSQL 查询参照、子表、多选引用的首选表达方式，返回结果按层级 JSON 组织。

### 参照穿透

对 `1:1` 参照字段做嵌套查询时，外层必须显式返回该参照字段，否则内部嵌套可能不生效。

```sql
-- ✅ 正确：外层显式包含 orgId，再从 orgId 中按需取字段
select id, code, orgId, (select id, code, name from orgId) as orgInfo
from pc.product.Product
where code = '001'
```

不要直接生成 `orgId.name` 这类穿透字段作为默认写法；社区文档已明确后续推荐嵌套查询。

### 主表查子表

对组合子表做嵌套查询时，外层必须包含主实体 `id`；外层不要再直接选择子表运行时关联字段，否则可能触发 `RoleA of composition can not be select/condition/join/groupby/having/orderby field`。

```sql
-- ✅ 正确：外层有 id，子表字段只放在内部 select
select id, code, (select id, materialId, productId from orderProduct) as orderProducts
from po.order.Order

-- ❌ 错误：外层直接选择子表关联字段 orderProduct
select id, code, orderProduct, (select id, materialId from orderProduct) as orderProducts
from po.order.Order
```

### 多选引用

多选引用通常由独立运行时关联字段承载。外层必须包含主实体 `id`；内部先返回多选字段，再对多选字段继续嵌套查询档案字段。

```sql
select id,
       (select productMore, (select id, code, name from productMore)
        from p42395_yonql_use_productMoreList) as products
from AT1FCF3BBE1C800004.AT1FCF3BBE1C800004.p42395_yonql_use
```

---

## 特征字段查询

特征字段本质上通常在 `1:1` 特征组对象中，不应默认按主表直属字段生成。必须先从元数据确认特征组字段、特征编码和引用字段结构。

### 推荐：按需嵌套穿透

```sql
select id, code, name, productCharacterDef,
       (select id,
               colortest1001,
               (select id, code, name from colortest1001),
               booleanKeyTest,
               wenben
        from productCharacterDef) as productCharacterDefInfo
from pc.product.Product
where code = '5732000002'
```

契约：

- 外层必须显式包含特征组字段，例如 `productCharacterDef`。
- 多选档案特征必须在内部查询中携带原字段，例如 `colortest1001`，再嵌套取 `id/code/name`。
- 文本、布尔、数值等普通特征字段在特征组内部按特征编码查询。
- 如果特征字段未赋值，平台可能不返回该字段；布尔特征未保存时，前端显示默认值不代表 YonSQL 可查询到。

### 不推荐：直接全量引用对象

```sql
select id, code, name, productCharacterDef
from pc.product.Product
where code = '5732000002'
```

该写法会返回特征组下较多系统冗余字段，多选档案也可能只是 ID 数组和系统拼接名称数组；只适合临时排查，不作为生产默认方案。

---

## 常见问题

**自建表和系统表是否支持关联查询？**
不支持。自建表 domainKey 为 `developplatform`（可省略），系统表须传对应领域 domainKey。如需跨表数据，分开查询后在脚本中合并。

**跨领域原厂表是否支持关联查询？**
不支持。不同领域 domainKey 不一致，且不在同一库中。

**原厂单据 domainKey 如何获取？**
F12 查看请求，从 header 中找到对应单据所在领域的 domainKey。
