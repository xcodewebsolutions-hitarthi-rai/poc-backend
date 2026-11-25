---
name: iuap-c-mdf-codegen
description: "MDF 前端扩展开发技能。当用户提及：前端开发、MDF 扩展、ViewModel、页面脚本、字段操作、子表编辑、参照过滤、按钮事件、保存校验、生命周期钩子时触发此技能。提供三层架构：需求解析 → 技术路线决策 → 代码生成。必须依赖：iuap-c-metadata-info。"
---

# MDF 前端扩展开发

## 三层架构

```
┌─────────────────────────────────────────────────────────────┐
│  层级1: 需求解析 → 意图分类                                  │
├─────────────────────────────────────────────────────────────┤
│  层级2: 技术路线决策 → 基于元数据的路由                        │
├─────────────────────────────────────────────────────────────┤
│  层级3: 代码生成 → 模板填充                                  │
└─────────────────────────────────────────────────────────────┘
```

## 意图分类（11 种类型）

**⚠️ 带 🔍 的 Intent 必须先通过元数据查询判定主表/子表**

| Intent | 关键词 | 技术路线 | 主表/子表判定 | 参考文档 |
|--------|--------|---------|-------------|---------|
| `FIELD_READ` 🔍 | 读取、获取、显示值 | `field.getValue()` | ✅ 必须判定 | [simple-model.md](references/models/simple-model.md) |
| `FIELD_WRITE` 🔍 | 设置、赋值、填入值 | `field.setValue()` | ✅ 必须判定 | [simple-model.md](references/models/simple-model.md) |
| `FIELD_LINKAGE` 🔍 | 联动、变化、触发 | `afterValueChange` | ✅ 必须判定 | [field-linkage.md](references/patterns/field-linkage.md) |
| `GRID_READ` | 读取子表、单元格值 | `gridModel.getCellValue()` | 子表 | [grid-model.md](references/models/grid-model.md) |
| `GRID_WRITE` | 写入子表、单元格赋值 | `gridModel.setCellValues()` | 子表 | [grid-model.md](references/models/grid-model.md) |
| `GRID_ROW` | 增行、删行、插入行 | `insertRow/deleteRow` | 子表 | [grid-model.md](references/models/grid-model.md) |
| `REFER_FILTER` 🔍 | 过滤、参照条件 | `beforeBrowse + setFilter` | ✅ 必须判定 | [refer-model.md](references/models/refer-model.md) |
| `BACKEND_QUERY` | 调用API、查询数据 | `invokeFunction` | 不涉及 | [backend-query.md](references/patterns/backend-query.md) |
| `BUTTON_EVENT` | 按钮点击、自定义事件 | `button.on('click')` | 不涉及 | [biz-actions.md](references/events/biz-actions.md) |
| `VALIDATION` | 校验、保存前校验 | `beforeSave` | 不涉及 | [biz-actions.md](references/events/biz-actions.md) |
| `PAGE_COMM` | 弹窗、返回、通信 | `communication()` | 不涉及 | [modal.md](references/patterns/modal.md) |

## 必须的前置条件：元数据查询

**⚠️ 任何字段操作前必须先查询元数据**

使用 `iuap-c-metadata-info` 技能查询：
1. 字段是主表还是子表
2. 子表的 childrenField
3. 特征组和特征编码

详见：[03-metadata-integration.md](references/architecture/03-metadata-integration.md)

## 快速路由表

| 需求 | 参考文档 | 模板 |
|------|---------|------|
| 主表字段 | [simple-model.md](references/models/simple-model.md) | - |
| 表格单元格操作 | [grid-model.md](references/models/grid-model.md) | - |
| **参照过滤（必须先判定主表/子表）** | [refer-model.md](references/models/refer-model.md) | [assets/templates/](assets/templates/) |
| 字段联动 | [field-linkage.md](references/patterns/field-linkage.md) | [field-linkage.js](assets/templates/field-linkage.js) |
| 后端查询 | [backend-query.md](references/patterns/backend-query.md) | - |
| 保存校验 | [biz-actions.md](references/events/biz-actions.md) | [validation.js](assets/templates/validation.js) |
| **生命周期决策** | [lifecycle.md](references/events/lifecycle.md) | - |
| **按钮拦截决策** | 下方"按钮事件决策"章节 | - |
| BaseModel 基类 | [base-model.md](references/models/base-model.md) | - |
| 业务动作卡片 | [biz-action.md](references/patterns/biz-action.md) | - |
| 弹窗通信 | [modal.md](references/patterns/modal.md) | [modal.js](assets/templates/modal.js) |
| 页面跳转 | [page-jump.md](references/patterns/page-jump.md) | - |
| 消息提示 | [message.md](references/patterns/message.md) | - |
| 移动端特性 | [mobile.md](references/patterns/mobile.md) | - |
| **移动端 API** | [mobile-api.md](references/mobile-api.md) | - |
| 上下文 | [context.md](references/patterns/context.md) | - |
| 缓存存储 | [storage.md](references/patterns/storage.md) | - |
| 扩展机制 | [extend.md](references/patterns/extend.md) | - |
| **通用模式** | [common-patterns.md](references/patterns/common-patterns.md) | - |
| **工具脚本** | [utils-script.md](references/utils-script.md) | - |

## 完整文档索引

详见文档目录：`references/` 目录下包含完整参考文档。

### 架构层
- [02-technical-decision.md](references/architecture/02-technical-decision.md) - 决策矩阵（**必读**）
- [04-feasibility-check.md](references/architecture/04-feasibility-check.md) - 需求可行性判断
- [05-develop-workflow.md](references/architecture/05-develop-workflow.md) - 开发流程与调试

### Model 层
- [simple-model.md](references/models/simple-model.md) - 主表字段
- [grid-model.md](references/models/grid-model.md) - 子表操作
- [refer-model.md](references/models/refer-model.md) - 参照过滤

### 事件层
- [lifecycle.md](references/events/lifecycle.md) - **生命周期决策表**（**必读**）
- [biz-actions.md](references/events/biz-actions.md) - 业务动作

### 模式层
- [common-patterns.md](references/patterns/common-patterns.md) - 通用模式
- [backend-query.md](references/patterns/backend-query.md) - 后端查询

### 规则层
- [01-lint-rules.md](references/rules/01-lint-rules.md) - Lint 禁止规则

### API 速查
- [api-index.md](references/api-index.md) - 一行式 API 速查

## 核心规则

1. **必须使用 `cb.define()`** - 禁止 IIFE 闭包
2. **必须先查询元数据判定主表/子表** - 用户未明确时，必须用 `iuap-c-metadata-info` 查询
3. **禁止循环调用 insertRow/deleteRow** - 必须使用批量方法
4. **禁止 `field.set('value')`** - 必须使用 `field.setValue()`
5. **参照过滤禁止 return filter** - 必须使用 `this.setFilter()`
6. **枚举字段比较 `.value`** - 不是 `.value.value`
7. **按钮拦截走对的路** - 标准动作用 `viewModel.on()`，自定义按钮用 `button.on()`

## 快速决策入口

### 业务需求 → 事件选择

| 业务描述 | 选择事件 | 说明 |
|---------|---------|------|
| "打开页面时设置默认值" | `afterLoadMeta` | 元数据加载后、UI渲染前 |
| "数据加载后计算总金额" | `afterLoadData` | 数据已加载完成 |
| "保存前检查编码是否重复" | `beforeSave` | 可阻断保存流程 |
| "保存成功后刷新" | `afterSave` | 保存完成后 |
| "提交前检查审批人" | `beforeSubmit` | 可阻断提交流程 |
| "新增时自动填充日期" | `modeChange(mode='add')` | 模式切换时 |
| "字段变更触发计算" | `afterValueChange` | 字段值变更后 |
| "单元格变更触发计算" | `afterCellValueChange` | 子表单元格变更后 |
| "参照打开前过滤数据" | `beforeBrowse` | 参照弹窗前 |
| "按钮点击调用接口" | `button.on('click')` | 自定义按钮 |

详见：[lifecycle.md](references/events/lifecycle.md)

### 按钮拦截决策

| 按钮类型 | 判断标准 | 正确写法 |
|---------|---------|---------|
| 标准动作（save/submit/delete等） | 有 action | `viewModel.on('beforeSave', fn)` |
| 自定义按钮 | 无 action | `viewModel.get('btnXxx').on('click', fn)` |

详见：[biz-actions.md](references/events/biz-actions.md)

### 后端调用决策

| 场景 | 调用方式 |
|------|---------|
| 公有云 + 未指定 | `invokeFunction`（**默认**）|
| 公有云 + 明确"Java后端" | Java API |
| 外部第三方 API | `setProxy` |

详见：[backend-query.md](references/patterns/backend-query.md)

## 5 个典型场景

详见 [02-technical-decision.md](references/architecture/02-technical-decision.md)：

1. **场景1**：参照过滤 - 按组织过滤供应商
2. **场景2**：保存校验 - 查询重复编码
3. **场景3**：按钮点击 - 外部 API 调用
4. **场景4**：字段联动 - 自动填充
5. **场景5**：子表批量操作

## 代码模板

| 模板 | 文件路径 |
|------|---------|
| 主表过滤 | `simple-model-filter.js` |
| 子表同步过滤 | `grid-filter-sync.js` |
| 字段联动 | `field-linkage.js` |
| 保存校验 | `validation.js` |
| 按钮事件 | `button-event.js` |
| 弹窗通信 | `modal.js` |

## 依赖技能

- [iuap-c-metadata-info](../iuap-c-metadata-info/SKILL.md) - 必须依赖，先查询元数据
