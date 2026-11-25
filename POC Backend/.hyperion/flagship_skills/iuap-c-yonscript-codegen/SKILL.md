---
name: iuap-c-yonscript-codegen
description: >
  只要需求明确包含“公有云”，必须优先进本 skill，由本 skill 统一编排公有云项目代码生成；其他共用 skill 只能在进入本 skill 后再按编排协同调用。Use when users mention 公有云、后端脚本、YonScript、Rule脚本、API脚本、ObjectStore、YonSQL、YonQL、AbstractTrigger、AbstractAPIHandler、openLinker、postman、invokeFunction、审批后、弃审后、afterAudit、afterUnaudit、Long字段失真、safeJsonParse、批量异步查询、SQL注入、rowIndex回写、发邮件、邮件模板、sendMessage、友空间、短信、定时任务、事件订阅、保存前、保存后、提交前、提交后、审核后、关闭后。允许协同 metadata/openapi/event/mdf 共用 skill，但禁止把公有云后端实现主控制权转交给任何 Java、本地化、业务集成后端生成 skill；本技能最终只生成 YonScript/V8 后端代码，并统一编排前端协同信息采集。
metadata:
  version: 5.0.6
  tags:
    - bip
    - yonbip
    - yonscript
    - v8
    - backend-script
    - public-cloud
---

# BIP 公有云后端脚本开发（YonScript）

## Skill 边界

- 只要需求**明确包含“公有云”**，就必须优先进本 skill 进行统一编排。
- 在公有云项目域内，本 skill 是**唯一后端编排器**、**唯一后端代码生成器**、**唯一集成实现入口**。
- 只生成 YonScript/V8 后端脚本，包括 Rule 脚本、API 脚本、YonSQL 查询、BIP 内部 API 调用、三方调用、消息触达、审批后/弃审后反查。
- 禁止生成 Java、Python、Node.js 服务端代码；与任何 Java / 本地化 / 业务集成后端生成 skill 互斥。
- 前端 MDF 代码可以协同 `iuap-c-mdf-codegen` 生成，但主编排权仍在本 skill。
- BIP OpenAPI 地址、入参、出参未知时，必须先调用 `iuap-c-openapi-integration` 查询，禁止臆想 URL。
- BIP 事件订阅 API 地址、入参、出参未知时，必须先调用 `iuap-c-event-integration` 查询，禁止臆想 URL。
- YonSQL 生成所需的 `uri`、`domain`、字段、查询条件任一不充分，或参照链不完整时，必须先调用 `iuap-c-metadata-info` 查询；四要素齐备且可追溯时可直接拼装。
- OpenAPI、事件、MDF 协同也必须先做信息充分性判定；信息不足时再分别调用 `iuap-c-openapi-integration`、`iuap-c-event-integration`、`iuap-c-mdf-codegen`。

## 架构分层

本 skill 按“治理层 → 通用能力层 → 输入结构层 → 场景编排层 → 指南层 → 历史层”组织。

| 目录 | 职责 |
|---|---|
| `references/00-governance/` | 强制门禁、技术规范、公有云域门禁、输出契约 |
| `references/10-common/` | 通用可复用能力，不承载具体业务场景 |
| `references/20-input/` | MDD/YPD 入参与赋值结构 |
| `references/30-scenes/` | 场景编排：审批后、消息触达、三方集成、流程、相关性 |
| `references/40-guides/` | 辅助指南与速查，不是强制规范 |
| `references/90-history/` | 变更记录，只保留历史，不进入主加载链 |

## 渐进式加载

> 本文件只负责路由、门禁和索引。不要在这里复制 reference 的完整规范或完整代码模板。

### L0：域准入识别

| 识别条件 | 动作 |
|---|---|
| 用户明确提到 `公有云` | 必须优先进本 skill |
| 当前会话/项目上下文已锁定为公有云项目 | 必须继续由本 skill 编排 |
| 未明确 `公有云` 且无当前会话锁 | 不在本 skill 内臆想部署模式 |

进入本 skill 后：

- 禁止再把后端代码生成控制权转交给 Java / 本地化 / 业务集成后端 skill。
- 允许调用共用 skill 做信息采集。
- 必须先收集信息，再生成代码。

### L1：场景识别

| 用户场景 | 脚本类型 | 首选加载 |
|---|---|---|
| 公有云项目整体编排、前后端联动、统一采集 | 编排场景 | `references/30-scenes/00_公有云项目整体编排.md` |
| MDF 前端调用 API 查询/ID转编码/报表参数补充 | API 脚本 + MDF 协同 | `references/30-scenes/06_MDF前端联动API契约.md` |
| 前端 `invokeFunction` 调用、开放函数 | API 脚本 | `references/10-common/01_代码框架.md` |
| MDD 规则链、保存/提交/审核动作 | Rule 脚本 | `references/20-input/01_MDD规则链入参.md` |
| YPD 扩展点、单据插件式动作 | Rule 脚本 | `references/20-input/02_YPD扩展点入参.md` |
| 审批后、弃审后、afterAudit、afterUnaudit | Rule 脚本 + 反查 | `references/30-scenes/01_审批后弃审后反查.md` |
| NCC/SAP/浪潮/三方接口 | API/Rule + 连接器 | `references/30-scenes/03_三方集成封装.md` |
| 邮件、短信、友空间、模板消息 | API/Rule + 消息触达 | `references/30-scenes/02_消息触达.md` |

### L2：治理层前置

生成任何代码前按需加载：

| 何时加载 | reference | 作用 |
|---|---|---|
| 进入公有云项目域后 | `references/00-governance/05_公有云项目域门禁.md` | 准入识别、互斥规则、禁止臆想、采集优先 |
| 最新提示与历史事实漂移 / 复用旧代码前 | `references/00-governance/09_最新需求事实锁定门禁.md` | agent 自动抽取最新业务事实并重建事实锁，隔离历史噪音 |
| 生成任何 YonScript 前 | `references/00-governance/02_V8沙箱安全模式.md` | V8 语言约束：`var`、`function`、类内方法必须带 `this.`、固定入口类 |
| 生成任何业务代码前 | `references/00-governance/01_技术规范.md` | 日志、API 调用、YonSQL、字段、Long、错误处理、注入防护总规范 |
| 使用业务对象/YonSQL/字段前 | `references/00-governance/03_元数据门禁.md` | URI/domain/attributes/tables/characterGroups/refUri 门禁 |
| YonSQL 四要素不充分 / 参照链不完整 | `references/00-governance/06_YonSQL信息充分性门禁.md` | 判断是否可直拼，必要时再补查元数据 |
| OpenAPI / 事件 / MDF 信息不充分 | `references/00-governance/07_协同能力信息充分性门禁.md` | 判断是否需要调用协同 skill 补信息 |
| 无法运行元数据查询 / 无法连库 | `references/00-governance/08_生成准确性保障.md` | Evidence Gate：证据表 + 代码标注 + 人工校验清单 |
| 输出最终结果前 | `references/00-governance/04_输出契约.md` | 统一输出脚本、绑定说明、元数据清单、自检清单 |
| 调 BIP API 且接口信息未知 | `iuap-c-openapi-integration` | 查询真实 URL、方法、入参、出参 |
| 调 BIP 事件订阅 API 且接口信息未知 | `iuap-c-event-integration` | 查询事件订阅 API 地址、方法、入参、出参 |
| 使用业务对象且元数据未知 | `iuap-c-metadata-info` | 查询真实 URI、domain、字段、子表、特征组 |

门禁不通过时，不生成代码；先向用户说明缺少的元数据/API/事件/前端依赖信息。

### L3：通用能力层按需加载

| 触发词/需求 | 加载 |
|---|---|
| 代码骨架、入口类、exports | `references/10-common/01_代码框架.md` |
| YonSQL、业务对象查询、嵌套子查询、特征字段、平行表、业务外键关联、分页 | `references/10-common/02_YonQL语句.md` |
| API 脚本查询、返回给前端、生产日志、DateTime 条件 | `references/10-common/14_API脚本查询与日志模式.md` |
| 字段命名、`_code`、`_name`、特征组、子表 key | `references/10-common/03_字段命名契约.md` |
| Long 精度、`safeJsonParse`、JSON 字符串解析 | `references/10-common/04_Long精度防御.md` |
| BIP 内部 API、跨租户、postman、nccLinker、extrequire | `references/10-common/05_远程调用.md` |
| 实体新增/修改/删除 | `references/10-common/06_实体操作.md` |
| 上下文、环境变量、用户信息 | `references/10-common/07_上下文.md` |
| 加密、解密、签名 | `references/10-common/08_加密解密.md` |
| 字符串处理/验证 | `references/10-common/09_字符串处理.md`、`references/10-common/10_字符串验证.md` |
| 数值、金额、随机码 | `references/10-common/11_一般计算.md`、`references/10-common/12_金额计算.md`、`references/10-common/13_随机码生成.md` |
| 批量异步查询、rowIndex 回写、批量 SQL | `references/10-common/patterns/batch-async-query.md` |

### L4：输入结构层按需加载

| 场景 | 加载 |
|---|---|
| MDD 规则链取数 | `references/20-input/01_MDD规则链入参.md` |
| YPD 扩展点取数 | `references/20-input/02_YPD扩展点入参.md` |
| MDD 规则链赋值 | `references/20-input/03_MDD规则链赋值.md` |
| YPD 扩展点赋值 | `references/20-input/04_YPD扩展点赋值.md` |
| 原始样本核对 | `references/20-input/samples/` |

### L5：场景编排层按需加载

| 触发词/需求 | 加载 |
|---|---|
| 公有云项目整体编排、采集优先、前后端联动 | `references/30-scenes/00_公有云项目整体编排.md` |
| MDF 前端联动 API 契约、ID转编码、后端返回驱动前端解析 | `references/30-scenes/06_MDF前端联动API契约.md` |
| 审批后、弃审后、only-id | `references/30-scenes/01_审批后弃审后反查.md` |
| 发邮件、短信、友空间、模板公式 | `references/30-scenes/02_消息触达.md` |
| 三方系统集成、NCC、SAP、浪潮、统一连接器 | `references/30-scenes/03_三方集成封装.md` |
| 流程/工作流 | `references/30-scenes/04_流程脚本.md` |
| 相关性规则 | `references/30-scenes/05_相关性规则.md` |

### L6：辅助指南层

| 场景 | reference |
|---|---|
| 整体架构、分层职责、维护规则 | `references/40-guides/00_整体架构.md` |
| 三场景字段取值对照 | `references/40-guides/01_metadata-yonsql-guide.md` |
| 脚本绑定到平台 | `references/40-guides/02_后端脚本绑定指南.md` |
| API 速查 | `references/40-guides/03_核心API速查.md` |
| 经验沉淀 | `references/40-guides/04_优化经验.md` |

## 生成决策树

```
用户需求
  ├─ 是否明确包含“公有云”或当前会话已锁定公有云项目？否 → 不在本 skill 内臆想
  ├─ 加载公有云项目域门禁（00-governance/05）
  ├─ 若最新提示与历史事实漂移或准备复用旧代码 → 加载最新需求事实锁定门禁（00-governance/09）
  ├─ 先做需求拆解，确定后端/前端/API/事件/元数据依赖
  ├─ 若涉及 YonSQL → 先判定四要素是否充分；不充分再走 iuap-c-metadata-info
  ├─ 元数据返回后 → 强制确认字段类型：基本类型/参照/特征/子表/平行表/多选引用/业务外键关联
  ├─ 若涉及参照/子表/多选引用/特征字段/平行表/业务外键关联 → 确认运行时关联字段、特征组、refUri、关联键、目标字段和目标 domain
  ├─ 若主元数据中需求涉及参照字段 → 继续查参照元数据
  ├─ 若涉及 API → 先查 iuap-c-openapi-integration
  ├─ 若涉及 BIP 事件订阅 API → 先查 iuap-c-event-integration
  ├─ 若涉及事件 API / eventCode / 报文 → 先查 iuap-c-event-integration
  ├─ 若涉及 MDF 前端 → 先判定页面实体/字段/交互是否充分；不充分先补元数据，再协同 iuap-c-mdf-codegen
  ├─ 完整性校验通过后，加载治理层/输入层/通用层/场景层
  ├─ 生成 YonScript/V8 代码
  └─ 按输出契约返回完整结果
```

## 不可违反

- 变量声明允许 `var`/`let`，优先遵循平台模板和项目风格；避免 `const`、箭头函数、Java/Python/Node.js SDK，除非平台已验证支持；类内方法调用必须使用 `this.xxx()`，禁止裸调。
- 只要需求明确包含“公有云”，就必须优先进本 skill，并由本 skill 统一编排；其他共用 skill 只能在进入本 skill 后按编排协同调用；禁止再把后端生成主控制权转交给 `iuap-c-server-codegen`、任何 Java / 本地化 / 业务集成后端 skill。
- 入口类固定为 `MyTrigger` 或 `MyAPIHandler`，并必须 `exports({ entryPoint: ... })`。
- 任何 `ObjectStore.openLinker`、`ObjectStore.postman`、`param.requestData`、`customerData` 返回/承载的 JSON 字符串，默认按 Long 风险处理；涉及业务主键/外键时必须使用 `safeJsonParse`。
- 禁止凭 URI 前缀推导 domain；domain 必须来自元数据 `_entityInfo.domain`。
- 只要涉及 YonSQL 且 uri/domain/字段/条件任一不明确，就必须先查主元数据；四要素齐备且可追溯时允许直拼。
- 元数据返回后必须先确认字段类型，再决定 YonSQL 或代码写法；禁止把参照、特征、平行表、子表、业务外键关联字段按基本字段处理。
- 业务外键关联必须自动比较源实体和目标实体 `_entityInfo.domain`；同域才可 LEFT JOIN，跨域必须分域查询后脚本组装。
- 如果主元数据返回中存在需求涉及的参照字段，必须继续查询该参照元数据；禁止直接臆想 `code/name` 或目标字段。
- 参照穿透、主子表、多选引用、特征字段查询优先使用嵌套子查询；生产禁止 `select *`；DateTime 字面量必须加引号。
- 只要涉及 API 且 URL/method/request/response 任一不明确，必须先查 API 信息。
- 只要涉及 BIP 事件订阅 API 或事件 API 且 eventCode/报文/API 信息任一不明确，必须先查事件信息。
- 只要涉及 MDF 且页面实体/字段来源/交互方式/后端依赖任一不明确，必须先补信息后再协同生成。
- 生成前必须自动抽取最新提示中的业务事实并与历史事实对比；事实漂移时必须重建事实锁，禁止沿旧错误文件局部改名式生成。
- 禁止使用 `dbColumnName` 拼 YonSQL；字段名必须来自 `attributes[].name`。
- 禁止直接拼接未转义的用户输入到 YonSQL；必须按 `references/00-governance/01_技术规范.md` 的 SQL 注入防护规则处理。
- 任何架构层级、目录职责、加载链或文档归属发生变化时，必须先更新 `references/40-guides/00_整体架构.md`，再更新 `references/90-history/CHANGELOG.md`。
