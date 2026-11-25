---
name: iuap-c-openapi-integration
description: >-
  BIP旗舰版 OpenAPI 动态查询与集成技能。
  【功能】根据接口描述动态查询BIP OpenAPI文档，返回接口URI、请求方式、入参结构、出参结构。
  【触发场景】查询XX接口、获取API信息、调用BIP接口、生成API调用代码、封装BIP接口。
  【需求确认】用户话术包含"需求确认"关键词时，自动设置 `--confirm Y`，查询所有匹配接口详情。
  【翻译/参照/更新处理】需求明确说"用API/调用API/OpenAPI"时 → 本技能；未明确时 → 路由到 iuap-c-server-codegen。
references:
  - name: openapi_integration_spec
    description: OpenAPI集成规范 - 代码模板、架构说明、禁止项
    path: ./reference/openapi_integration_spec.md
---

# BIP OpenAPI 动态查询与集成

## 快速使用

```bash
# 标准查询（自动精确匹配）
bash run_query.sh --all-interface-desc "销售发票列表查询"

# 需求确认模式（话术含"需求确认"时自动触发）
bash run_query.sh --all-interface-desc "部门"

# 带入参关注点
bash run_query.sh --all-interface-desc "销售发票" --interface-input-hints "组织,客户"

# 精确指定接口
bash run_query.sh --all-interface-desc "部门" --api-name "部门保存"
```

## 需求确认模式

**当用户话术包含"需求确认"关键词时，自动使用 `--confirm Y`**，批量查询所有匹配接口详情。

| 用户话术示例 | 触发条件 |
|-------------|---------|
| "查询部门的API，需求确认" | 含"需求确认" |
| "需求确认场景：部门接口" | 含"需求确认" |

**默认模式**：用户未提及"需求确认"时，使用 `--confirm N`，多匹配时进入表格选择模式。

## 智能匹配策略

优先级：精确指定 > 精确匹配 > 模糊匹配 > 多候选

| 场景 | `--confirm N` | `--confirm Y` |
|------|---------------|---------------|
| 精确匹配 > 1 | 表格选择 | **查所有详情** |
| 精确匹配 = 0 | 表格选择 | **降级模糊，查所有详情** |
| 模糊匹配 > 1 | 表格选择 | **查所有详情** |

## 核心参数

| 参数 | 说明 |
|------|------|
| `--all-interface-desc` | 接口描述（必填，除非使用 `--api-id`） |
| `--api-id` | 直接通过 API ID 查询详情（跳过列表搜索） |
| `--interface-input-hints` | 入参关注点，逗号分隔 |
| `--confirm Y/N` | 默认为（N），含"需求确认"时自动设为 Y |
| `--api-name` | 精确指定接口名称 |
| `--parallel` | 启用并行查询 |

### API ID 查询模式

当用户选择接口列表中的某个接口后，可以直接使用其 API ID 查询详情：

```bash
# 从列表中看到序号30是"部门保存"，API ID为 bafdbde5165c45ef8706f192ca2507e7
# 直接通过 API ID 查询详情，无需重新搜索
bash run_query.sh --api-id "bafdbde5165c45ef8706f192ca2507e7"

# 配合入参关注点
bash run_query.sh --api-id "bafdbde5165c45ef8706f192ca2507e7" --interface-input-hints "部门编码,部门名称"
```

**适用场景**：用户从候选列表中选择某个接口后，希望直接查看该接口的完整详情。

## 翻译/参照/更新处理

| 需求描述 | 路由目标 |
|---------|---------|
| "翻译/参照翻译/参照更新，**用API**" | 本技能（iuap-c-openapi-integration） |
| "翻译/参照翻译/参照更新"（未明确） | iuap-c-server-codegen 技能 |

**判断逻辑**：需求中明确提到"用API"、"调用API"、"OpenAPI"等关键词时，优先使用本技能；未明确说明时，路由到 iuap-c-server-codegen 技能处理。

## 特征字段处理

当接口入参含 `characteristic` 类型字段时，自动查询特征组子字段结构。

| 需求 | 处理方式 |
|------|---------|
| API 支持 characteristic | 特征组 Map 赋值 |
| API 不支持 characteristic | 先 API 保存，再 ElasticTool 更新 |
| 只说"更新特征" | ElasticTool |

## 代码生成规范

> 详见 [reference/openapi_integration_spec.md](reference/openapi_integration_spec.md)

**核心要求**：
- 必须使用 `RestTemplate`，禁止 `HttpURLConnection`
- 通过 `AccessTokenUtils` 获取 AccessToken
- 使用 `@Value` 从 YMS 配置中心获取配置
- 入参/出参严格按 API 文档，禁止臆想

**本地类复用**：先搜索 `{引擎名}-service/src/main/java/com/yonyou/` 下是否存在 AccessToken、OpenApi、Token 相关类。

## 查询结果结构

```json
{
  "接口地址": "/gateway/...",
  "请求方式": "POST",
  "入参列表": [{"参数名": "...", "参数描述": "...", "参数类型": "..."}],
  "出参结构": {...}
}
```

特征组字段会注入 `特征组字段` 属性。

## 常见问题

**Token 获取失败**：检查 app_key/app_secret 是否正确，成功码应为 `00000`。

**多匹配时不知选哪个**：使用 `--confirm Y` 自动获取所有匹配接口详情。
