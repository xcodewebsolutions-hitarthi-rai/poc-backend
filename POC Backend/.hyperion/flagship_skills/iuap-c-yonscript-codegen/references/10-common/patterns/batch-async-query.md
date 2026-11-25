# 批量异步查询模式（BATCH_ASYNC_QUERY）

> **加载时机**：用户需求命中"批量查询"、"列表回写"、"前端异步回调"、"rowIndex 回写"、"id→code 转换"
> **场景定位**：前端 List_VM.Extend.js / Card_VM.Extend.js 通过 `cb.rest.invokeFunction` 调后端 API 脚本，**一次性查多行数据**后**按行索引**回写。
> 这是 YonScript API 脚本最常见的实战模式。

---

## 实战案例

**`SxPriceDecisionMaterialAvgPriceQuery.js`** 是本模式的典型代表：
- 前端传入 N 行（每行含 `rowIndex`、`materialCode`、`supplierId`）
- 后端做 id→code 转换 + 批量 SQL 查询
- 按 rowIndex 拼装结果返回 `{"0": 8.5, "2": 12.3}`
- 前端按 rowIndex 精确回写

---

## 5 个核心子模式

```
BATCH_ASYNC_QUERY 模式分解
    │
    ├─ 模式 1: 入参校验（queryList 必填、缺参跳过）
    ├─ 模式 2: id→code 转换（参照字段 id 批量转业务码）
    ├─ 模式 3: SQL 注入防护（_wrapInQuotes + 字符串转义）
    ├─ 模式 4: 批量 SQL 查询（IN (...) + 3 个月时间窗口）
    └─ 模式 5: 按 rowIndex 回写结果（{"0": value, "2": value}）
```

---

## 模式 1：入参校验

### 业务场景

前端传入的 `queryList` 可能为空、缺字段、不合法，必须做防御性校验。

### 完整模板

```javascript
// ========== Step 1: 入参校验 ==========
var queryList = (request && request.queryList) || [];
if (!Array.isArray(queryList) || queryList.length === 0) {
    console.warn('【MyAPIHandler】queryList 为空');
    return { success: true, data: {} };  // ⭐ 空入参不报错，返回空数据
}

// 校验每行必填字段
var validCount = 0;
for (var i = 0; i < queryList.length; i++) {
    var item = queryList[i] || {};
    if (!item.materialCode || !item.supplierId) {
        console.warn('【MyAPIHandler】第 ' + i + ' 行缺参，跳过');
        continue;
    }
    validCount++;
}
if (validCount === 0) {
    return { success: true, data: {} };
}
```

### 关键点

| # | 关键点 | 原因 |
|---|--------|------|
| 1 | **空 queryList 返回 success:true** | 不阻塞前端，只是没数据 |
| 2 | **逐行校验必填** | 前端可能漏传 |
| 3 | **校验日志要带行号** | 便于排查 |
| 4 | **校验不通过要 continue** | 不能 throw，否则整批失败 |

---

## 模式 2：id→code 转换（参照字段 → 业务码）

### 业务场景

前端传的 `supplierId` 是**参照档案主键 id**（数字），而后端 SQL 关联的字段是**业务码 code**（字符串）。需要批量做 id→code 转换。

### 完整模板

```javascript
// ========== Step 2: 供应商 id → code 批量转换 ==========
// 原因：前端 supplierDocId 是参照字段，传的是供应商档案主键 id
//       而供应商物料价格业务对象里存的是 supplierCode
//       所以必须先批量查出 id→code 映射

// 2.1 收集去重
var supplierIds = [];
var supplierSet = {};
for (var i = 0; i < queryList.length; i++) {
    var sId = queryList[i] && queryList[i].supplierId;
    if (sId && !supplierSet[sId]) {
        supplierIds.push(sId);
        supplierSet[sId] = true;
    }
}
if (supplierIds.length === 0) {
    return { success: true, data: {} };
}

// 2.2 查供应商档案：id → code
// ⚠️ 门禁：这里用的是业务对象 URI + 元数据字段，不是数据库表查询
var vendorSql = 'select id, code from aa.vendor.Vendor where id in (' + _wrapInQuotes(supplierIds) + ')';
var vendorResult = ObjectStore.queryByYonQL(vendorSql, 'yssupplier');

var idToCodeMap = {};
if (vendorResult && Array.isArray(vendorResult)) {
    for (var i = 0; i < vendorResult.length; i++) {
        var row = vendorResult[i] || {};
        if (row.id && row.code) {
            idToCodeMap[row.id] = row.code;
        }
    }
}

if (Object.keys(idToCodeMap).length === 0) {
    console.warn('【MyAPIHandler】无可映射的供应商编码');
    return { success: true, data: {} };
}
```

### 关键点

| # | 关键点 | 原因 |
|---|--------|------|
| 1 | **收集去重** | 减少 SQL `IN` 列表长度 |
| 2 | **domain 用元数据** | 禁止凭 URI 前缀推导（门禁 1） |
| 3 | **id→code 映射要校验空** | 没映射到说明 supplierId 无效 |
| 4 | **SQL 拼装 IN (...) 简单列表** | 此处 supplierId 是数字，安全性较高 |
| 5 | **如含字符串字段需 _wrapInQuotes** | 见模式 3 |

---

## 模式 3：SQL 注入防护（_wrapInQuotes）

### 业务场景

IN 列表中包含**字符串**字段（如物料编码），直接拼接容易被 SQL 注入。

### 完整模板

```javascript
/**
 * 工具方法：用单引号包裹数组元素，构造 SQL IN 条件
 * @param {string[]} arr 字符串数组
 * @returns {string} 'a','b','c'
 */
function _wrapInQuotes(arr) {
    if (!arr || arr.length === 0) return "''";
    var result = [];
    for (var i = 0; i < arr.length; i++) {
        // ⭐ SQL 注入防护：单引号转义
        var v = String(arr[i] || '').replace(/'/g, "''");
        result.push("'" + v + "'");
    }
    return result.join(',');
}

// 使用示例
var sql = 'select materialCode, supplierCode from xxx.yyy.Entity ' +
          'where materialCode in (' + _wrapInQuotes(materialCodes) + ') ' +
          'and supplierCode in (' + _wrapInQuotes(supplierCodes) + ')';
```

### 关键点

| # | 关键点 | 原因 |
|---|--------|------|
| 1 | **必须用单引号包裹** | SQL 字符串字面量 |
| 2 | **单引号转义为两个单引号** | SQL 标准转义规则 |
| 3 | **`String()` 强制转换** | 防止数字/对象被强转 |
| 4 | **空数组返回 `''`** | SQL 语法合法（恒为假） |
| 5 | **数字字段不需要** | 但要保证是纯数字 |

### 反面教材

```javascript
// ❌ 错误 1：直接拼接字符串
var sql = "select materialCode from xxx.yyy.Entity where materialCode in ('" + materialCodes.join(',') + "')";
// 如果 materialCodes 含 'abc' OR 1=1' 就被注入了

// ❌ 错误 2：未转义单引号
var v = arr[i].replace(/'/g, '');  // 错误！应该转义为 ''

// ❌ 错误 3：未 String() 转换
var v = arr[i];  // 如果是对象，toString 结果不可控
```

---

## 模式 4：批量 YonQL 查询（IN + 时间窗口）

### 业务场景

按物料编码 + 供应商编码批量查近 3 个月内的价格，取最低均价。

### 完整模板

```javascript
// ========== Step 3: 批量 YonQL 查询 ==========
// 过滤：业务日期距今 ≤ 90 天
var now = new Date();
var threeMonthsAgo = new Date(now.getTime() - 90 * 24 * 60 * 60 * 1000);
var threeMonthsAgoStr = threeMonthsAgo.getFullYear() + '-' +
                        _pad(threeMonthsAgo.getMonth() + 1) + '-' +
                        _pad(threeMonthsAgo.getDate()) + ' 00:00:00';

var sql = 'select materialCode, supplierCode, avgUsagePrice ' +
          'from xxx.yyy.SupplierMaterialInfo ' +
          'where businessDate >= ' + "'" + threeMonthsAgoStr + "' " +
          'and materialCode in (' + _wrapInQuotes(materialCodes) + ') ' +
          'and supplierCode in (' + _wrapInQuotes(supplierCodes) + ') ' +
          'order by avgUsagePrice asc';  // ⭐ 升序，第一条即最低

// ⚠️ 门禁：domain 用 developplatform（自建表），不凭 URI 推导
var queryResult = ObjectStore.queryByYonQL(sql, 'developplatform');

if (!queryResult || !Array.isArray(queryResult) || queryResult.length === 0) {
    return { success: true, data: {} };
}

/**
 * 工具方法：补零（2 位数字）
 */
function _pad(num) {
    return num < 10 ? '0' + num : '' + num;
}
```

### 关键点

| # | 关键点 | 原因 |
|---|--------|------|
| 1 | **时间窗口用 _pad 补零** | 月日必须 2 位 |
| 2 | **ORDER BY avgUsagePrice ASC** | 后续取第一条即最低 |
| 3 | **domain 用 developplatform** | 自建表的 domain |
| 4 | **空结果返回空对象不报错** | 业务上正常 |
| 5 | **IN 列表用 _wrapInQuotes** | 见模式 3 |

---

## 模式 5：按 rowIndex 回写结果

### 业务场景

前端传的每行都有 `rowIndex`，后端查询后需要按 rowIndex 拼装结果 `{ "0": 8.5, "2": 12.3 }`，前端按 rowIndex 精确回写。

### 完整模板

```javascript
// ========== Step 4: 按 (物料code + 供应商code) 取最低均价 ==========
// YonQL 已 ORDER BY avgUsagePrice ASC，取每个组合的第一条
var seenCombo = {};
var comboToAvgPrice = {};
for (var i = 0; i < queryResult.length; i++) {
    var row = queryResult[i] || {};
    var mCode = row.materialCode;
    var sName = row.supplierCode;
    if (!mCode || !sName) continue;

    var comboKey = mCode + '||' + sName;  // ⭐ 组合 key
    if (seenCombo[comboKey]) continue;
    seenCombo[comboKey] = true;

    var avgPriceRaw = Number(row.avgUsagePrice || 0);
    if (isNaN(avgPriceRaw)) avgPriceRaw = 0;
    comboToAvgPrice[comboKey] = Math.round(avgPriceRaw * 100) / 100;  // ⭐ 保留 2 位小数
}

// ========== Step 5: 按 rowIndex 组装返回结果 ==========
var resultMap = {};
for (var i = 0; i < queryList.length; i++) {
    var item = queryList[i] || {};
    var rowIdx = item.rowIndex;
    var mCode = item.materialCode;
    var sId = item.supplierId;

    if (rowIdx == null || !mCode || !sId) continue;

    var sCode = idToCodeMap[sId];
    if (!sCode) continue;

    var comboKey = mCode + '||' + sCode;
    var avgPrice = comboToAvgPrice[comboKey];
    if (avgPrice == null) continue;

    // ⭐ key = rowIndex 字符串，value = 均价数字
    resultMap[rowIdx + ''] = avgPrice;
}

return {
    success: true,
    data: resultMap,
};
```

### 关键点

| # | 关键点 | 原因 |
|---|--------|------|
| 1 | **组合 key 用 `||` 分隔** | 避免单字段组合冲突 |
| 2 | **Math.round(price*100)/100** | 保留 2 位小数（金额规范） |
| 3 | **resultMap key 用 rowIdx 字符串** | JS 对象 key 必须是字符串 |
| 4 | **缺数据 continue 不报错** | 业务上可能该组合无近 3 个月数据 |
| 5 | **结果数字不要包对象** | 前端 setCellValues 直接用数字 |

---

## 完整组合示例

```javascript
var AbstractAPIHandler = require('AbstractAPIHandler');
class MyAPIHandler extends AbstractAPIHandler {
    execute(request) {
        // 【规范2】日志格式
        console.error('【MyAPIHandler】入参行数=' + (request && request.queryList ? request.queryList.length : 0));

        try {
            // ========== 模式 1: 入参校验 ==========
            var queryList = (request && request.queryList) || [];
            if (!Array.isArray(queryList) || queryList.length === 0) {
                return { success: true, data: {} };
            }

            // ========== 模式 2: id → code 转换 ==========
            var materialCodes = [];
            var supplierIds = [];
            var materialSet = {};
            var supplierSet = {};
            for (var i = 0; i < queryList.length; i++) {
                var item = queryList[i] || {};
                var mCode = item.materialCode;
                var sId = item.supplierId;
                if (mCode && !materialSet[mCode]) {
                    materialCodes.push(mCode);
                    materialSet[mCode] = true;
                }
                if (sId && !supplierSet[sId]) {
                    supplierIds.push(sId);
                    supplierSet[sId] = true;
                }
            }
            if (materialCodes.length === 0 || supplierIds.length === 0) {
                return { success: true, data: {} };
            }

            // ⚠️ 门禁：domain 用元数据真实值 yssupplier
            var vendorSql = 'select id, code from aa.vendor.Vendor where id in (' + _wrapInQuotes(supplierIds) + ')';
            var vendorResult = ObjectStore.queryByYonQL(vendorSql, 'yssupplier');
            var idToCodeMap = {};
            if (vendorResult && Array.isArray(vendorResult)) {
                for (var i = 0; i < vendorResult.length; i++) {
                    var row = vendorResult[i] || {};
                    if (row.id && row.code) {
                        idToCodeMap[row.id] = row.code;
                    }
                }
            }
            if (Object.keys(idToCodeMap).length === 0) {
                return { success: true, data: {} };
            }

            // 收集 supplier codes
            var supplierCodes = [];
            var codeSet = {};
            for (var idKey in idToCodeMap) {
                var c = idToCodeMap[idKey];
                if (!codeSet[c]) {
                    supplierCodes.push(c);
                    codeSet[c] = true;
                }
            }

            // ========== 模式 3 + 4: SQL 注入防护 + 批量 YonQL ==========
            var now = new Date();
            var threeMonthsAgo = new Date(now.getTime() - 90 * 24 * 60 * 60 * 1000);
            var threeMonthsAgoStr = threeMonthsAgo.getFullYear() + '-' + _pad(threeMonthsAgo.getMonth() + 1) + '-' + _pad(threeMonthsAgo.getDate()) + ' 00:00:00';

            // ⚠️ 门禁：domain 用 developplatform
            var sql = 'select materialCode, supplierCode, avgUsagePrice ' +
                      'from xxx.yyy.SupplierMaterialInfo ' +
                      'where businessDate >= ' + "'" + threeMonthsAgoStr + "' " +
                      'and materialCode in (' + _wrapInQuotes(materialCodes) + ') ' +
                      'and supplierCode in (' + _wrapInQuotes(supplierCodes) + ') ' +
                      'order by avgUsagePrice asc';
            var queryResult = ObjectStore.queryByYonQL(sql, 'developplatform');
            if (!queryResult || !Array.isArray(queryResult) || queryResult.length === 0) {
                return { success: true, data: {} };
            }

            // ========== 模式 5: 按 rowIndex 回写结果 ==========
            var seenCombo = {};
            var comboToAvgPrice = {};
            for (var i = 0; i < queryResult.length; i++) {
                var row = queryResult[i] || {};
                var comboKey = row.materialCode + '||' + row.supplierCode;
                if (seenCombo[comboKey]) continue;
                seenCombo[comboKey] = true;
                var avgPriceRaw = Number(row.avgUsagePrice || 0);
                if (isNaN(avgPriceRaw)) avgPriceRaw = 0;
                comboToAvgPrice[comboKey] = Math.round(avgPriceRaw * 100) / 100;
            }

            var resultMap = {};
            for (var i = 0; i < queryList.length; i++) {
                var item = queryList[i] || {};
                var rowIdx = item.rowIndex;
                if (rowIdx == null) continue;
                var sCode = idToCodeMap[item.supplierId];
                if (!sCode) continue;
                var comboKey = item.materialCode + '||' + sCode;
                var avgPrice = comboToAvgPrice[comboKey];
                if (avgPrice == null) continue;
                resultMap[rowIdx + ''] = avgPrice;
            }

            return { success: true, data: resultMap };

        } catch (e) {
            console.error('【MyAPIHandler】异常：' + (e.message || JSON.stringify(e)));
            console.error('【MyAPIHandler】堆栈：' + (e.stack || ''));
            return {
                success: false,
                errorMessage: '查询失败：' + (e.message || '未知错误'),
                data: {},
            };
        }

        function _pad(num) { return num < 10 ? '0' + num : '' + num; }
        function _wrapInQuotes(arr) {
            if (!arr || arr.length === 0) return "''";
            var result = [];
            for (var i = 0; i < arr.length; i++) {
                var v = String(arr[i] || '').replace(/'/g, "''");
                result.push("'" + v + "'");
            }
            return result.join(',');
        }
    }
}
exports({ entryPoint: MyAPIHandler });
```

---

## 禁止事项

| 禁止 | 原因 | 正确 |
|------|------|------|
| 字符串 IN 列表直接拼接 | SQL 注入风险 | 用 `_wrapInQuotes` 包裹 |
| resultMap key 用数字 | JS 对象 key 会自动转字符串 | 用 `rowIdx + ''` 显式转 |
| 单引号不转义 | 包含 `'` 的字符串会破坏 SQL | `replace(/'/g, "''")` |
| 数字精度不做处理 | 可能产生 8.499999999 之类小数 | `Math.round(price*100)/100` |
| 入参不校验 | 异常可能出现在深层 | Step 1 必须校验 |
| 出错时 throw 阻断前端 | 前端整页崩溃 | return `{success: false, errorMessage}` |
| 凭 URI 推 domain | 平台 Service not found | 调 iuap-c-metadata-info 查 |

---

## AI 使用建议

- 看到"前端 invokeFunction"+"批量查询"+"按行回写" → 立即联想 BATCH_ASYNC_QUERY 5 模式
- **5 模式缺一不可**（校验 → id转code → 注入防护 → 批量YonQL → rowIndex拼装）
- **String 字段必须用 `_wrapInQuotes`**
- **结果 key 必须是 rowIndex 字符串**
- **异常必须 try-catch + return errorMessage**

---

## 相关文档

- [iuap-c-mdf-codegen/references/patterns/list-data-enrich.md](../../iuap-c-mdf-codegen/references/patterns/list-data-enrich.md) — 前端列表数据补全（配套）
- [../../00-governance/01_技术规范.md](../../00-governance/01_技术规范.md) — 技术规范
- [../../00-governance/03_元数据门禁.md](../../00-governance/03_元数据门禁.md) — 元数据门禁
- [../02_YonQL语句.md](../02_YonQL语句.md) — YonQL 语法
- [../03_字段命名契约.md](../03_字段命名契约.md) — 字段命名
- [../01_代码框架.md](../01_代码框架.md) — 代码框架
- [../../40-guides/02_后端脚本绑定指南.md](../../40-guides/02_后端脚本绑定指南.md) — 脚本编码命名
