# YPD 扩展点脚本入参结构

> **加载时机**：YPD 扩展点触发时需要获取单据数据时
> **场景定位**：从 `param.billDOs` 中获取当前单据数据
>
> **⚠️ 字段命名契约**（字段类型识别、参照/特征/子表/平行表写法）已统一在 [../10-common/03_字段命名契约.md](../10-common/03_字段命名契约.md) 中维护，本文档不再重复。
> **⚠️ 审批后/弃审后 only-id 场景**（5 步反查法）见 [../30-scenes/01_审批后弃审后反查.md](../30-scenes/01_审批后弃审后反查.md)，**不在此重复**。
> **⚠️ 三场景对比表**（MDD vs YONSQL vs YPD 字段取值差异）已统一在 [../40-guides/01_metadata-yonsql-guide.md](../40-guides/01_metadata-yonsql-guide.md) 中维护，本文档不再重复。
> **⚠️ 技术规范**（URI/日志/openLinker/postman/safeJsonParse/YonSQL/字段取值）见 [../00-governance/01_技术规范.md](../00-governance/01_技术规范.md)，**不在此重复**。

---

## 一、场景定位

YPD 扩展点从 `param.billDOs` 数组获取单据数据，与 MDD 规则链（`param.data`）的差异详见 [../40-guides/01_metadata-yonsql-guide.md](../40-guides/01_metadata-yonsql-guide.md) 的"三场景字段取值对比"小节。

---

## 二、字段取值规范

> **核心原则**：所有字段名必须从元数据 `attributes[].name` 获取，禁止臆想。详见 [../00-governance/01_技术规范.md 规范 7](../00-governance/01_技术规范.md#规范7字段取值规范)。

### 2.1 主表字段取值

| 字段类型 | 取值路径 | 示例 |
|---------|---------|------|
| 基础字段 | `bill.fieldName` | `bill.code`, `bill.name` |
| 参照字段 | `bill.fieldName.subField` | `bill.supplierId.code` |
| 特征组 | `bill.userDefineCharacter.特征编码` | `bill.userDefineCharacter.RZRW01` |

---

YPD 扩展点绑定的 **Rule 脚本**（`AbstractTrigger`）在 `execute(context, param)` 中接收两个独立参数：

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    // context — 当前操作的上下文信息
    // param  — 包含完整单据数据和操作上下文
    return {};
  }
}
exports({ entryPoint: MyTrigger });
```

> 不同扩展点（保存前/后、提交前/后、审核前/后）触发时，`context.action` 和 `param.billDOs` 中的数据状态会有差异。
> 完整的原始参数数据参见 `samples/YPD扩展点入参结构.json`。

---

## context 结构

context 提供当前操作的基本上下文信息：

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| `busiObj` | string | 业务对象URI（领域.实体编码） | `"productcenter.aa_customerapply"` |
| `cBillNo` | string | 单据编码 | `"aa_customerapply"` |
| `billnum` | string | 单据编号（同cBillNo） | `"aa_customerapply"` |
| `userName` | string | 当前操作人姓名 | `"测试环境管理员"` |
| `userId` | string | 当前操作人ID | `"99ea7655-00a2-4bda-b23c-19ade37ea574"` |
| `billName` | string | 单据名称 | `"客户申请单"` |
| `name` | string | 单据名称（同billName） | `"客户申请单"` |
| `domain` | string | 领域编码 | `"productcenter"` |
| `tenantId` | string | 租户ID | `"0000L6YTYEY5FUZPXE0000"` |
| `tenant` | string | 租户ID（同tenantId） | `"0000L6YTYEY5FUZPXE0000"` |
| `action` | string | 当前触发的动作 | `"audit"`（审核）、`"save"`（保存）、`"submit"`（提交） |
| `fullname` | string | 元数据全路径 | `"aa.customerapply.CustomerApply"` |
| `metaFullName` | string | 元数据全名（同fullname） | `"aa.customerapply.CustomerApply"` |

### context 示例

```json
{
  "busiObj": "productcenter.aa_customerapply",
  "cBillNo": "aa_customerapply",
  "billnum": "aa_customerapply",
  "userName": "测试环境管理员",
  "userId": "99ea7655-00a2-4bda-b23c-19ade37ea574",
  "billName": "客户申请单",
  "domain": "productcenter",
  "tenantId": "0000L6YTYEY5FUZPXE0000",
  "action": "audit",
  "fullname": "aa.customerapply.CustomerApply",
  "metaFullName": "aa.customerapply.CustomerApply"
}
```

### 常见 action 值

| action | 说明 | 触发时机 |
|--------|------|---------|
| `save` | 保存 | 单据保存前/后 |
| `submit` | 提交 | 单据提交前/后 |
| `audit` | 审核 | 单据审核前/后 |
| `unaudit` | 反审核 | 单据反审核前/后 |
| `delete` | 删除 | 单据删除前/后 |

---

## param 结构

param 是一个复杂的嵌套对象，包含完整的单据数据和操作上下文。

### 一级字段概览

| 字段 | 类型 | 说明 |
|------|------|------|
| `param` | object | 附加参数（通常为空对象） |
| `isvExtensionScripts` | null | ISV扩展脚本（通常为null） |
| `externalData` | null | 外部数据（通常为null） |
| `baseBillContext` | object | **单据基础上下文**（详见下文） |
| `billDOs` | array | **单据数据数组**（优先使用，详见下文） |
| `billDO` | object | 单据数据对象（`billDOs` 为 null 时使用，结构与 `billDOs` 中元素相同） |
| `bizExtendData` | null | 业务扩展数据 |
| `workflowParam` | null | 工作流参数（工作流相关操作时有值） |
| `formulaMap` | null | 公式映射 |

---

### baseBillContext 结构

单据的基础上下文信息，包含元数据定义、动作信息等：

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| `appName` | string | 应用名称 | `"productcenter"` |
| `domain` | string | 领域编码 | `"productcenter"` |
| `subId` | string | 子系统标识 | `"PC"` |
| `cardKey` | string | 卡片Key | `"aa_customerapply"` |
| `busiObj` | string | 业务对象URI | `"productcenter.aa_customerapply"` |
| `serviceCode` | string | 服务编码 | `"aa_customerapply"` |
| `billName` | string | 单据名称 | `"客户申请单"` |
| `cBillNo` | string | 单据编码 | `"aa_customerapply"` |
| `metaFullName` | string | 元数据全名 | `"aa.customerapply.CustomerApply"` |
| `billEntityInfo` | object | **实体元数据信息**（字段映射等） | |
| `actionInfo` | object | **动作信息**（当前操作详情） | |
| `extProps` | object | **扩展属性**（BPM流程参数等） | |
| `isFromYnf` | boolean | 是否来自YNF框架 | `false` |
| `dataPowerEnable` | boolean | 是否启用数据权限 | `true` |

#### baseBillContext.billEntityInfo

实体元数据定义，包含字段映射关系：

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| `metaFullName` | string | 元数据全名 | `"aa.customerapply.CustomerApply"` |
| `pkFieldName` | string | 主键字段名 | `"id"` |
| `tableName` | string | 数据库表名 | `"customerapply"` |
| `billField` | object | **字段映射表**（key为标准字段标识，value为实际字段名） | |

**billField 字段映射示例：**

| 标准标识 | 实际字段名 | 说明 |
|---------|-----------|------|
| `field_id` | `"id"` | 主键 |
| `field_code` | `"code"` | 编码 |
| `field_name` | `"name"` | 名称 |
| `field_creatorId` | `"creator"` | 创建人 |
| `field_modifier` | `"modifier"` | 修改人 |
| `field_createTime` | `"createTime"` | 创建时间 |
| `field_modifyTime` | `"modifyTime"` | 修改时间 |
| `field_auditTime` | `"auditTime"` | 审核时间 |
| `field_auditor` | `"auditor"` | 审核人 |
| `field_billStatus` | `"verifyState"` | 单据状态 |
| `field_orgId` | `"orgId"` | 组织 |
| `field_bustype` | `"bustype"` | 业务类型 |
| `field_enable` | `"enable"` | 启用状态 |

> `billField` 中的映射关系可用于在脚本中动态获取字段名，避免硬编码。

#### baseBillContext.actionInfo

当前触发的动作详情：

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| `code` | string | 动作编码 | `"audit"` |
| `origCode` | string | 原始动作编码 | `"bpmcomplete"` |
| `url` | string | 动作URL | `"/bpm/complete"` |
| `contextPath` | string | 上下文路径 | `"/iuap-apdoc-material"` |
| `async` | boolean | 是否异步 | `false` |
| `busiObj` | string | 业务对象 | `"productcenter.aa_customerapply"` |
| `billNum` | string | 单据编号 | `"aa_customerapply"` |
| `busiType` | string | 业务类型 | `""` |
| `fromAPI` | boolean | 是否来自API调用 | `false` |
| `fromOpenAPI` | boolean | 是否来自OpenAPI | `false` |

#### baseBillContext.extProps（BPM流程相关）

工作流审批相关的扩展参数：

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| `bpmcompleteParam` | object | BPM完成参数 | |
| `bpmcompleteParam.businessKey` | string | 业务主键 | `"aa_customerapply_2539428645377146885"` |
| `bpmcompleteParam.userId` | string | 审批人ID | `"99ea7655-..."` |
| `bpmcompleteParam.userName` | string | 审批人姓名 | `"测试环境管理员"` |
| `bpmcompleteParam.processDefinitionKey` | string | 流程定义Key | `"processcenter_76b136b6"` |
| `bpmcompleteParam.tenantId` | string | 流程租户ID | `"0000L6YTYEY5FUZPXE0000_PC"` |
| `bpmcompleteParam.source` | string | 来源 | `"PC"` |
| `bpmcompleteParam.processInstId` | string | 流程实例ID | `"62a3d0d3-..."` |
| `bpmcompleteParam.processEnd` | string | 流程是否结束 | `"true"` / `"false"` |
| `bpmcompleteParam.msgComment` | string | 审批意见 | `"同意"` |
| `bpmcompleteParam.approvalAction` | string | 审批动作 | `"complete"` |
| `bpmcompleteParam.billNo` | string | 单据编号 | `"aa_customerapply"` |

---

### 获取单据数据的推荐方式

优先使用 `param.billDOs`（数组），当 `billDOs` 为 null 时回退到 `param.billDO`：

```javascript
// 推荐的取值方式
var bills = param.billDOs || (param.billDO ? [param.billDO] : []);
```

`billDOs` 中每个元素的结构与 `billDO` 相同，详见下方字段说明。

### billDO 结构

包含当前单据的完整业务数据。**关键认知**：`billDOs[0]` / `billDO` 是 YonBIP 平台**已解析的嵌套对象**，**业务主键/外键已被平台 String 化保护**（详见 [../10-common/04_Long精度防御.md §二](../10-common/04_Long精度防御.md#二yonbip-平台实际保护机制)），可直接使用。

| 字段 | JavaScript 实际类型 | 平台保护 | 失真风险 | 说明 | 示例值 |
|------|------------------|---------|---------|------|--------|
| `id` | **string** | ✅ 已保护 | ✅ 安全 | **单据主键**（19 位 Long，平台 String 化） | `"2539428645377146885"` |
| `code` | string | - | - | 单据编码 | `"12312312002"` |
| `status` | number | - | ✅ 安全 | 业务状态 | `3` |
| `verifystate` | number | - | ✅ 安全 | 审核状态 | `1` |
| `isWfControlled` | boolean | - | ✅ 安全 | 是否受工作流控制 | `true` |
| `isDeleted` | boolean | - | ✅ 安全 | 是否已删除 | `false` |
| `stopstatus` | boolean | - | ✅ 安全 | 是否停用 | `false` |
| `creator` | string | - | - | 创建人 | `"测试环境管理员"` |
| `creatorId` | string | ✅ 已保护 | ✅ 安全 | 创建人ID（19 位 Long，平台 String 化） | `"1525695460210638858"` |
| `createTime` | string | - | - | 创建时间 | `"2026-05-15 15:00:03"` |
| `modifier` | string | - | - | 修改人 | `"测试环境管理员"` |
| `modifierId` | string | ✅ 已保护 | ✅ 安全 | 修改人ID（19 位 Long，平台 String 化） | `"1525695460210638858"` |
| `modifyTime` | string | - | - | 修改时间 | `"2026-05-21 09:37:29"` |
| `ytenant` | string | - | ✅ 安全 | 租户编码（含字母，String 形式） | `"0000L6YTYEY5FUZPXE0000"` |
| `tenant` | number | ⚠️ **未保护** | ⚠️ **19 位有失真风险** | **租户数字 ID**（YonBIP 平台保护不完整） | `2910033146761808` |
| `pubts` | string | - | - | 时间戳 | `"2026-05-21 09:42:56"` |
| `entityName` | string | - | - | 实体名 | `"aa.customerapply.CustomerApply"` |
| `entityStatus` | string | - | - | 实体状态 | `"Unchanged"` |
| `_status` | number | - | ✅ 安全 | 状态码（0/1/2） | `0` |
| **业务字段** | | | | *以下为各单据的业务字段，因单据类型不同而不同* | |
| `applyType` | number | - | ✅ 安全 | 申请类型（枚举） | `0` |
| `applyCode` | string | - | - | 申请编码 | `"12312312002"` |
| `merchantCode` | string | - | - | 商户编码 | `"00000000032365"` |
| `merchantName` | string | - | - | 商户名称 | `"ypd05151459"` |
| `applicationOrg` | string | ✅ 已保护 | ✅ 安全 | 申请组织ID（19 位 Long，平台 String 化） | `"1529979475333742597"` |
| `applyDepartment` | string | ✅ 已保护 | ✅ 安全 | 申请部门ID（19 位 Long，平台 String 化） | `"1526180671237128197"` |
| `bizOperator` | string | ✅ 已保护 | ✅ 安全 | 业务经办人ID（19 位 Long，平台 String 化） | `"1952441692752707593"` |
| `customerData` | **string (JSON 字符串)** | ⚠️ **未保护** | ⚠️ **未解析** | **客户数据**（JSON 字符串，需 `safeJsonParse`） | `"{"merchantRole!_status":"Insert",...}"` |
| `effectStatus` | number | - | ✅ 安全 | 生效状态（0/1） | `0` |
| `effectType` | number | - | ✅ 安全 | 生效类型 | `0` |
| `failInfo` | string | - | - | 失败信息 | `null` |

**关键说明**：

- **`id`、参照外键（`applicationOrg`、`applyDepartment`、`bizOperator`、`creatorId`、`modifierId`）** —— 19 位 Long 已被平台 String 化，**直接用 String**
- **`tenant`** —— 19 位 Number，**唯一**未受保护字段，**用 `ytenant` 替代**
- **`customerData`** —— String 字段但内容是 JSON 字符串（**扁平结构含 Long 字段**），**解析时必须用 `safeJsonParse`**
- **业务状态/枚举（`status`、`verifystate`、`applyType`、`_status`）** —— 0~9999 范围，V8 安全

#### billDO 中的子表数据

> **billDataKey确定规则**：子表billDataKey从元数据的`tables`目录的key获取，详见 `iuap-c-metadata-info/output-spec.md`。

### 2.2 子表字段取值

| 字段类型 | 取值路径 | 示例 |
|---------|---------|------|
| 子表数组 | `bill.extendData.{billDataKey}` | `bill.extendData.detailList` |
| 子表字段 | `bill.extendData.{key}[i].fieldName` | `bill.extendData.detailList[0].fieldName` |

#### 双路径使用模式（billDOs 优先 + customerData 兜底）

> **架构师视角**：YonBIP 平台为 YPD 扩展点设计了两个核心数据入口 + 一个嵌入式 JSON 字段。**billDOs/customerData 都是"主路径"**，`requestData` 不存在（YPD 场景特有）。

```
┌──────────────────────────────────────────────────────────────────┐
│         YPD 扩展点 param 多入口设计（架构师视角）                  │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  入口 1（推荐 · 主路径 A）                                        │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  param.billDOs[0] / param.billDO                          │   │
│  │  ├─ 平台已解析 → 主表 + extendData.子表                  │   │
│  │  ├─ id / 外键 → 已被平台 String 化                       │   │
│  │  └─ ✅ 直接 bill.fieldName 调用                          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  入口 2（嵌入式 JSON · 特殊场景）                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  param.billDO.customerData                                │   │
│  │  ├─ String 字段（内容是 JSON 字符串）                    │   │
│  │  ├─ 未受平台 String 化保护                                │   │
│  │  └─ ⚠️ safeJsonParse 后才能用                             │   │
│  │                                                            │   │
│  │  典型场景：客户申请单的客户数据嵌套 JSON                   │   │
│  │  {"merchantRole!settlementMethod":"0",                    │   │
│  │   "orgId":"1567954261903933444", ...}                    │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  入口 3（context 上下文）                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  context.{action, billnum, userId, tenant, ...}           │   │
│  │  ├─ 全部 String（context 路径 YonBIP 平台已保护）         │   │
│  │  └─ ✅ 直接 context.fieldName                              │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ⚠️ 注意：YPD 扩展点没有 param.requestData，                      │
│     若 billDOs 不可用，参考 MDD 场景的 5 步反查法                 │
│     [../30-scenes/01_审批后弃审后反查.md](../30-scenes/01_审批后弃审后反查.md)│
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

**为什么 billDOs 优先（核心 4 个理由）**：

1. **平台已 String 化保护**：`billDOs[0].id` 是 String（19 位），不会失真
2. **嵌套结构 + 子表统一入口**：`bill.extendData.{子表 key}` 一次拿全
3. **参照字段自动拉平**：`applicationOrg` / `applyDepartment` 等已经是 String，直接用
4. **与 MDD 规则链一致**：开发模式统一，迁移成本低

**决策表（何时用入口 1/2/3）**：

| 场景 | 推荐入口 | 原因 |
|------|---------|------|
| 业务主流程（取主表/子表/参照） | ✅ 入口 1（`billDOs`） | 平台已保护，足够 |
| 需要客户的"扩展数据"（非标字段） | ✅ 入口 2（`customerData`） | 含业务自定义字段 |
| 上下文元数据（操作人、单据类型、租户） | ✅ 入口 3（`context`） | 与 action 强相关 |
| `billDOs` 为空 | ⚠️ 5 步反查法 | 见 [reference 20](20_审批后弃审后反查.md) |
| 需要原始 refId 关联 | ⚠️ 入口 2 部分场景 | customerData 含原始关联 |

**入口组合代码模式**（典型 YPD 脚本）：

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    // 入口 1：主路径（主表 + 子表）
    var bill = (param.billDOs && param.billDOs[0]) || param.billDO;
    if (!bill) {
      throw new Error('【MyTrigger】billDOs 与 billDO 均为空');
    }
    
    // ✅ bill.id 已是 String（平台保护）
    var billId = bill.id;
    var billCode = bill.code;
    
    // 入口 2：customerData 嵌入式 JSON（如需）
    if (bill.customerData) {
      // ⚠️ 必须用 safeJsonParse
      var customerData = safeJsonParse(bill.customerData, {
        longFields: [
          'orgId', 'transType', 'allocatorId', 'id', 
          'merchantCharacter.*', 'merchantRole!*', 'merchantAppliedDetail!*'
        ],
        lengthThreshold: 16
      });
      // customerData 现在可安全使用
    }
    
    // 入口 3：context 上下文
    var action = context.action;       // "audit"
    var userId = context.userId;       // "99ea7655-00a2-4bda-b23c-19ade37ea574"
    var tenantId = context.tenantId;   // "0000L6YTYEY5FUZPXE0000"
    
    return {};
  }
}
exports({ entryPoint: MyTrigger });
```

**反向场景：billDOs 子表为空时从 customerData 补**：

```javascript
// 场景：billDOs 有但 extendData 子表为空，需从 customerData 补
if (bill && (!bill.extendData || !bill.extendData.detailList || bill.extendData.detailList.length === 0)) {
    if (bill.customerData) {
        var raw = safeJsonParse(bill.customerData, {
            longFields: ['*'],  // 嵌入式 JSON 含任意 Long 字段
            lengthThreshold: 16
        });
        // raw 现已是对象，可安全使用
    }
}
```

billDO 的 `extendData` 中包含子表数据，以子表名为 key：

```javascript
// 访问子表数据
var bill = (param.billDOs && param.billDOs[0]) || param.billDO;
// billDataKey: customerApplyBpmSteps, objectList, detailList 等
var bpmSteps = bill.extendData.customerApplyBpmSteps;
// bpmSteps 是数组，每个元素是子表行对象
```

**取值示例**：

```javascript
// 主表字段
var code = bill.code;
var supplierCode = bill.supplierId.code;

// 子表字段
var detailList = bill.extendData.detailList || [];
if (detailList.length > 0) {
    var fieldValue = detailList[0].fieldName;
}
```

子表行对象结构示例（customerApplyBpmSteps）：

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| `id` | string | 子表行ID | |
| `customerApplyId` | string | 主表ID（关联主表） | |
| `flag` | string | 标识 | `"create"` |
| `processinstId` | string | 流程实例ID | |
| `taskId` | string | 任务ID | |
| `stepLastModify` | string | 步骤最后修改时间 | `"2026-05-21 09:43:11"` |
| `entityStatus` | string | 行状态 | `"Unchanged"` |

#### billDO 中的特性子对象

billDO 中可能包含关联的特性子对象（如 `customerApplyCharacter`），结构为：

```json
{
  "_fullName": "aa.customerapply.CustomerApplyCharacter",
  "ytenant": "0000L6YTYEY5FUZPXE0000",
  "id": "2539428653967343621",
  "ytenantId": "0000L6YTYEY5FUZPXE0000"
}
```

---

## 脚本中使用示例

### 获取上下文和单据数据

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    // 从 context 获取操作信息
    var action = context.action;          // 当前动作：save/submit/audit 等
    var billName = context.billName;       // 单据名称
    var domain = context.domain;           // 领域编码
    var tenantId = context.tenantId;       // 租户ID
    var userId = context.userId;           // 操作人ID
    var userName = context.userName;       // 操作人姓名

    // 从 param 获取单据数据
    var bills = param.billDOs || (param.billDO ? [param.billDO] : []);
    var bill = bills[0];
    var billId = bill.id;                  // 单据ID
    var billCode = bill.code;              // 单据编码
    var status = bill.status;              // 业务状态

    return {};
  }
}
exports({ entryPoint: MyTrigger });
```

### 根据不同动作执行不同逻辑

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    var action = context.action;
    var bills = param.billDOs || (param.billDO ? [param.billDO] : []);
    var bill = bills[0];

    if (action === 'audit') {
      // 审核前校验
      if (bill.status !== 2) {
        throw new Error('单据状态不是已提交，无法审核');
      }
    }

    if (action === 'save') {
      // 保存前校验
      if (!bill.merchantName) {
        throw new Error('商户名称不能为空');
      }
    }

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
    var bills = param.billDOs || (param.billDO ? [param.billDO] : []);
    var bill = bills[0];
    var bpmSteps = bill.extendData.customerApplyBpmSteps || [];

    for (var i = 0; i < bpmSteps.length; i++) {
      var step = bpmSteps[i];
      // 处理每个步骤记录
    }

    //自定义方法
    return {};
  }
}
exports({ entryPoint: MyTrigger });
```

### 获取BPM流程信息（审核扩展点）

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    var baseCtx = param.baseBillContext;
    var extProps = baseCtx.extProps;
    var bpmParam = extProps.bpmcompleteParam;

    if (bpmParam) {
      var processEnd = bpmParam.processEnd === 'true';
      var msgComment = bpmParam.msgComment;
      var processInstId = bpmParam.processInstId;

      // 流程结束后触发后续业务
      if (processEnd) {
        // 执行流程完成后的业务逻辑
      }
    }

    //自定义方法
    return {};
  }
}
exports({ entryPoint: MyTrigger });
```

---

## 安全解析 openLinker / postman 返回值（强制规范）

> ⚠️ **核心规则**：YonBIP 平台对 `param.billDOs[0].id` 等业务主键/外键已做 String 化保护（**直接用 String，无需处理**），但对 `ObjectStore.openLinker` / `ObjectStore.postman` 返回的 JSON 字符串**未做保护**。
> 任何 JSON 字符串反序列化必须用 [safeJsonParse](../10-common/04_Long精度防御.md#四safejsonparse-工具函数)，禁止直接 `JSON.parse`。
> 完整规范见 [../00-governance/01_技术规范.md §规范8](../00-governance/01_技术规范.md#规范8long-字段精度防御新增)。

### 4.1 openLinker 返回值安全解析

```javascript
var AbstractTrigger = require('AbstractTrigger');
class MyTrigger extends AbstractTrigger {
  execute(context, param) {
    // ✅ param.billDOs[0].id 已是 String（平台保护），直接用
    var billId = (param.billDOs && param.billDOs[0]) ? param.billDOs[0].id : null;
    console.error('【MyTrigger】billId:' + billId);
    
    // 调用 OpenAPI
    var url = '/yonbip/scm/purchaseorder/cardlineticketcollectionopen';
    var appCode = 'pu_applyorderlist';
    var body = {
      data: {
        id: billId,
        purchaseOrders: { id: '1234567890123456789' }
      }
    };
    
    var resultString = ObjectStore.openLinker('POST', url, appCode, body);
    
    // ❌ 错误：直接 JSON.parse，Long 字段失真
    // var result = JSON.parse(resultString);
    
    // ✅ 正确：safeJsonParse
    var result = safeJsonParse(resultString, {
        longFields: ['id', 'purchaseOrderSourceId', 'purchaseOrderSourceDetailId'],
        lengthThreshold: 16,
        excludeFields: []
    });
    
    return {};
  }
}
exports({ entryPoint: MyTrigger });
```

### 4.2 postman 返回值安全解析

```javascript
// 跨租户/三方 API：postman 返回字符串同样需 safeJsonParse
var token = func.execute();
var env = ObjectStore.env();
var url = env.url + '/iuap-api-gateway/yonbip/xxx/xxx?access_token=' + token.access_token;
var header = { 'Content-Type': 'application/json' };

var resultString = ObjectStore.postman('POST', url, header, body);

// ✅ 用 safeJsonParse
var result = safeJsonParse(resultString, {
    longFields: ['id', 'orgId', 'transType', 'userId'],
    lengthThreshold: 16
});
```

### 4.3 tenant 字段特殊处理

```javascript
// ⚠️ param.billDO.tenant 仍是 19 位 Number，有失真风险
// 优先使用 ytenant（String 形式）
var ytenant = param.billDO.ytenant;  // "0000L6YTYEY5FUZPXE0000"（不失真）
var tenant = (param.billDO.tenant).toString();  // 19 位 Number 转 String（toString 不修复已失真值，仅兜底）

// SQL 中建议用 ytenant
var sql = `... where ytenant = '${ytenant}'`;
```

### 4.4 与 MDD 规则链的差异

| 路径 | MDD 规则链 | YPD 扩展点 |
|------|-----------|-----------|
| param.data / billDOs 中 id | ✅ 平台保护 | ✅ 平台保护 |
| param.requestData（MDD 专属） | ❌ 未保护，safeJsonParse | - |
| openLinker 返回字符串 | ❌ 未保护，safeJsonParse | ❌ 未保护，safeJsonParse |
| postman 返回字符串 | ❌ 未保护，safeJsonParse | ❌ 未保护，safeJsonParse |
| customerData 字符串 | ❌ 未保护，safeJsonParse | ❌ 未保护，safeJsonParse |

> `customerData` 字段在 `billDO` 中也是扁平 JSON 字符串（见 [§billDO 结构](#billdo-结构)），同样需 safeJsonParse 解析。

---

## 三、三个场景字段取值对比

| 对比项 | MDD规则链 | YONSQL查询 | YPD扩展点 |
|--------|-----------|------------|-----------|
| **数据来源** | param.data 数组 | 数据库查询 | param.billDOs |
| **主表字段** | `bill.fieldName` | `fieldName` (SQL) | `bill.fieldName` |
| **参照字段** | `bill.supplierId.code` | `supplierId.code` | `bill.supplierId.code` |
| **子表数组** | `bill.{billDataKey}` | `FROM table` | `bill.extendData.{billDataKey}` |
| **子表字段** | `bill.{key}[i].field` | `{key}[i].field` | `bill.extendData.{key}[i].field` |
| **孙表** | `bill.{key}[i].tables.{subKey}[j]` | `{key}[i].tables.{subKey}[j]` | 不常用 |

---

## 四、相关文档

| 文档 | 用途 |
|------|------|
| `../40-guides/01_metadata-yonsql-guide.md` | YONSQL查询字段取值规范 |
| `01_MDD规则链入参.md` | MDD规则链入参结构 |
| `iuap-c-metadata-info/output-spec.md` | billDataKey 确定规则 |
