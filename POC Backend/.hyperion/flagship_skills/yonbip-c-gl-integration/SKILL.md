---
name: yonbip-c-gl-integration
description: 总账集成技能，提供凭证与第三方系统双向集成能力（凭证推送、凭证录入）以及Oracle总账数据在线迁移能力（历史凭证迁移、总账期初保存）。基于中间表+多线程+API调用的在线迁移架构，涵盖数据提取、字段映射、辅助核算转换、批量保存、进度查询、状态回写的全流程。
type: agent
triggers:
  - 凭证推送
  - 凭证集成
  - 凭证同步
  - 凭证导入
  - 期初导入
  - 数据迁移
  - Oracle迁移
  - ERP迁移
  - 历史凭证迁移
  - 总账期初保存
  - 在线数据迁移
  - 期初余额导入
  - 凭证批量导入
  - 期初在线保存
  - 中间表迁移
---

# 总账集成技能（General Ledger Integration Skill）

## 业务概述

本技能提供总账业务的完整集成解决方案，涵盖两大核心场景：

### 场景一：凭证与第三方系统集成
将BIP系统中的凭证数据推送至第三方财务系统（SAP、NC、U8、NCC、GS等），或从第三方系统录入凭证到BIP。

### 场景二：Oracle总账在线数据迁移
从Oracle EBS总账中间表在线迁移数据到YonBIP旗舰版，包括历史凭证迁移和总账期初保存。基于中间表+多线程+API调用的在线架构。

---

## 核心能力

### 模块一：凭证三方集成

> **⚠️ 业务流程范围**：仅包含以下4个步骤

1. **凭证查询**：从BIP系统查询凭证数据
2. **字段转换**：将BIP字段转换为第三方目标格式
3. **接口调用**：推送数据至第三方系统
4. **响应解析**：解析响应并回写状态

**详细说明：** 见 [reference/voucher_integration.md](reference/voucher_integration.md)

### 模块二：Oracle总账在线数据迁移

#### 2.1 总账期初保存

从Oracle期初余额中间表读取数据，转换为BIP总账期初保存API报文，通过在线API批量保存期初余额（含辅助核算），并轮询进度确认最终保存结果。

**数据流程：**
```
中间表 CUX_GL_B_SG_BALANCE_{YYYY}_{MM}
  → 按 (公司+科目+币种+期间) 分组
  → 构建辅助核算 itemMap
  → 合并相同辅助核算的行 (金额求和)
  → 调用 auxiliarySave API
  → 获取 reqId → 轮询 progress API
  → 回写 STATUS: Y(成功)/N(失败)/P(跳过)
```

**API 端点：**

| 用途 | URL | 方法 |
|------|-----|------|
| 期初保存 | `{domain}/iuap-api-gateway/{tenant}/wanda_be/sync2/datamigrate/balance/auxiliarySave?access_token={token}` | POST |
| 进度查询 | `{domain}/iuap-api-gateway/{tenant}/wanda_be/sync2/datamigrate/balance/progress?access_token={token}` | POST |

**期初保存报文结构：**
```json
{
  "accbook": "账簿ID",
  "period": "2024-01",
  "pk_org": "组织ID",
  "currency": "币种ID",
  "accsubject": "科目ID",
  "direction": "Debit/Credit",
  "bodies": [
    {
      "accbook": "账簿ID",
      "period": "2024-01",
      "currency": "币种ID",
      "accsubject": "科目ID",
      "direction": "Debit",
      "amount": "100.00",
      "localamount": "100.00",
      "early_amount": "100.00",
      "early_localamount": "100.00",
      "auxiliaryDisplayVO": {
        "itemMap": {
          "def1": {"code":"xxx","id":"xxx","name":"xxx","refcode":"ucf-org-center.bd_adminorgsharetreeref"}
        }
      }
    }
  ]
}
```

**进度查询响应判断：**
- `success=true && progress=1` → 保存成功，回写 STATUS="Y"
- `success=false && progress=1` → 保存失败，回写 STATUS="N"，记录 message
- `progress!=1` → 仍在处理中，需继续轮询

**金额方向处理：**
```java
// 期初数据有 BEGIN_BALANCE_DR(借) 和 BEGIN_BALANCE_CR(贷)
if ("Debit".equalsIgnoreCase(direction)) {
    resultAmount = drAmount.subtract(crAmount);
} else if ("Credit".equalsIgnoreCase(direction)) {
    resultAmount = crAmount.subtract(drAmount);
}
// amount / localamount / early_amount / early_localamount 四个字段都设为 resultAmount
```

**相同辅助核算行合并：**
```java
// 用 JSONUtil.toJsonStr(itemMap) 作为合并 key，相同辅助核算的多行金额累加
Map<String, Bodies> result = bodiesList.stream().collect(Collectors.toMap(
    e -> JSONUtil.toJsonStr(e.getAuxiliaryDisplayVO().getItemMap()),
    Function.identity(),
    (existing, replacement) -> {
        existing.setAmount(String.valueOf(
            new BigDecimal(existing.getAmount()).add(new BigDecimal(replacement.getAmount()))
        ));
        existing.setLocalamount(existing.getAmount());
        existing.setEarly_amount(existing.getAmount());
        existing.setEarly_localamount(existing.getAmount());
        return existing;
    }
));
```

**详细说明：** 见 [reference/period_initial_save.md](reference/period_initial_save.md)

#### 2.2 历史凭证迁移

从Oracle历史凭证中间表读取数据，按凭证编号分组构建凭证报文，调用BIP凭证保存OpenAPI完成导入。

**数据流程：**
```
中间表 CUX_GL_B_SG_JOURNAL_{YYYY}_{MM}
  → 按账簿查询数据
  → 按 JURAL_NUMBER(凭证编号) 分组
  → 每组构建一个凭证 JSON (多个分录)
  → 调用 addVoucher API
  → 回写 STATUS: Y(成功)/N(失败)/T(已存在)/P(跳过)/E(HTTP错误)
```

**API 端点：**

| 用途 | URL | 方法 |
|------|-----|------|
| 凭证保存 | `{domain}/iuap-api-gateway/yonbip/fi/ficloud/openapi/voucher/addVoucher?access_token={token}` | POST |

**凭证保存报文结构：**
```json
{
  "srcSystemCode": "figl",
  "accbookCode": "账簿编码",
  "voucherTypeCode": "1",
  "makerEmail": "username@wanda.com",
  "makeTime": "2024-01-15 00:00:00",
  "description": "凭证名",
  "defInfo1": "Oracle凭证编号",
  "defInfo2": "单据编号",
  "defInfo3": "凭证批号",
  "bodies": [
    {
      "description": "摘要",
      "accsubjectCode": "科目编码",
      "currencyCode": "CNY",
      "rateOrg": 1.0,
      "busidate": "2024-01-15 00:00:00",
      "debitOriginal": 100.00,
      "creditOriginal": 0,
      "debitOrg": 100.00,
      "creditOrg": 0,
      "billTime": "2024-01-15 00:00:00",
      "clientAuxiliaryList": [
        {"filedCode": "0001", "valueCode": "部门编码"},
        {"filedCode": "0035", "valueCode": "往来子目编码"}
      ],
      "cashflowList": []
    }
  ]
}
```

**凭证响应码处理：**

| code | 含义 | STATUS |
|------|------|--------|
| 200 | 保存成功 | Y |
| 310504/310074 | 凭证已存在 | T |
| 其他 | 保存失败 | N |
| HTTP异常 | 网络错误 | E |

**详细说明：** 见 [reference/voucher_migration.md](reference/voucher_migration.md)

---

## 架构分层与文件职责

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              外层 SKILL.md                                      │
│  技能主文件：定义技能基本信息、业务流程、触发条件                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
                                         │
                    ┌────────────────────┴────────────────────┐
                    │                                         │
                    ▼                                         ▼
┌──────────────────────────────────────┐  ┌──────────────────────────────────────┐
│   凭证三方集成模块                    │  │   在线数据迁移模块                    │
│   voucher_integration.md             │  │   period_initial_save.md              │
│   - 凭证查询                          │  │   voucher_migration.md                │
│   - 字段转换                          │  │   auxiliary_mapping.md                │
│   - 接口调用                          │  │   relation_mapping.md                 │
│   - 响应解析                          │  │   cache_and_threading.md              │
└──────────────────────────────────────┘  │   table_structure.md                  │
                    │                     │   api_and_status.md                   │
                    └────────────────────┬────────────────────┘
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    公共规范层 (gl_common_specification.md)                      │
│  共性能力：字段映射、API调用、数据转换、错误处理等通用逻辑                          │
│                    API集成层 (api_integration.md)                                │
│  BIP OpenAPI认证、HTTP工具、签名加密                                              │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 在线迁移核心架构

### 整体架构分层

```
┌───────────────────────────────────────────────────────────────────┐
│                       REST Controller 层                          │
│   SubjectAccountInitController / VorcherInitController            │
│   提供 HTTP 接口：单账簿、批量分发、API分发、进度查询               │
└───────────────────────────┬───────────────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────────────┐
│                      Service 层                                   │
│  SubjectAccountsInitService (期初) / VorcherInitService (凭证)     │
│  @PostConstruct 缓存 + 多线程分发 + 数据构建 + API调用            │
└───────────────────────────┬───────────────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────────────┐
│                    MyBatis Mapper 层                               │
│  CommonDaoMapper / SubjectAccountDaoMapper / VoucherDaoMapper     │
│  动态表名(${tableName})查询中间表 + 状态回写                       │
└───────────────────────────┬───────────────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────────────┐
│                 Oracle 中间表 (数据源)                             │
│  CUX_GL_B_SG_BALANCE_{YYYY}_{MM}  (期初)                         │
│  CUX_GL_B_SG_JOURNAL_{YYYY}_{MM}  (凭证)                         │
│  MAPPER_TABLE_ACCBOOK_PERIOD       (迁移范围配置)                 │
└───────────────────────────────────────────────────────────────────┘
```

### 迁移执行流程

```
1. Oracle 导出 dmp → 导入 BIP 数据库中间表
2. 配置 MAPPER_TABLE_ACCBOOK_PERIOD 指定迁移范围
3. @PostConstruct 启动时加载所有基础档案到内存 Map
4. Controller 接收请求 → Service 多线程分发
5. 每个线程：查中间表 → 构建报文 → 调API → 回写状态
6. 期初场景额外轮询 progress 接口确认最终结果
```

---

## 集成模式

| 模式 | 说明 | 适用场景 |
|------|------|---------|
| 动态配置模式 | 三方系统字段、接口配置从外部Excel/需求动态读取 | 凭证推送到第三方系统 |
| 在线迁移模式 | 从源系统中间表在线读取数据，转换后通过API导入BIP | Oracle到BIP的数据迁移 |
| 托底策略模式 | 无配置时的默认实现，托底数据全面 | 快速原型开发 |

---

## 公共机制

### 1. 基础档案缓存 (@PostConstruct)

启动时将所有参考数据加载到内存Map，避免导入时反复查询数据库。

| 缓存 Map | 数据来源 | Key |
|-----------|---------|-----|
| allSubjectAndExtMap | epub_accsubject_dimensionext | 科目ID |
| accountBookMap | epub_accountbook | 账簿编码 |
| accsubjectCodeAndAccsubjectMap | epub_accsubject | 编码+科目表ID |
| allDeptMap | org_orgs | 公司前缀+"_"+部门编码 |
| currencyMap | bd_currency_tenant | 币种编码 |
| vendorMap | aa_vendor | 供应商编码 |
| merchantMap | MERCHANT | 客户编码 |
| alternatingSubHeadMap | wd_fz_alternating_subhead | 往来子目编码 |
| payrollSubheadMap | wd_fz_payroll_subhead | 薪资子目编码 |
| assetsSubheadMap | wd_fz_assets_subhead | 资产子目编码 |
| brandInformationMap | wd_fz_brand_information | 品牌编码 |
| productServiceMap | wd_fz_product_service | 产品服务编码 |
| plazaInformationMap | wd_fz_plaza_information | 广场编码 |
| projectMap | bd_project | 项目编码 |
| orgFinBankacctMap | org_fin_bankacct | 银行账户编码 |
| mapperCustomerOrMerchantIntMap | MAPPER_CUSTOMER_OR_MERCHANT_INT | 编码+类型 |
| mapperSubaccMap | mapper_subacc | 子目编码 |
| mapperBankMap | BANK_MAPPING_T | 银行映射编码 |
| allVrsAndRefCodeMap | epub_multidimension_ext | 辅助核算编码 |

**详细说明：** 见 [reference/cache_and_threading.md](reference/cache_and_threading.md)

### 2. 辅助核算映射

辅助核算编码枚举 (AuxiliaryDisplayEnum)：

| 名称 | 编码 | 说明 |
|------|------|------|
| 部门 | 0001 | refcode: ucf-org-center.bd_adminorgsharetreeref |
| 项目 | 0002 | refcode: bd_project_ref |
| 供应商 | 0004 | refcode: aa_vendor_vendorref |
| 客户 | 0005 | refcode: merchant_customerref |
| 银行子目 | 021 | refcode: org_fin_bankacctref |
| 铺位 | 020 | 自定义档案 |
| 广场 | 0031 | 自定义档案 |
| 薪资子目 | 0032 | 自定义档案 |
| 资产子目 | 0033 | 自定义档案 |
| 品牌 | 0034 | 自定义档案 |
| 往来子目 | 0035 | 自定义档案 |
| 产品服务段 | 0036 | 自定义档案 |
| 前端客户 | 0051 | 系统客户编码 |
| 前端供应商 | 0052 | 系统供应商编码 |

**辅助核算两种表达方式：**

```java
// 期初保存：使用 def{N} + {code,id,name,refcode} 的 itemMap 结构
SimpleResultVO vrs = allVrsAndRefCodeMap.get("0001");
Map<String, String> def1 = new LinkedHashMap<>();
def1.put("code", dept.getCode());
def1.put("id", dept.getId());
def1.put("name", dept.getName());
def1.put("refcode", vrs.getRefCode());
itemMap.put(vrs.getVrs().replace("vr", "def"), def1);

// 凭证迁移：使用 filedCode + valueCode 的 ClientAuxiliaryList 结构
ClientAuxiliaryList aux = new ClientAuxiliaryList();
aux.setFiledCode("0001");
aux.setValueCode(deptCode);
```

**详细说明：** 见 [reference/auxiliary_mapping.md](reference/auxiliary_mapping.md)

### 3. 关联方/客户/供应商映射逻辑

**核心规则（关联方有值时）：**
1. 科目同时配了客户+供应商 → 有供应商编码时优先赋供应商，有客户编码时赋客户，都无则默认赋客户
2. 科目只配了供应商 → 关联方赋供应商
3. 科目只配了客户 → 关联方赋客户

**核心规则（关联方无值时）：**
1. 客户编码有值 → 先查映射表，找到映射关系则映射到关联方+前端客户，找不到则直接赋客户+前端客户
2. 供应商编码有值 → 直接赋供应商+前端供应商

**详细说明：** 见 [reference/relation_mapping.md](reference/relation_mapping.md)

### 4. 多线程分发策略

| 场景 | 分发策略 | 线程池 |
|------|---------|--------|
| 期初批量分发 | 按账簿分5个一组提交线程 | YmsExecutors |
| 期初数据子分片 | 按分组数/10拆分Map并行处理 | YmsExecutors |
| 凭证批量分发 | 按账簿总数/20分片提交线程 | YmsExecutors |
| 凭证数据子分片 | 按凭证编号Map/10拆分并行处理 | YmsExecutors |

### 5. 状态追踪

| STATUS | 含义 |
|--------|------|
| null | 待处理 |
| Y | 成功 |
| N | 失败（PARAMJSON/REMARK字段记录错误信息） |
| P | 跳过（借贷金额相减为0） |
| T | 已存在（BIP返回310504/310074） |
| E | HTTP异常 |

### 6. 迁移范围配置表 (MAPPER_TABLE_ACCBOOK_PERIOD)

| 字段 | 说明 |
|------|------|
| TABLE_NAME | 中间表名（如 CUX_GL_B_SG_BALANCE_2024_01） |
| PERIOD_NAME | 期间（如 2024-01） |
| TYPE_NAME | 类型（"总账期初" 或 "历史凭证"） |
| DR | 删除标记 |

---

## 特征字段说明

> **⚠️ 重要**：凭证的特征字段遵循 BIP 规范

### 主表特征字段

- 特征组名称：`defineDts`
- 说明：凭证主表的特征字段组

### 子表特征字段（如有）

- 特征组名称：`entryDts.defineDts` / `assDts.defineDts`
- 说明：凭证分录或辅助核算的特征字段组

> **⚠️ 通用逻辑**：特征字段翻译能力请参考 [char_translation_spec](../bip_pub_base_des/char_translation_spec.md)

---

## 快速开始

### 场景1：凭证推送到第三方系统

```java
// 1. 查询凭证数据
VoucherQueryService queryService = new VoucherQueryService();
Map<String, Object> voucherData = queryService.queryVoucher(voucherId);

// 2. 字段转换
VoucherFieldMapper fieldMapper = new VoucherFieldMapper();
JSONObject thirdPartyData = fieldMapper.convertToThirdParty(new JSONObject(voucherData));

// 3. 调用第三方接口
ThirdPartyVoucherService apiService = new ThirdPartyVoucherServiceImpl();
ThirdApiResponse response = apiService.pushVoucher(thirdPartyData);

// 4. 解析响应并回写状态
if (response.isSuccess()) {
    writeService.writeBack(voucherId, response);
}
```

### 场景2：总账期初在线保存

```
# 单账簿执行
GET /subject/subjectAccountsInit?com=账簿编码&tableName=CUX_GL_B_SG_BALANCE_2024_01&status=

# 批量分发
GET /subject/dispatchSubjectAccountsInit?status=

# 进度查询
GET /subject/subjectCheckProgress?coms=&tableName=CUX_GL_B_SG_BALANCE_2024_01
```

### 场景3：历史凭证在线迁移

```
# 单账簿执行
GET /vorcher/vorcherInit?com=账簿编码&tableName=CUX_GL_B_SG_JOURNAL_2024_01&status=

# 批量分发
GET /vorcher/dispatchVorcherInit?status=&remark=
```

---

## 参考文档

### 凭证三方集成模块
- **[voucher_integration.md](reference/voucher_integration.md)** - 凭证三方集成完整指南
- **[gl_common_specification.md](reference/gl_common_specification.md)** - 总账公共规范

### 在线数据迁移模块
- **[period_initial_save.md](reference/period_initial_save.md)** - 总账期初保存完整实现详解
- **[voucher_migration.md](reference/voucher_migration.md)** - 历史凭证迁移完整实现详解
- **[auxiliary_mapping.md](reference/auxiliary_mapping.md)** - 辅助核算映射详解（子目、关联方、客户、供应商）
- **[relation_mapping.md](reference/relation_mapping.md)** - 关联方/客户/供应商赋值优先级详解
- **[cache_and_threading.md](reference/cache_and_threading.md)** - 基础档案缓存与多线程分发机制
- **[table_structure.md](reference/table_structure.md)** - 中间表和映射表结构设计
- **[api_and_status.md](reference/api_and_status.md)** - API接口说明与状态追踪机制

### 公共基础
- **[api_integration.md](reference/api_integration.md)** - BIP OpenAPI集成说明

---

## 使用场景

**场景1：凭证推送到SAP**
```
用户："我需要将BIP凭证推送到SAP系统"
→ 阅读 voucher_integration.md 了解集成流程
→ 配置SAP接口地址和字段映射
→ 实现推送逻辑
```

**场景2：总账期初在线保存**
```
用户："我需要从Oracle期初余额在线保存到BIP"
→ 阅读 period_initial_save.md 了解完整流程
→ 参考 cache_and_threading.md 配置基础档案缓存
→ 参考 auxiliary_mapping.md 实现辅助核算映射
→ 参考 table_structure.md 设计中间表
```

**场景3：历史凭证在线迁移**
```
用户："我需要从Oracle历史凭证在线迁移到BIP"
→ 阅读 voucher_migration.md 了解完整流程
→ 参考 relation_mapping.md 处理关联方映射
→ 参考 api_and_status.md 了解状态回写机制
```

**场景4：处理导入失败数据**
```
用户："凭证导入失败，如何重新导入？"
→ STATUS=N的记录可以通过传入status=N重新执行
→ 错误信息记录在REMARK/PARAMJSON字段中
→ 修正数据后将STATUS置为null即可重新导入
```

**场景5：新增辅助核算维度**
```
用户："需要新增一个辅助核算维度"
→ 在 AuxiliaryDisplayEnum 中新增枚举值
→ 在 Service 的 @PostConstruct 中新增缓存Map
→ 在 buildDataAndSaveSubjectAccount 方法中新增映射逻辑
→ 更新 Mapper XML 新增查询
```

**场景6：开发类似功能**
```
用户："我需要开发从金蝶在线迁移到BIP的功能"
→ 参考本skill的整体架构和设计模式
→ 复用基础档案缓存、多线程分发等核心逻辑
→ 调整中间表结构和数据转换部分
```

---

## 检查清单

### 凭证三方集成
- [ ] 确认业务流程仅为"查询 → 字段转换 → 调用三方 → 解析响应"
- [ ] 三方接口地址、字段映射从外部Excel/需求动态读取
- [ ] 特征字段翻译引用公共基础文档
- [ ] 禁止硬编码任何厂商特定逻辑
- [ ] 托底策略可配置启用/禁用

### 期初保存
- [ ] 中间表已创建并导入数据
- [ ] MAPPER_TABLE_ACCBOOK_PERIOD 配置了迁移范围
- [ ] 所有基础档案数据在BIP系统中已存在
- [ ] 辅助核算映射表配置完整（子目、关联方、银行映射）
- [ ] 总账期初保存API已发布（非标品API，需基于前端按钮发布）
- [ ] 进度查询API已发布
- [ ] 科目方向确认正确（借/贷方向影响金额计算）

### 凭证迁移
- [ ] 中间表已创建并导入数据
- [ ] 凭证去重逻辑确认（def11字段存放原始凭证编号）
- [ ] 制单人信息确认（邮箱规则：username@wanda.com）
- [ ] 凭证来源设置为 "figl"
- [ ] API认证配置正确（appKey/appSecret）

### 通用
- [ ] 线程池大小合理（YmsExecutors 默认配置）
- [ ] 状态字段含义明确（Y/N/P/T/E）
- [ ] 错误信息截断在字段长度限制内
- [ ] 导入后的校验SQL已准备

---

## 技术栈

- **Java**: JDK 8+
- **Spring Boot**: 应用框架
- **MyBatis**: ORM，动态表名 ${tableName}
- **BIP OpenAPI**: 在线API调用（HmacSHA256签名认证）
- **YMS Framework**: YonBIP模块系统、线程池 (YmsExecutors)
- **Hutool**: HttpUtil / JSONUtil
- **Fastjson**: JSON序列化
- **Guava**: Lists.partition 分片
