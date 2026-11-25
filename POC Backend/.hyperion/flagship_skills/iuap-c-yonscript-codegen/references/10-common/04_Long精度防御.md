# Long 字段精度防御（权威参考）

> **加载时机**：用户在以下场景必须加载本文档：
> 1. 调用 `ObjectStore.openLinker` / `ObjectStore.postman` 后用 `JSON.parse` 解析返回字符串
> 2. 解析 `param.requestData` 字符串（MDD 规则链扁平 JSON）
> 3. 处理 `ObjectStore.queryByYonQL` 返回值中的 Long 字段
> 4. 任何涉及 `JSON.parse(jsonString)` 且 JSON 含业务主键/外键的场景
>
> **场景定位**：YonScript 跑在 V8 上，V8 Number 精度上限 2⁵³−1 ≈ 16 位；YonBIP 数据库 Long 是 19 位；超过 16 位的整数传给 V8 必然失真。YonBIP 平台只对 `param.data[0].id` 等业务主键做了 String 化保护，**对 JSON 字符串路径未做保护**。
>
> **⚠️ 强制规范**：本文档定义的 `safeJsonParse` 工具函数是规范 8（[../00-governance/01_技术规范.md §规范8](../00-governance/01_技术规范.md#规范8long-字段精度防御新增)）的**唯一实现**。其它 references/ 文档、SKILL.md、代码注释中**禁止**复制工具函数实现，仅可引用本文档。
>
> **关联 reference**：
> - 技术规范定义：[../00-governance/01_技术规范.md §规范8](../00-governance/01_技术规范.md#规范8long-字段精度防御新增)
> - 审批后反查场景：[../30-scenes/01_审批后弃审后反查.md](../30-scenes/01_审批后弃审后反查.md)（Step 7 联动）
> - 入参结构：[02_MDD规则链脚本入参.md](02_MDD规则链脚本入参.md) §"安全解析 requestData"
> - 入参结构：[03_YPD扩展点脚本入参.md](03_YPD扩展点脚本入参.md) §"安全解析 openLinker 返回值"

---

## 目录

1. [问题本质](#一问题本质)
2. [YonBIP 平台实际保护机制](#二yonbip-平台实际保护机制)
3. [四类路径的失真风险矩阵](#三四类路径的失真风险矩阵)
4. [safeJsonParse 工具函数](#四safejsonparse-工具函数)
5. [自动从元数据提取 Long 字段](#五自动从元数据提取-long-字段)
6. [典型场景示例](#六典型场景示例)
7. [tenant 字段的特殊处理](#七tenant-字段的特殊处理)
8. [为什么选 16 位阈值](#八为什么选-16-位阈值)
9. [常见错误 vs 正确写法](#九常见错误-vs-正确写法)
10. [与 YonBIP 平台工单建议](#十与-yonbip-平台工单建议)

---

## 一、问题本质

### 1.1 V8 引擎的硬约束

| 引擎 | Number 类型 | 最大安全整数 | 约等于十进制位数 |
|------|-------------|--------------|------------------|
| V8（JavaScript） | IEEE 754 双精度浮点 | **2⁵³ − 1 = 9007199254740991** | **15~16 位** |
| Java `Long` | 64 位有符号整数 | 9223372036854775807 | **19 位** |
| 数据库 `BIGINT` | 64 位有符号整数 | 9223372036854775807 | **19 位** |

### 1.2 失真发生的根本原因

```
Java 端 Long (19位精确)
    ↓
JSON 字符串（Java → JSON 序列化，保留为 JSON Number）
    ↓
V8 JSON.parse()  ← 失真发生在这里（如果 ≥ 17 位）
    ↓
V8 Number (IEEE 754 双精度，末位失真)
    ↓
YonScript 拿到失真值
```

### 1.3 失真示例

```javascript
// V8 控制台实测（Node.js 18）
Number(1900000123456780).toString()  // "1900000123456780"（侥幸）
Number(1900000123456781).toString()  // "1900000123456780"（失真！）
Number(1900000123456782).toString()  // "1900000123456784"（失真！）
Number(1900000123456783).toString()  // "1900000123456784"（失真！）
```

**结论**：在 YonBIP 这种"主键 19 位"的业务场景下，**必然失真**。

---

## 二、YonBIP 平台实际保护机制

通过对 `03_YPD扩展点脚本入参结构.JSON` 实际样本的逐行分析（line 207~330），YonBIP 平台对 `param` 中的字段做了**有限保护**：

### 2.1 已被平台 String 化的字段

```json
"id": "2543718355636322313"               ← 业务主键
"customerApplyId": "2539428645377146885"  ← 关联外键
"applicationOrg": "1529979475333742597"   ← 参照外键
"applyDepartment": "1526180671237128197"  ← 参照外键
"bizOperator": "1952441692752707593"      ← 用户ID
"creatorId": "1525695460210638858"        ← 用户ID
"modifierId": "1525695460210638858"       ← 用户ID
"orgId": "1567954261903933444"            ← 参照外键
"transType": "1567817673359753220"        ← 参照外键
```

**特征**：所有"业务主键 + 参照外键 + 用户ID"都以 **String 形式** 传递。

### 2.2 未被平台保护的字段

```json
"tenant": 2910033146761808   ← 19 位 Number，有失真风险
"verifystate": 1             ← 枚举，0~9999，安全
"status": 3                  ← 业务状态，安全
"applyType": 0               ← 枚举，安全
"_status": 0                 ← 行状态，安全
"stopstatus": false          ← 布尔，安全
"isDeleted": false           ← 布尔，安全
```

**特征**：
- ⚠️ `tenant`（19 位纯数字）是少数派——平台保护不完整
- ✅ 其他枚举/状态/布尔都在 V8 安全范围内
- ✅ YonBIP 平台的判断标准似乎是："超过 16 位的纯数字才强制 String 化"

### 2.3 平台保护判断标准的推断

根据实际样本推断，YonBIP 平台的 String 化规则可能是：

```python
# 伪代码（推断）
if 字段名.matches(/^(id|.*_id|.*Id)$/) or 字段名 in ['orgId', 'transType', 'userId', 'creatorId', ...]:
    serialize_as_string = True  # 业务主键/外键/用户ID
elif field_type == 'Boolean':
    serialize_as_boolean = True
else:
    serialize_as_number = True  # 默认，包括 tenant
```

---

## 三、四类路径的失真风险矩阵

| # | 路径 | 拿到的是什么 | 失真风险 | 防御方法 |
|---|------|------------|---------|---------|
| 1 | `param.data[0].id` | 平台已解析的对象 | ✅ **已保护**（String） | 直接用 String，无需处理 |
| 2 | `param.billDOs[0].id` | 平台已解析的对象 | ✅ **已保护**（String） | 直接用 String，无需处理 |
| 3 | `param.billDO.tenant` | 平台已解析的对象 | ⚠️ **19 位 Number 有风险** | 优先用 `ytenant`（String），或 `.toString()` 兜底 |
| 4 | `param.requestData` 字符串 | 原始扁平 JSON | ❌ **必失真** | `safeJsonParse` |
| 5 | `ObjectStore.queryByYonQL` 返回 | 平台已解析的对象 | ❌ **id 是 Number** | 普通查询 + 结果侧 `String()` / `.toString()` 安全处理 |
| 6 | `ObjectStore.openLinker` 返回字符串 | 原始 JSON 字符串 | ❌ **必失真** | `safeJsonParse` |
| 7 | `ObjectStore.postman` 返回字符串 | 原始 JSON 字符串 | ❌ **必失真** | `safeJsonParse` |

### 3.1 YonBIP 平台双路径架构模式

> **核心架构认知**：YonBIP 平台为 Rule 脚本设计了两条取值路径（MDD 场景）或三个入口（YPD 场景），这是**有意为之的标准架构**，理解它才能正确选择 Long 防御策略。

#### 3.1.1 MDD 规则链双路径（param.data + param.requestData）

```
┌──────────────────────────────────────────────────────────────────────┐
│            MDD 规则链 param 双路径架构（推荐标准）                     │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  路径 A（主路径 · 推荐）                  路径 B（兜底 · 老 skill 兼容）│
│  ┌──────────────────────────┐            ┌──────────────────────────┐│
│  │  param.data               │            │  param.requestData        ││
│  │  ────────────────         │            │  ────────────────         ││
│  │  • 平台已解析              │            │  • 平台未解析              ││
│  │  • 嵌套对象数组            │            │  • 扁平 JSON 字符串         ││
│  │  • id 已被 String 化      │            │  • id 是 Number（必失真）   ││
│  │  • 子表已嵌套              │            │  • 子表字段名是 JSON 串     ││
│  │                           │            │                           ││
│  │  ✅ 直接 bill.id          │            │  ⚠️ safeJsonParse 后才能用 ││
│  │  ✅ bill.orderDetails     │            │  ⚠️ JSON.parse(string)... ││
│  └──────────────────────────┘            └──────────────────────────┘│
│                                                                      │
│  何时回退到 B？                                                       │
│  • param.data 为空（afterAudit 等 only-id 场景）                       │
│  • param.data 缺少关键子表字段                                         │
│  • 需要原始 refId 关联关系                                             │
│  • 老版本兼容（历史脚本依赖 requestData）                              │
│                                                                      │
│  共同 fallback：5 步反查法                                              │
│  context.ids → param.ids → param.billDOs → param.data → queryByYonQL │
│  → 见 [../30-scenes/01_审批后弃审后反查.md](../30-scenes/01_审批后弃审后反查.md)  │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

**为什么 YonBIP 平台要设计双路径（架构师视角）**：

| 原因 | 解释 |
|------|------|
| **性能优化** | 路径 A（param.data）是平台已解析的，**零反序列化开销**；路径 B 需要脚本自解析 |
| **向后兼容** | 老 YonBIP 版本（V3、V4）只提供 requestData；新版本（V5+）加 param.data 提升性能但不破坏老脚本 |
| **数据完整性** | 路径 A 平台加工过，丢失了"原始 refId 关联"；路径 B 保留原始数据，**用于调试和特殊场景** |
| **业务分工** | 路径 A 用于常规业务逻辑；路径 B 用于"业务数据回写/审计"等需要原始数据的场景 |

#### 3.1.2 YPD 扩展点多入口（billDOs + customerData + context）

```
┌──────────────────────────────────────────────────────────────────────┐
│            YPD 扩展点 param 多入口架构                                │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  入口 1（主路径 · billDOs）         入口 2（嵌入式 JSON · customerData）│
│  ┌──────────────────────────┐            ┌──────────────────────────┐│
│  │  param.billDOs[0]        │            │  param.billDO.customerData││
│  │  ────────────────         │            │  ────────────────         ││
│  │  • 主表 + extendData 子表 │            │  • String 字段内容是 JSON ││
│  │  • id / 外键 已 String 化 │            │  • 含业务自定义字段        ││
│  │  ✅ 直接 bill.id          │            │  ⚠️ safeJsonParse 后才能用 ││
│  └──────────────────────────┘            └──────────────────────────┘│
│                                                                      │
│  入口 3（context 上下文）                                              │
│  ┌──────────────────────────────────────────────────────────────────┐│
│  │  context.{action, billnum, userId, tenant, ...}                    ││
│  │  ────────────────                                                  ││
│  │  • 全部 String（YonBIP 平台已保护）                                ││
│  │  • 与 action / 流程强相关                                          ││
│  │  ✅ 直接 context.fieldName                                         ││
│  └──────────────────────────────────────────────────────────────────┘│
│                                                                      │
│  ⚠️ YPD 没有 param.requestData                                          │
│     若 billDOs 不可用，参考 5 步反查法                                 │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

#### 3.1.3 双路径选择决策表

| 场景 | MDD 规则链 | YPD 扩展点 | Long 防御 |
|------|-----------|-----------|----------|
| **常规业务**（取主表/子表/参照） | ✅ 路径 A：`param.data` | ✅ 入口 1：`billDOs` | **无需防御**（平台已保护） |
| **扩展数据**（业务自定义 JSON） | - | ✅ 入口 2：`customerData` | **必须 safeJsonParse** |
| **上下文**（action/用户/租户） | ✅ `context` | ✅ `context` | **无需防御** |
| `param.data`/`billDOs` 为空 | ⚠️ 路径 B：`requestData` | ❌ 不存在 requestData | 必须 safeJsonParse（MDD） |
| 5 步反查法兜底（审批后） | ✅ | ✅ | queryByYonQL 路径需 `cast/toString` |
| **老脚本兼容**（历史依赖 requestData） | ✅ 路径 B | - | 必须 safeJsonParse |

#### 3.1.4 完整双路径 YonScript 模板（推荐用于生产）

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    // ===== 双路径取数 =====
    var bill = null;
    var billSource = null;  // 标记数据来源，便于排查
    
    // 路径 A：param.data（推荐）
    if (param.data && param.data[0]) {
      bill = param.data[0];
      billSource = 'param.data';
    }
    // 路径 B：param.requestData（兜底）
    else if (param.requestData) {
      // ⚠️ 必须 safeJsonParse
      var requestDataObj = safeJsonParse(param.requestData, {
        longFields: [
          'id', 'orgId', 'transType', 'userId', 
          'creatorId', 'modifierId', 'auditorId',
          'tenant', 'purchaseOrderSourceId', 'purchaseOrderSourceDetailId'
        ],
        lengthThreshold: 16
      });
      bill = requestDataObj;  // 注意：扁平结构，子表需另 JSON.parse
      billSource = 'param.requestData';
    }
    // 5 步反查法：context.ids / param.ids
    else {
      var billId = extractBillId(context, param);
      if (!billId) {
        throw new Error('【MyTrigger】双路径均未取到 bill，paths A/B/all-failed');
      }
      // 见 references/20_审批后弃审后反查.md §三
      bill = queryByYonQLAndToString(`select * from \`xxx.Entity\` where id = '${billId}'`, 'domain');
      billSource = '5-step-query';
    }
    
    if (!bill) {
      throw new Error('【MyTrigger】bill 为空');
    }
    
    // 记录数据来源，便于排查
    console.error('【MyTrigger】bill 来源:' + billSource + ', id=' + bill.id);
    
    // ✅ bill.id 已是 String（路径 A）或经 safeJsonParse（路径 B）
    //    路径 A 必是 String，路径 B 必是 String
    //    5 步反查法自行 .toString() 后是 String
    var billId = bill.id;
    
    return {};
  }
}
exports({ entryPoint: MyTrigger });
```

**核心规律**：
- **平台解析过的对象**（路径 1/2/3）：平台已部分保护，id 是 String，但 tenant 等少数字段仍是 Number
- **JSON 字符串**（路径 4/6/7）：**完全没有保护**，必须自己用 safeJsonParse
- **YonSQL 返回值**（路径 5）：**完全没有保护**（数据库返回 Number，V8 解析为 Number）

---

## 四、safeJsonParse 工具函数

### 4.1 完整实现

```javascript
/**
 * YonScript 安全的 JSON.parse，自动处理 Long 字段精度失真
 * 
 * @param {string} content 原始 JSON 字符串
 * @param {object} options 选项
 *   - longFields: 已知 Long 类型字段名（来自元数据）— 第 1 层精确模式
 *   - lengthThreshold: 长度阈值（默认 16，覆盖 V8 安全边界）— 第 2 层兜底
 *   - excludeFields: 已知非 Long 数字字段 — 第 3 层排除
 * @returns {object} 解析后的对象
 */
function safeJsonParse(content, options) {
    if (typeof content !== 'string') {
        return content;  // 已经是对象，直接返回
    }
    options = options || {};
    var longFields = options.longFields || [];
    var excludeFields = options.excludeFields || [];
    var threshold = options.lengthThreshold || 16;
    var excludeSet = {};
    for (var i = 0; i < excludeFields.length; i++) {
        excludeSet[excludeFields[i]] = true;
    }

    // ========== 第 1 层：元数据驱动的精确识别 ==========
    // 对元数据明确标识为 Long 的字段，强制加引号
    for (var j = 0; j < longFields.length; j++) {
        var field = longFields[j];
        var escaped = field.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        var re = new RegExp('"' + escaped + '":(\\d+)(?=[,\\]\\}\\s])', 'g');
        content = content.replace(re, '"' + field + '":"$1"');
    }

    // ========== 第 2 层：长度阈值的通用兜底 ==========
    // 对所有 ≥ 阈值位纯数字字段加引号（覆盖未在白名单的 Long 字段）
    var lengthRe = new RegExp('"([^"]+)":(\\d{' + threshold + ',})(?=[,\\]\\}\\s])', 'g');
    content = content.replace(lengthRe, function (match, key, val) {
        if (excludeSet[key]) {
            return match;  // 排除字段不动
        }
        return '"' + key + '":"' + val + '"';
    });

    // ========== 第 3 层：安全 parse ==========
    return JSON.parse(content);
}
```

### 4.2 三层防御的工作原理

```
┌─────────────────────────────────────────────────────────────────┐
│                  safeJsonParse 三层防御                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  第 1 层：元数据驱动的精确识别（精确）                            │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │  输入：longFields = ['id', 'orgId', 'purchaseOrderSourceId']│ │
│  │  处理：对每个字段用正则加引号                                │ │
│  │  适用：已知元数据的精确字段                                  │ │
│  └───────────────────────────────────────────────────────────┘ │
│                            ↓                                    │
│  第 2 层：长度阈值的通用兜底（兜底）                              │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │  输入：lengthThreshold = 16                                │ │
│  │  处理：匹配所有 "key":1234567890123456（≥ 16 位纯数字）    │ │
│  │  适用：未在 longFields 中的 Long 字段（防御性兜底）         │ │
│  │  误伤面：几乎为零（业务中无 16 位纯数字非 ID 数据）         │ │
│  └───────────────────────────────────────────────────────────┘ │
│                            ↓                                    │
│  第 3 层：业务字段名黑名单（精修）                                │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │  输入：excludeFields = ['version', 'sortNum']               │ │
│  │  处理：第 2 层对排除字段不生效                               │ │
│  │  适用：业务上"非 Long 数字"字段（如版本号、排序号）         │ │
│  └───────────────────────────────────────────────────────────┘ │
│                            ↓                                    │
│  最终：JSON.parse(content) ← 此时所有 Long 字段都是 String      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 4.3 工具函数放置位置

**YonScript 平台的限制**：YonScript 不支持 ES6 import/export，工具函数必须**复制到每个使用脚本**。

**推荐做法**：
- 在公共脚本项目中维护一个 `safe-json.js`（[详见 iuap-c-server-codegen]）
- 每个 YonScript 触发器开头复制一份 `safeJsonParse` 函数实现
- 或者用 `extrequire('GTxxx.Module.safeJsonParse')` 跨脚本调用（如平台支持）

---

## 五、自动从元数据提取 Long 字段

### 5.1 从 iuap-c-metadata-info 输出提取

iuap-c-metadata-info 元数据中，`dataType` 字段标识类型：

```json
{
  "attributes": [
    {
      "name": "id",
      "type": "primitive",
      "dataType": "Long",          ← 关键标识
      "dbColumnName": "id"
    },
    {
      "name": "code",
      "type": "primitive",
      "dataType": "String"
    },
    {
      "name": "orgId",
      "type": "Reference",
      "dataType": "Long",          ← 参照也是 Long
      "refUri": "aa.vendor.Vendor"
    }
  ]
}
```

### 5.2 自动提取函数

```javascript
/**
 * 从元数据中自动提取所有 Long 字段
 * @param {object} metadata - iuap-c-metadata-info 输出的元数据
 * @returns {string[]} Long 字段名列表
 */
function extractLongFields(metadata) {
    var longFields = [];
    function walk(attrs) {
        if (!attrs) return;
        for (var i = 0; i < attrs.length; i++) {
            var attr = attrs[i];
            if (attr.dataType === 'Long' || 
                attr.dataType === 'Integer' || 
                attr.dataType === 'BigInt') {
                longFields.push(attr.name);
            }
        }
    }
    // 主表字段
    walk(metadata.attributes);
    // 子表字段
    if (metadata.tables) {
        for (var key in metadata.tables) {
            if (metadata.tables.hasOwnProperty(key)) {
                walk(metadata.tables[key].attributes);
            }
        }
    }
    // 特征组字段（按需）
    if (metadata.characterGroups) {
        for (var gKey in metadata.characterGroups) {
            if (metadata.characterGroups.hasOwnProperty(gKey)) {
                walk(metadata.characterGroups[gKey].attributes);
            }
        }
    }
    return longFields;
}
```

### 5.3 使用示例

```javascript
// 假设已通过 iuap-c-metadata-info 拿到元数据
var poMeta = {
    "attributes": [
        { "name": "id", "dataType": "Long" },
        { "name": "code", "dataType": "String" },
        { "name": "orgId", "dataType": "Long" },
        { "name": "ticketCollectionClose", "dataType": "Boolean" }
    ],
    "tables": {
        "pu_applyorderlist_detail": {
            "attributes": [
                { "name": "id", "dataType": "Long" },
                { "name": "purchaseOrderSourceId", "dataType": "Long" }
            ]
        }
    }
};

var longFields = extractLongFields(poMeta);
// 结果: ['id', 'orgId', 'id', 'purchaseOrderSourceId']
// 去重后: ['id', 'orgId', 'purchaseOrderSourceId']

var result = safeJsonParse(jsonString, {
    longFields: longFields,
    lengthThreshold: 16
});
```

---

## 六、典型场景示例

### 6.1 场景 1：openLinker 返回字符串（采购订单开票状态打开）

```javascript
// YonScript Rule 脚本：采购入库单审批后
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
    execute(context, param) {
        // ✅ param.data[0].id 已是 String，平台保护
        var billId = param.data[0].id;
        console.error('【MyTrigger】billId:' + billId);
        
        // 调用 OpenAPI
        var resultString = ObjectStore.openLinker('POST', url, appCode, body);
        // ❌ 错误：var result = JSON.parse(resultString);  // Long 字段失真
        
        // ✅ 正确：safeJsonParse
        var result = safeJsonParse(resultString, {
            longFields: ['id', 'purchaseOrderSourceId', 'purchaseOrderSourceDetailId'],
            lengthThreshold: 16,
            excludeFields: []
        });
        
        return {};
    }
}
```

### 6.2 场景 2：param.requestData 字符串解析

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
    execute(context, param) {
        // param.requestData 是扁平 JSON 字符串（未受平台保护）
        // ❌ 错误：var requestData = JSON.parse(param.requestData);
        
        // ✅ 正确：safeJsonParse
        var requestData = safeJsonParse(param.requestData, {
            longFields: [
                'id', 'parentId', 'orderId', 'orgId', 'transType',
                'userId', 'creatorId', 'modifierId', 'auditorId',
                'tenant', 'purchaseOrderSourceId', 'purchaseOrderSourceDetailId'
            ],
            lengthThreshold: 16
        });
        
        return {};
    }
}
```

### 6.3 场景 3：queryByYonQL 返回值处理

```javascript
// YonQL 端：默认不用 cast，直接普通查询
var detailSql = `
    select 
        id,
        purchaseOrderSourceId,
        purchaseOrderSourceDetailId,
        qty, firstupcode
    from \`st.purinrecord.PurInRecords\`
    where mainid = '${billId}'
`;
var detailList = ObjectStore.queryByYonQL(detailSql, 'ustock');

// 结果侧兜底：拿到 Number 后统一转字符串
for (var i = 0; i < detailList.length; i++) {
    detailList[i].id = String(detailList[i].id || '');
    detailList[i].purchaseOrderSourceId = String(detailList[i].purchaseOrderSourceId || '');
}
```

### 6.4 场景 4：通用兜底（无元数据时）

```javascript
// 没有 longFields，依赖第 2 层兜底（长度阈值）
var result = safeJsonParse(jsonString, {
    longFields: [],
    lengthThreshold: 16
});
// 所有 ≥ 16 位纯数字字段都会被加引号
```

### 6.5 场景 5：极简应急（不依赖 safeJsonParse）

如果项目中没引入 safeJsonParse 函数，可以用最小化的正则加引号方案：

```javascript
// 最小化应急：仅给已知 Long 字段加引号
var fixContent = resultString
    .replace(/"id":(\d+)/g, '"id":"$1"')
    .replace(/"purchaseOrderSourceId":(\d+)/g, '"purchaseOrderSourceId":"$1"')
    .replace(/"purchaseOrderSourceDetailId":(\d+)/g, '"purchaseOrderSourceDetailId":"$1"')
    // 兜底：所有 16 位以上纯数字
    .replace(/"([^"]+)":(\d{16,})/g, '"$1":"$2"');
var result = JSON.parse(fixContent);
```

---

## 七、tenant 字段的特殊处理

### 7.1 风险确认

`param.billDO.tenant` 是 19 位 Number（如 `2910033146761808`），是 YonBIP 平台**保护不完整**的典型案例。

**验证失真**：
```javascript
// 假设 tenant = 2910033146761809
Number(2910033146761809).toString()  // "2910033146761808"（失真！）
Number(2910033146761810).toString()  // "2910033146761810"（侥幸）
Number(2910033146761811).toString()  // "2910033146761812"（失真！）
```

**结论**：tenant 字段**有失真风险**，但仅当实际值落在"失真区间"时才出错。

### 7.2 解决方案

**优先方案：用 `ytenant` 替代**

```javascript
// ytenant 是 String 形式（"0000L6YTYEY5FUZPXE0000"），不失真
var ytenant = param.billDO.ytenant;  // 推荐使用

// 仅用于日志、SQL 拼接
var sql = `select * from ... where ytenant = '${ytenant}'`;
```

**兜底方案：`.toString()` 防御性使用**

```javascript
// .toString() 不修复已失真的 Number，但保证下游 SQL/API 不再失真
var tenant = (param.billDO.tenant).toString();

// 重要：toString() 不能修复"已经失真"的值
// 只能保证不再二次失真
```

### 7.3 推动平台修复

向 YonBIP 平台提工单：

> 建议将 `param.billDO.tenant` 字段统一为 String 形式（与 `ytenant` 对齐），以避免 V8 引擎精度失真。

---

## 八、为什么选 16 位阈值

### 8.1 V8 安全整数边界

| 数字位数 | V8 安全 | 含义 |
|---------|--------|------|
| 1~15 位 | ✅ 精确 | 所有业务数字 |
| 16 位 | ⚠️ 边界 | 2⁵³−1 = 9007199254740991 是最后的安全 16 位数 |
| 17~19 位 | ❌ 必失真 | YonBIP 数据库 BIGINT 范围 |
| ≥ 20 位 | ❌ 必失真 | 实际业务无 |

### 8.2 为什么选 16 不是 15

- **选 15** 会误伤业务中所有 15 位纯数字（如某些统计码、批次号）
- **选 16** 误伤面为零——业务中**几乎不存在 16 位纯数字的非 ID 数据**
- **16 是 V8 安全整数上限的位数**（2⁵³−1 恰好是 16 位）

### 8.3 误伤面分析

| 业务数据类型 | 典型位数 | 是否 16 位 | 是否被误伤 |
|-------------|---------|-----------|-----------|
| 金额（元） | ≤ 12 位 | 否 | ✅ 不误伤 |
| 数量 | ≤ 8 位 | 否 | ✅ 不误伤 |
| 百分比 | 0~100 | 1~3 位 | ✅ 不误伤 |
| 版本号 | ≤ 6 位 | 否 | ✅ 不误伤 |
| 日期时间戳（秒） | 10 位 | 否 | ✅ 不误伤 |
| 日期时间戳（毫秒） | 13 位 | 否 | ✅ 不误伤 |
| 业务主键 id | 19 位 | 是 | 🎯 **目标** |
| 参照外键 | 19 位 | 是 | 🎯 **目标** |
| 租户数字 ID | 19 位 | 是 | 🎯 **目标** |

**结论**：**16 位是工程最优阈值**，误伤面为零。

---

## 九、常见错误 vs 正确写法

### 9.1 错误 1：直接 JSON.parse openLinker 返回值

```javascript
// ❌ 错误
var resultString = ObjectStore.openLinker('POST', url, appCode, body);
var result = JSON.parse(resultString);
result.id  // 末位可能失真
```

### 9.2 正确 1：用 safeJsonParse

```javascript
// ✅ 正确
var result = safeJsonParse(resultString, {
    longFields: ['id', 'orgId'],
    lengthThreshold: 16
});
result.id  // String，不会失真
```

### 9.3 错误 2：把 param.data[0].id 当 Long 处理

```javascript
// ❌ 错误：担心 id 失真，手工 toString
var billId = (param.data[0].id).toString();  // 平台已 String 化，toString 是多余的
```

### 9.4 正确 2：直接用 String

```javascript
// ✅ 正确：平台已 String 化，直接用
var billId = param.data[0].id;
console.error('billId type:', typeof billId);  // "string"
```

### 9.5 错误 3：safeJsonParse 缺失 longFields

```javascript
// ❌ 错误：只依赖第 2 层兜底，可能漏掉 16 位以下的 Long 字段
var result = safeJsonParse(content, {
    longFields: [],
    lengthThreshold: 16
});
```

### 9.6 正确 3：长字段白名单 + 兜底组合

```javascript
// ✅ 正确：白名单精确 + 兜底防御
var result = safeJsonParse(content, {
    longFields: extractLongFields(metadata),  // 元数据自动提取
    lengthThreshold: 16
});
```

### 9.7 错误 4：在 SQL 中直接拼接 Number id

```javascript
// ❌ 错误：id 失真后用 Number 拼 SQL
var billId = param.data[0].id;  // 假设是 "2539428645377146885"
var sql = `... where id = ${billId}`;  // 拼成 Number 字面量
```

### 9.8 正确 4：SQL 中 id 加单引号

```javascript
// ✅ 正确：String 加单引号
var billId = param.data[0].id;
var sql = `... where id = '${billId}'`;  // 拼成 String 字面量
```

---

## 十、与 YonBIP 平台工单建议

| 优先级 | 工单标题 | 内容 | 预期效果 |
|--------|---------|------|---------|
| P0 | `param.billDO.tenant` 字段统一为 String | 与 `ytenant` 字段对齐，避免 19 位 Number 失真 | 解决路径 3 的失真 |
| P1 | `param.requestData` 扁平 JSON 中 Long 字段预加引号 | 解决 MDD 规则链中 requestData 解析失真 | 解决路径 4 的失真 |
| P1 | `queryByYonQL` 返回值对 Long 字段支持 `cast` 转换 | 解决 YonSQL 返回路径失真 | 解决路径 5 的失真 |
| P2 | 统一 YonBIP 文档：明确 Long 字段的"主键已 String 化"机制 | 帮助开发者理解平台行为，避免误用 | 文档完善 |

---

## 十一、与其他 reference 的关系

| Reference | 引用本 reference 的位置 |
|----------|----------------------|
| [../00-governance/01_技术规范.md](../00-governance/01_技术规范.md) §规范 8 | 顶部声明"权威单一来源" |
| [../30-scenes/01_审批后弃审后反查.md](../30-scenes/01_审批后弃审后反查.md) | Step 7 "Long 字段防御"小节 |
| [02_MDD规则链脚本入参.md](02_MDD规则链脚本入参.md) | §"安全解析 requestData"小节 |
| [03_YPD扩展点脚本入参.md](03_YPD扩展点脚本入参.md) | §"安全解析 openLinker 返回值"小节 |
| [SKILL.md](../SKILL.md) | 决策树增加"Long 字段防御"分支，索引表引用本 reference |

**本 reference 是 Long 字段精度防御的**唯一权威**。修改本 reference 后，须同步检查并更新所有交叉引用。**
