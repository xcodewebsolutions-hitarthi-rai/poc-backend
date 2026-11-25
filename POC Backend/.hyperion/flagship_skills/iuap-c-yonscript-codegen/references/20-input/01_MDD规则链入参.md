# MDD 规则链脚本入参结构

> **加载时机**：MDD 规则链触发时需要获取单据数据时
> **场景定位**：从 `param.data` 数组中获取当前单据数据
>
> **⚠️ 字段命名契约**（字段类型识别、参照/特征/子表/平行表写法）已统一在 [../10-common/03_字段命名契约.md](../10-common/03_字段命名契约.md) 中维护，本文档不再重复。
> **⚠️ 审批后/弃审后 only-id 场景**（5 步反查法）见 [../30-scenes/01_审批后弃审后反查.md](../30-scenes/01_审批后弃审后反查.md)，**不在此重复**。
> **⚠️ 三场景对比表**（MDD vs YONSQL vs YPD 字段取值差异）已统一在 [../40-guides/01_metadata-yonsql-guide.md](../40-guides/01_metadata-yonsql-guide.md) 中维护，本文档不再重复。
> **⚠️ 技术规范**（URI/日志/openLinker/postman/safeJsonParse/YonSQL/字段取值）见 [../00-governance/01_技术规范.md](../00-governance/01_技术规范.md)，**不在此重复**。

---

## 一、场景定位

MDD 规则链从 `param.data` 数组获取单据数据，与 YONSQL 查询、YPD 扩展点的差异详见 [../40-guides/01_metadata-yonsql-guide.md](../40-guides/01_metadata-yonsql-guide.md) 的"三场景字段取值对比"小节。

---

## 二、字段取值规范

> **核心原则**：所有字段名必须从元数据 `attributes[].name` 获取，禁止臆想。详见 [../00-governance/01_技术规范.md 规范 7](../00-governance/01_技术规范.md#规范7字段取值规范)。

### 2.1 主表字段取值

| 字段类型 | 取值路径 | 示例 |
|---------|---------|------|
| 基础字段 | `bill.fieldName` | `bill.code`, `bill.name` |
| 参照字段 | `bill.fieldName.subField` | `bill.supplierId.code` |
| 特征组 | `bill.userDefineCharacter.特征编码` | `bill.userDefineCharacter.RZRW01` |

> 完整的原始参数数据参见 `samples/MDD规则链入参结构.json`。

---

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| `action` | string | 当前触发的动作 | `"save"`、`"submit"`、`"audit"` |
| `billnum` | string | 单据编码 | `"staffInfo10"` |
| `name` | string | 单据名称 | `"员工信息10"` |
| `fullname` | string | 元数据全路径（含租户前缀） | `"AT1D447CBE04A8000B.AT1D447CBE04A8000B.staffInfo"` |
| `subid` | string | 应用编码 | `"AT1D447CBE04A8000B"` |
| `billtype` | string | 单据类型 | `"Voucher"` |
| `entityCode` | string | 实体编码 | `"staffinfo_2378335348513046531"` |
| `mddBoId` | string | MDD业务对象ID | `"staffInfo"` |
| `tenant` | string | 租户ID | `"0000L6YQ8AVLFUZPXD0000"` |
| `tenantId` | string | 租户ID（通常为`"0"`） | `"0"` |
| `userId` | string | 用户ID（通常为null） | `null` |
| `userName` | string | 用户名（通常为null） | `null` |
| `bMain` | boolean | 是否主实体 | `true` |
| `i18ndoc` | boolean | 是否支持多语 | `true` |
| `customMap` | object | 自定义参数Map | 见下文 |
| `extProps` | object | 扩展属性 | 见下文 |
| `supportBpm` | boolean | 是否支持工作流 | `true` |
| `transaction` | boolean | 是否启用事务 | `false` |

### context 常见 action 值

| action | 说明 |
|--------|------|
| `save` | 保存 |
| `submit` | 提交 |
| `audit` | 审核 |
| `unaudit` | 反审核 |
| `delete` | 删除 |

### context.customMap

自定义参数映射，包含幂等校验等信息：

```json
{
  "_mdd-filterItemName": null,
  "_mdd-filterColumnName": null,
  "_mdd-filterItemOrder": null,
  "_IDEMPOTENT_CHECK_RELATION": {
    "staffInfo10_add_a66a2262-4f35-4a50-b870-3f8ff43d6eb2": "2543956210944573441"
  }
}
```

### context.extProps

扩展属性，包含业务域、版本等配置：

```json
{
  "ISBIZFLOW": false,
  "mobileBillNo": "staffInfo10MobileArchive",
  "printOrgAppCode": "AT1D447CBE04A8000B",
  "unionDomainKey": "union",
  "bizDomainCode": "PF",
  "ISSOCIALREVERSE": false,
  "version": "5",
  "uiObject": "viewApplication"
}
```

---

## param 结构

param 包含完整的单据数据和操作上下文。

### 一级字段概览

| 字段 | 类型 | 说明 |
|------|------|------|
| `requestData` | string | **原始请求数据**（JSON字符串，扁平结构） |
| `data` | array | **结构化单据数据数组**（优先使用，含嵌套对象） |
| `action` | string | 当前动作 | `"save"` |
| `billnum` | string | 单据编码 | `"voucher_orderchange"` |
| `parameters` | object | 操作参数（含服务编码、命令名等） |
| `bPromotion` | boolean | 是否促销相关 | `true` |
| `skipErrorMessage` | array | 跳过的错误消息 | `[]` |
| `item` | null | 行项目 |
| `refCode` | null | 参考编码 |
| `ids` | null | ID列表 |
| `fullname` | null | 元数据全名 |
| `tplid` | null | 模板ID |
| `condition` | null | 查询条件 |
| `isIncludeMeta` | boolean | 是否包含元数据 | `false` |
| `externalData` | object | 外部数据 | `{}` |
| `partitionable` | boolean | 是否可分区 | `true` |

---

### 获取单据数据的推荐方式

优先使用 `param.data`（结构化数组，含嵌套子表对象）：

```javascript
// 推荐的取值方式
var bills = param.data || [];
```

> `param.requestData` 是原始的扁平JSON字符串，必须用 `safeJsonParse(param.requestData)` 解析，但不如 `param.data` 好用。`param.data` 已将子表（如 `orderDetails`）解析为嵌套对象数组。

---

### data 数组元素结构

`param.data` 中每个元素是结构化的单据记录。**关键认知**：`param.data` 是 YonBIP 平台**已解析的嵌套对象**，**业务主键/外键已被平台 String 化保护**（详见 [../10-common/04_Long精度防御.md §二](../10-common/04_Long精度防御.md#二yonbip-平台实际保护机制)），可直接使用。

| 字段 | JavaScript 实际类型 | 平台保护 | 失真风险 | 说明 | 示例值 |
|------|------------------|---------|---------|------|--------|
| `_realtype` | string | - | - | 实体类型标记 | `"true"` |
| `_entityName` | string | - | - | 实体名 | `"voucher.orderchange.OrderChange"` |
| `_keyName` | string | - | - | 主键字段名 | `"id"` |
| `_status` | string | - | - | 行状态 | `"Update"`、`"Insert"`、`"Unchanged"`、`"Delete"` |
| `id` | **string** | ✅ 已保护 | ✅ 安全 | **主键**（19 位 Long，平台 String 化） | `"2539428645377146885"` |
| `code` | string | - | - | 单据编码 | `"V20260521001"` |
| `status` | number | - | ✅ 安全 | 业务状态（0~9999） | `2` |
| `verifystate` | number | - | ✅ 安全 | 审核状态 | `1` |
| `isWfControlled` | boolean | - | ✅ 安全 | 是否受工作流控制 | `true` |
| `iDeleted` | number | - | ✅ 安全 | 是否已删除（0/1） | `0` |
| `version` | number | - | ✅ 安全 | 版本号 | `1` |
| `creator` | string | - | - | 创建人 | `"测试管理员"` |
| `createTime` | string | - | - | 创建时间 | `"2026-05-21 09:00:00"` |
| `modifier` | string | - | - | 修改人 | `"测试管理员"` |
| `modifyTime` | string | - | - | 修改时间 | `"2026-05-21 09:30:00"` |
| `pubts` | string | - | - | 时间戳 | `"2026-05-21 09:30:00"` |
| `tenant` | number | ⚠️ **未保护** | ⚠️ **19 位有失真风险** | **租户数字 ID**（YonBIP 平台保护不完整） | `2910033146761808` |
| `ytenant` | string | - | ✅ 安全 | 租户编码（推荐替代 tenant） | `"0000L6YTYEY5FUZPXE0000"` |
| **业务字段** | | | | *因单据类型不同而不同* | |
| `orgId`（参照外键） | **string** | ✅ 已保护 | ✅ 安全 | 业务外键（19 位 Long，平台 String 化） | `"1529979475333742597"` |
| `transType`（参照外键） | **string** | ✅ 已保护 | ✅ 安全 | 单据类型（19 位 Long，平台 String 化） | `"1567817673359753220"` |
| `userId`（参照外键） | **string** | ✅ 已保护 | ✅ 安全 | 用户ID | `"99ea7655-00a2-4bda-b23c-19ade37ea574"` |
| `creatorId`（参照外键） | **string** | ✅ 已保护 | ✅ 安全 | 创建人ID | `"1525695460210638858"` |
| `qty`（数字业务字段） | number | - | ✅ 安全 | 数量（≤ 8 位） | `100` |
| `verifymark`（业务状态） | number | - | ✅ 安全 | 审核标记（0/1） | `0` |

**关键说明**：

- **`id`、参照外键（`orgId`、`transType`、`userId` 等）** —— 19 位 Long 在 `param.data` 中**已被平台 String 化**为字符串，**直接用 String**，无需 toString() 防御
- **`tenant`（数字租户ID）** —— 19 位 Number，**唯一**仍以 Number 形式存在的 Long 字段，**用 `ytenant` 替代**（详见 [22 §七](../10-common/04_Long精度防御.md#七tenant-字段的特殊处理)）
- **业务状态/枚举（`status`、`verifystate`、`iDeleted`）** —— 0~9999 范围，V8 安全

#### data 元素中的子表数据

> **billDataKey确定规则**：子表billDataKey从元数据的`tables`目录的key获取，详见 `iuap-c-metadata-info/output-spec.md`。

#### 双路径使用模式（param.data 优先 + requestData 兜底）

> **架构师视角**：YonBIP 平台为 Rule 脚本设计了两个取值入口，**这是有意为之的标准双路径**，不是临时方案。

```
┌──────────────────────────────────────────────────────────────────┐
│           MDD 规则链 param 双路径设计（架构师视角）                │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  路径 A（推荐 · 主路径）                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  param.data                                                │   │
│  │  ├─ 平台已解析 → 嵌套对象数组                              │   │
│  │  ├─ id/orgId/transType → 已被平台 String 化               │   │
│  │  ├─ 子表 → bill.orderDetails（嵌套对象数组）               │   │
│  │  └─ ✅ 直接 bill.fieldName 调用                           │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│                              │ 何时回退到 B？                    │
│                              │ • param.data 为空                 │
│                              │ • param.data 缺少关键字段          │
│                              │ • 需要原始 refId 关联关系          │
│                              ▼                                   │
│  路径 B（兜底 · 老 skill 兼容）                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  param.requestData                                         │   │
│  │  ├─ 平台未解析 → 扁平 JSON 字符串                          │   │
│  │  ├─ id/orgId → Number（必失真）                            │   │
│  │  ├─ 子表 → 字段名是 JSON 数组字符串                        │   │
│  │  └─ ⚠️ safeJsonParse 后才能用                              │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**为什么 param.data 优先（核心 4 个理由）**：

1. **平台已 String 化保护**：`param.data[0].id` 是 String（19 位），不会失真；`param.requestData` 的 id 是 Number（必失真）
2. **嵌套结构更易用**：`bill.orderDetails[0].material.code` 直接点操作 vs `JSON.parse(...).orderDetails[0].material.code` 两步走
3. **业务字段自动拉平**：参照字段的 `code/name` 已被平台预拉平（如 `supplierId.code` 直接可用）
4. **历史脚本兼容**：老 skill 都基于 `param.data` 设计，迁移成本最低

**决策表（何时用 A / B / A+B 兜底）**：

| 场景 | 推荐路径 | 原因 |
|------|---------|------|
| **新开发** Rule 脚本 | ✅ 仅用 A（`param.data`） | 平台已保护，足够 |
| **老脚本迁移** | ✅ 仅用 A | 优先重构，删除 requestData 依赖 |
| `param.data` 为空（afterAudit 等） | ⚠️ **A 为主 + B 兜底** | 见 5 步反查法 [20](../30-scenes/01_审批后弃审后反查.md) |
| `param.data` 缺关键子表字段 | ⚠️ **A 为主 + B 补** | param.data 是拉平后的视图，B 是原始数据 |
| 调试 / 需要原始 refId 关联 | 仅用 B | param.data 已加工，丢失了原始关联 |
| 历史兼容（脚本无法改） | 仅用 B | 维持原状 |

**A+B 兜底代码模式**（推荐用于审批后/弃审后场景）：

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    var bill = null;
    
    // 路径 A：优先 param.data
    if (param.data && param.data[0]) {
      bill = param.data[0];
    } 
    // 路径 B：兜底 param.requestData
    else if (param.requestData) {
      bill = safeJsonParse(param.requestData, {
        longFields: ['id', 'orgId', 'transType', 'userId', 'tenant'],
        lengthThreshold: 16
      });
    }
    // 5 步反查法：从 context.ids / param.ids 提取
    else {
      // 见 references/20_审批后弃审后反查.md §二
      var billId = extractBillId(context, param);
      // ... 走 queryByYonQL
    }
    
    if (!bill) {
      throw new Error('【MyTrigger】单据数据为空，路径 A 和 B 均未取到');
    }
    
    // ✅ bill.id 已是 String（来自 A）或经 safeJsonParse（来自 B）
    var billId = bill.id;
    
    return {};
  }
}
```

**反向场景：param.data 不完整时补全**：

```javascript
// 场景：param.data[0] 有但子表为空，需从 requestData 取子表
var bills = param.data || [];
var bill = bills[0];

if (bill && (!bill.orderDetails || bill.orderDetails.length === 0)) {
    // 用 requestData 补全子表
    if (param.requestData) {
        var raw = safeJsonParse(param.requestData, {
            longFields: ['id', 'orderDetails.*.material', 'orderDetails.*.qty'],
            lengthThreshold: 16
        });
        if (raw.orderDetails) {
            bill.orderDetails = safeJsonParse(raw.orderDetails, { longFields: [], lengthThreshold: 16 });  // 子表字段本身是 JSON 字符串
        }
    }
}
```

### 2.2 子表字段取值

| 字段类型 | 取值路径 | 示例 |
|---------|---------|------|
| 子表数组 | `bill.{billDataKey}` | `bill.orderDetails`, `bill.objectList` |
| 子表字段 | `bill.{billDataKey}[i].fieldName` | `bill.objectList[0].supplierId.code` |
| 子表特征 | `bill.{billDataKey}[i].userDefineCharacter.特征` | `bill.objectList[0].userDefineCharacter.RZRW01` |
| 孙表数组 | `bill.{billDataKey}[i].tables.{孙表key}` | `bill.objectList[0].tables.obsoleteList` |
| 孙表字段 | `bill.{billDataKey}[i].tables.{孙表key}[j].field` | `bill.objectList[0].tables.obsoleteList[0].status` |

子表以嵌套数组形式存在于 data 元素中：

```javascript
// 访问子表数据
var bills = param.data || [];
var bill = bills[0];
// billDataKey: orderDetails, objectList, productList 等
var details = bill.orderDetails || [];
```

**取值示例**：

```javascript
// 主表字段
var code = bill.code;
var supplierCode = bill.supplierId.code;

// 子表字段
var objectList = bill.objectList || [];
if (objectList.length > 0) {
    var supplierCode = objectList[0].supplierId.code;
    var materialCode = objectList[0].material.code;
}

// 孙表字段
var obsoleteList = objectList[0].tables.obsoleteList || [];
if (obsoleteList.length > 0) {
    var status = obsoleteList[0].status;
}
```

每个子表行也包含 `_realtype`、`_entityName`、`_keyName`、`_status` 等元信息。

子表行通过 `_status` 标记变更类型：
- `"Insert"` — 新增行
- `"Update"` — 修改行
- `"Unchanged"` — 未变更行
- `"Delete"` — 删除行

#### data 元素中的嵌套对象

部分字段以嵌套对象形式存在（而非扁平化），如：
- `orderPrices` — 价格信息对象
- `orderDefineCharacter` — 自定义特性对象
- `orderDetails[].orderDetailPrices` — 行价格信息
- `orderDetails[].orderProductApportions` — 行分摊信息

---

### param.parameters

操作参数信息：

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| `serviceCode` | string | 服务编码 | `"voucher_orderchange"` |
| `cmdname` | string | 命令名 | `"cmdSave"` |
| `businessActName` | string | 业务动作名称 | `"销售订单变更-保存"` |
| `sbillno` | string | 列表单据编号 | `"voucher_orderchangelist"` |
| `source` | string | 来源 | `"u8c"` |
| `idempotencySave` | string | 幂等保存标识 | `"voucher_orderchange_copy_..."` |
| `terminalType` | string | 终端类型 | `"1"` |

---

## 三、三个场景字段取值对比

> ⚠️ 此对比表的**唯一权威版本**在 [../40-guides/01_metadata-yonsql-guide.md](../40-guides/01_metadata-yonsql-guide.md) 第三节。
> 本处仅保留 MDD vs YPD 二元对比（MDD/YONSQL 三元对比请查阅 `../40-guides/01_metadata-yonsql-guide.md`）。

| 对比项 | MDD 规则链 | YPD 扩展点 |
|--------|-----------|-----------|
| 单据数据位置 | `param.data`（数组） | `param.billDOs` / `param.billDO` |
| 子表结构 | 嵌套对象数组 `bill.{key}` | `billDO.extendData.{key}` |
| 原始数据 | `param.requestData`（JSON字符串） | 无对应字段 |
| 上下文信息 | `context`（扁平结构） | `context`（扁平结构，字段更少） |
| 单据元数据 | `param.data` 中 `_entityName` 等 | `param.baseBillContext.billEntityInfo` |
| 流程信息 | 无 | `param.baseBillContext.extProps.bpmcompleteParam` |

| 对比项 | MDD 规则链 | YPD 扩展点 |
|--------|-----------|-----------|
| 单据数据位置 | `param.data`（数组） | `param.billDOs` / `param.billDO` |
| 子表结构 | 嵌套对象数组 `bill.{key}` | `billDO.extendData.{key}` |
| 原始数据 | `param.requestData`（JSON字符串） | 无对应字段 |
| 上下文信息 | `context`（扁平结构） | `context`（扁平结构，字段更少） |
| 单据元数据 | `param.data` 中 `_entityName` 等 | `param.baseBillContext.billEntityInfo` |
| 流程信息 | 无 | `param.baseBillContext.extProps.bpmcompleteParam` |

---

## 脚本中使用示例

### 获取上下文和单据数据

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    // 从 context 获取操作信息
    var action = context.action;
    var billName = context.name;
    var tenantId = context.tenant;
    var billnum = context.billnum;

    // 从 param 获取单据数据
    var bills = param.data || [];
    var bill = bills[0];
    var billId = bill.id;
    var billCode = bill.code;

    //自定义方法
    return {};
  }
}
exports({ entryPoint: MyTrigger });
```

### 读取子表数据并处理

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    var bills = param.data || [];
    var bill = bills[0];
    var details = bill.orderDetails || [];

    for (var i = 0; i < details.length; i++) {
      var detail = details[i];
      // 通过 _status 判断行变更类型
      if (detail._status === 'Insert') {
        // 处理新增行
      }
      if (detail._status === 'Update') {
        // 处理修改行
      }
    }

    //自定义方法
    return {};
  }
}
exports({ entryPoint: MyTrigger });
```

### 解析 requestData（不推荐）

> ⚠️ `param.requestData` 为扁平 JSON 字符串，**未受 YonBIP 平台 String 化保护**，含 19 位 Long 字段时**必然失真**。
> 必须用 [safeJsonParse](../10-common/04_Long精度防御.md#四safejsonparse-工具函数) 解析，禁止直接 `JSON.parse`。
> 完整规范见 [../00-governance/01_技术规范.md §规范8](../00-governance/01_技术规范.md#规范8long-字段精度防御新增)。
> 业务上推荐优先用 `param.data`（含嵌套对象、已部分受平台保护）。

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    // ❌ 错误：直接 JSON.parse，Long 字段失真
    // var requestData = JSON.parse(param.requestData);

    // ✅ 正确：safeJsonParse
    var requestData = param.requestData ? safeJsonParse(param.requestData, {
        longFields: [
            'id', 'parentId', 'orderId', 'orgId', 'transType',
            'userId', 'creatorId', 'modifierId', 'auditorId',
            'tenant', 'purchaseOrderSourceId', 'purchaseOrderSourceDetailId'
        ],
        lengthThreshold: 16
    }) : {};

    // requestData 是扁平结构，子表字段名为字符串化的JSON数组
    // 如：requestData["orderDetails"] 是 JSON 数组字符串

    return {};
  }
}
exports({ entryPoint: MyTrigger });
```

### 获取操作参数

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    var params = param.parameters || {};
    var serviceCode = params.serviceCode;
    var cmdName = params.cmdname;
    var businessActName = params.businessActName;

    //自定义方法
    return {};
  }
}
exports({ entryPoint: MyTrigger });
```
