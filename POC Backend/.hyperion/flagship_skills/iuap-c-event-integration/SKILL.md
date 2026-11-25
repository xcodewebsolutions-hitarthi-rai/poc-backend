---
name: iuap-c-event-integration
description: 用于根据名称查询BIP事件类型、获取事件订阅详情、解析事件报文结构，支持集成开发场景
---

# BIP 事件订阅/监听查询技能

## 功能概述

用于查询事件类型列表和事件详情解析，支持根据名称模糊查询事件，选择后获取事件解析报文。

## 触发场景

- 查询事件订阅
- 监听事件类型
- 获取事件报文格式
- 解析事件详情结构

## 快速使用

### Python 脚本方式

```bash
# 设置环境变量
export API_BASE_URL=https://v5devkk1.yonyoucloud.com
export API_APP_KEY=your_app_key
export API_APP_SECRET=your_app_secret
export YONBIP_SYS_ID=current_yonbip_default_sys

# 查询事件类型（带 --json 只输出报文）
python scripts/event_query.py --query "待结算数据办结" --json

# 查询所有事件
python scripts/event_query.py --all

# 查询指定事件详情
python scripts/event_query.py --detail 1814934938926448649

# 查询事件详情并输出报文 JSON
python scripts/event_query.py --detail 1814934938926448649 --json
```

### 环境变量配置

在 `.env` 文件中配置：

```bash
API_BASE_URL=https://v5devkk1.yonyoucloud.com/
API_APP_KEY=your_app_key_here
API_APP_SECRET=your_app_secret_here
YONBIP_SYS_ID=current_yonbip_default_sys
```

## 执行流程

```
┌─────────────────────────────────────────────────────────────────────┐
│                        事件订阅/监听查询流程                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  Step 1: 查询事件类型列表                                            │
│     │                                                              │
│     ▼                                                              │
│  POST /iuap-api-gateway/{sysId}/AW/list/event                      │
│     │                                                              │
│     ├── 有多个结果 → 让用户选择                                      │
│     │                                                              │
│     ▼                                                              │
│  解析返回报文，提取 eventId                                          │
│     │                                                              │
│     ▼                                                              │
│  Step 2: 查询事件详情                                                │
│     │                                                              │
│     ▼                                                              │
│  GET /iuap-api-gateway/{sysId}/AW/openPortal/event/eventDetail/{id}│
│     │                                                              │
│     ▼                                                              │
│  解析报文：                                                          │
│     │                                                              │
│     ├── data.example 有值 → 直接返回 example 内容                     │
│     │                                                              │
│     └── example 为空 → 递归解析 paramList.children                  │
│                         提取 name、paramDesc、example                │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## API 信息

### API 1: 根据名称查询事件类型

| 属性 | 值 |
|------|-----|
| 请求地址 | `/iuap-api-gateway/{sysId}/AW/list/event` |
| 请求方式 | POST |
| Content-Type | application/json |

**请求头：**

| 参数名 | 类型 | 位置 | 必填 | 描述 |
|--------|------|------|------|------|
| access_token | String | Query | 是 | 调用方应用token |

**Body参数：**

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| eventName | String | 否 | 事件名称（支持模糊查询） |

**请求示例：**

```json
POST /current_yonbip_default_sys/AW/list/event?access_token=访问令牌
Body: {
    "eventName": "员工新增"
}
```

**出参结构：**

| 参数名 | 类型 | 描述 |
|--------|------|------|
| success | boolean | 请求是否成功 |
| code | String | 响应码 |
| message | String | 消息 |
| data | Array | 事件列表 |

**data数组元素：**

| 参数名 | 类型 | 描述 |
|--------|------|------|
| id | String | 事件ID |
| eventCode | String | 事件编码，如 STAFF_ADD |
| eventName | String | 事件名称，如 员工新增事件 |
| eventDesc | String | 事件描述 |
| eventSource | String | 事件来源 |
| domainAppCode | String | 领域应用编码 |
| eventId | String | 事件ID（与id相同） |
| classifyPath | String | 分类路径 |
| characteristic | boolean | 是否有特征 |
| beanMap | Object | Bean映射 |

**正确返回示例：**

```json
{
    "success": true,
    "code": "200",
    "message": "",
    "data": [
        {
            "_status": 0,
            "id": "dac0d1ae8cad4bbdab4f1688f0819761",
            "eventCode": "STAFF_ADD",
            "eventName": "员工新增事件",
            "eventDesc": "员工新增推送新增事件，支持下游拿到新增员工基本信息。",
            "eventSource": "0",
            "domainAppCode": "ucf-staff-center.bd_staff_card",
            "eventId": "dac0d1ae8cad4bbdab4f1688f0819761",
            "classifyPath": "用友 YonBIP-应用平台-数字化建模-组织管理-员工",
            "characteristic": false,
            "beanMap": {}
        }
    ]
}
```

---

### API 2: 查询事件详情

| 属性 | 值 |
|------|-----|
| 请求地址 | `/iuap-api-gateway/{sysId}/AW/openPortal/event/eventDetail/{id}` |
| 请求方式 | GET |
| Content-Type | - |

**请求头：**

| 参数名 | 类型 | 位置 | 必填 | 描述 |
|--------|------|------|------|------|
| access_token | String | Query | 是 | 调用方应用token |
| id | String | Path | 是 | 事件ID |
| isAjax | String | Query | 否 | 默认值: 1 |

**请求示例：**

```
GET /current_yonbip_default_sys/AW/openPortal/event/eventDetail/{id}?access_token=访问令牌&isAjax=1
```

**出参结构：**

| 参数名 | 类型 | 描述 |
|--------|------|------|
| success | boolean | 请求是否成功 |
| code | String | 响应码 |
| message | String | 消息 |
| data | Object | 事件详情 |

**data对象：**

| 参数名 | 类型 | 描述 |
|--------|------|------|
| eventName | String | 事件名称 |
| description | String | 事件描述 |
| example | String | 事件报文示例（JSON字符串格式） |
| eventCode | String | 事件编码 |
| apiInfo | String | API信息 |
| domainAppCode | String | 领域应用编码 |
| paramList | Array | 参数列表 |

**paramList数组元素（递归结构）：**

| 参数名 | 类型 | 描述 |
|--------|------|------|
| id | String | 参数ID |
| sort | Number | 排序 |
| name | String | 参数名称 |
| parentId | String | 父级ID |
| children | Array | 子参数列表（递归） |
| eventId | String | 事件ID |
| paramOrder | Number | 参数顺序 |
| paramDesc | String | 参数描述 |
| paramType | String | 参数类型 |
| arr | String | 数组标识 |
| example | String | 示例值 |
| sourceId | String | 来源ID |
| metaUri | String | 元数据URI |
| translateType | Number | 翻译类型 |
| i18nPath | String | 国际化路径 |

**正确返回示例：**

```json
{
    "success": true,
    "code": "200",
    "message": "",
    "data": {
        "eventName": "员工新增事件",
        "description": "员工新增推送新增事件，支持下游拿到新增员工基本信息。",
        "example": "{\n    \"model\": {\n        \"id\": \"2024551616081231880\",\n        \"code\": \"000001\",\n        \"name\": \"测试\",\n        \"enable\": 1,\n        \"tenantid\": \"0000L5GGG35C5N7K0P0000\"\n    }\n}",
        "eventCode": "STAFF_ADD",
        "apiInfo": "",
        "domainAppCode": "ucf-staff-center.bd_staff_card",
        "paramList": [
            {
                "id": "2246301534086234118",
                "sort": 0,
                "name": "model",
                "parentId": "",
                "children": [
                    {
                        "id": "2246301534086234119",
                        "sort": 0,
                        "name": "id",
                        "parentId": "2246301534086234118",
                        "children": [""],
                        "eventId": "dac0d1ae8cad4bbdab4f1688f0819761",
                        "paramOrder": 0,
                        "paramDesc": "主键",
                        "paramType": "string",
                        "arr": "",
                        "example": "2024551616081231880",
                        "sourceId": "",
                        "metaUri": "",
                        "translateType": 0,
                        "i18nPath": ""
                    }
                ],
                "eventId": "dac0d1ae8cad4bbdab4f1688f0819761",
                "paramOrder": 0,
                "paramDesc": "数据",
                "paramType": "object",
                "arr": "",
                "example": "",
                "sourceId": "",
                "metaUri": "",
                "translateType": 0,
                "i18nPath": ""
            }
        ]
    }
}
```

---

## 报文解析规则

### 解析优先级

```
1. data.example 有值且是有效的 JSON 对象或数组
   └── 直接返回 example 内容（解析JSON字符串后校验类型）
       字段展开时：扁平化 example 取完整字段集，用 paramList 补充 paramDesc/paramType

2. data.example 为空 / 非法 JSON / 标量
   └── 按 data.paramList 递归构建/展开
       └── 遍历 children 数组
           ├── 提取 name
           ├── 提取 paramDesc
           ├── 提取 example
           └── 递归处理 children 中的嵌套 children
```

### 解析算法

```python
def parse_event_detail(data):
    """
    解析事件详情报文
    规则：example 优先 + paramList 兜底/补充
    """
    example = data.get("example")
    parsed_example = None
    if example:
        try:
            parsed_example = json.loads(example)
        except (json.JSONDecodeError, TypeError):
            parsed_example = None

    # 规则1: example 是有效的 JSON 对象或数组 → 优先采用
    #   - 构建报文：直接返回 parsed_example
    #   - 字段展开：扁平化 parsed_example 取完整字段集，
    #     用 paramList 建立 {路径: paramDesc/paramType/字段example} 索引补充元数据
    if isinstance(parsed_example, (dict, list)):
        return parsed_example  # 报文构建直接返回

    # 规则2: example 为空 / 非法 / 标量 → 回退到 paramList 递归构建/展开
    param_list = data.get("paramList", [])

    result = {}

    def parse_children(items, prefix=""):
        for item in items:
            if not item or not isinstance(item, dict):
                continue

            name = item.get("name", "")
            if not name:
                continue

            key = f"{prefix}.{name}" if prefix else name

            result[key] = {
                "name": name,
                "paramDesc": item.get("paramDesc", ""),
                "example": item.get("example", ""),
                "paramType": item.get("paramType", "")
            }

            # 递归解析 children
            children = item.get("children")
            if children and isinstance(children, list):
                parse_children(children, key)

    parse_children(param_list)
    return result
```

---

## 使用示例

### 场景：查询"员工新增"事件

```
用户: 查询"员工新增"的事件订阅详情

助手:
  1. 调用 API 1 查询事件类型
     POST /iuap-api-gateway/{sysId}/AW/list/event
     Body: {"eventName": "员工新增"}

  2. 返回匹配的事件列表:
     - [1] 员工新增事件 (eventId: dac0d1ae8cad4bbdab4f1688f0819761)

  3. 调用 API 2 查询详情
     GET /iuap-api-gateway/{sysId}/AW/openPortal/event/eventDetail/{eventId}

  4. 解析并返回事件报文格式:
     {
         "model": {
             "id": "2024551616081231880",
             "code": "000001",
             "name": "测试",
             "enable": 1,
             "tenantid": "0000L5GGG35C5N7K0P0000"
         }
     }
```

### 场景：查询复杂事件（example为空）

```
用户: 查询某个事件详情

助手:
  1. 查询事件列表，选择目标事件

  2. 调用 API 2 获取详情，发现 example 为空

  3. 解析 paramList.children:
     解析结果:
     {
       "model": {"paramDesc": "数据", "paramType": "object"},
       "model.id": {"paramDesc": "主键", "paramType": "string", "example": "2024551616081231880"},
       "model.code": {"paramDesc": "编码", "paramType": "string", "example": "000001"},
       ...
     }
```

---

## Java代码模板

```java
/**
 * 事件订阅查询服务
 */
@Service
@Slf4j
public class EventSubscriptionService {

    @Value("${bip.api.base-url}")
    private String baseUrl;

    @Value("${bip.api.sys-id:current_yonbip_default_sys}")
    private String sysId;

    /**
     * 根据名称查询事件类型列表
     */
    public List<EventTypeVO> queryEventTypes(String eventName) {
        String url = baseUrl + "/" + sysId + "/AW/list/event";

        // 构建请求体
        Map<String, Object> requestBody = new HashMap<>();
        if (StringUtils.hasText(eventName)) {
            requestBody.put("eventName", eventName);
        }

        // 获取token
        String token = getAccessToken();
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);

        // 构建完整URL
        String fullUrl = url + "?access_token=" + token;

        HttpEntity<Map<String, Object>> entity = new HttpEntity<>(requestBody, headers);
        RestTemplate restTemplate = new RestTemplate();

        try {
            ResponseEntity<Map> response = restTemplate.postForEntity(fullUrl, entity, Map.class);
            Map<String, Object> body = response.getBody();

            List<EventTypeVO> result = new ArrayList<>();
            if (body != null && Boolean.TRUE.equals(body.get("success"))) {
                List<Map<String, Object>> dataList = (List<Map<String, Object>>) body.get("data");
                if (dataList != null) {
                    for (Map<String, Object> data : dataList) {
                        EventTypeVO vo = new EventTypeVO();
                        vo.setId((String) data.get("id"));
                        vo.setEventCode((String) data.get("eventCode"));
                        vo.setEventName((String) data.get("eventName"));
                        vo.setEventDesc((String) data.get("eventDesc"));
                        vo.setEventSource((String) data.get("eventSource"));
                        vo.setDomainAppCode((String) data.get("domainAppCode"));
                        vo.setEventId((String) data.get("eventId"));
                        vo.setClassifyPath((String) data.get("classifyPath"));
                        vo.setCharacteristic((Boolean) data.get("characteristic"));
                        result.add(vo);
                    }
                }
            }
            return result;
        } catch (Exception e) {
            log.error("查询事件类型失败", e);
            return Collections.emptyList();
        }
    }

    /**
     * 查询事件详情并解析报文
     */
    public EventDetailVO getEventDetail(String eventId) {
        String url = baseUrl + "/" + sysId + "/AW/openPortal/event/eventDetail/" + eventId;

        // 获取token
        String token = getAccessToken();
        HttpHeaders headers = new HttpHeaders();

        // 构建完整URL
        String fullUrl = url + "?access_token=" + token + "&isAjax=1";

        RestTemplate restTemplate = new RestTemplate();

        try {
            ResponseEntity<Map> response = restTemplate.getForEntity(fullUrl, Map.class);
            Map<String, Object> body = response.getBody();

            EventDetailVO result = new EventDetailVO();
            if (body != null && Boolean.TRUE.equals(body.get("success"))) {
                Map<String, Object> data = (Map<String, Object>) body.get("data");
                if (data != null) {
                    result.setEventName((String) data.get("eventName"));
                    result.setDescription((String) data.get("description"));
                    result.setEventCode((String) data.get("eventCode"));
                    result.setApiInfo((String) data.get("apiInfo"));
                    result.setDomainAppCode((String) data.get("domainAppCode"));

                    // 解析报文
                    Map<String, Object> parsedMessage = parseEventData(data);
                    result.setParsedMessage(parsedMessage);
                }
            }
            return result;
        } catch (Exception e) {
            log.error("查询事件详情失败", e);
            return new EventDetailVO();
        }
    }

    /**
     * 解析事件报文数据
     * 规则：example 优先 + paramList 兜底
     * 1. example 有值且是有效的 JSON 对象或数组 → 直接返回解析结果
     * 2. 否则（example 为空 / 非法 / 标量）递归解析 paramList.children
     */
    private Map<String, Object> parseEventData(Map<String, Object> data) {
        // 规则1: example 有值，尝试解析（必须是有效的 JSON 对象或数组）
        Object example = data.get("example");
        if (example != null && !example.toString().trim().isEmpty()) {
            String ex = example.toString().trim();
            // 仅当是 JSON 对象或数组时才采用，标量回退到规则2
            if (ex.startsWith("{") || ex.startsWith("[")) {
                try {
                    return JsonUtil.parseObject(ex, Map.class);
                } catch (Exception e) {
                    log.warn("解析example失败，回退到paramList: {}", ex);
                }
            }
        }

        // 规则2: 递归解析 paramList.children
        Map<String, Object> result = new LinkedHashMap<>();
        Object paramListObj = data.get("paramList");
        if (paramListObj instanceof List) {
            List<Map<String, Object>> paramList = (List<Map<String, Object>>) paramListObj;
            parseChildren(paramList, result, "");
        }

        return result;
    }

    @SuppressWarnings("unchecked")
    private void parseChildren(List<Map<String, Object>> items,
                                Map<String, Object> result,
                                String parentKey) {
        for (Map<String, Object> item : items) {
            if (item == null) continue;

            String name = (String) item.get("name");
            if (name == null || name.isEmpty()) continue;

            String key = parentKey.isEmpty() ? name : parentKey + "." + name;

            Map<String, Object> fieldInfo = new LinkedHashMap<>();
            fieldInfo.put("name", name);
            fieldInfo.put("paramDesc", item.get("paramDesc"));
            fieldInfo.put("example", item.get("example"));
            fieldInfo.put("paramType", item.get("paramType"));
            result.put(key, fieldInfo);

            // 递归处理 children
            Object childrenObj = item.get("children");
            if (childrenObj instanceof List) {
                List<Map<String, Object>> children = (List<Map<String, Object>>) childrenObj;
                parseChildren(children, result, key);
            }
        }
    }

    /**
     * 获取AccessToken
     */
    private String getAccessToken() {
        // 复用现有的Token获取逻辑
        return AccessTokenUtils.getToken(
            appKey,  // 从配置注入
            appSecret  // 从配置注入
        );
    }
}
```

### VO类定义

```java
/**
 * 事件类型VO
 */
@Data
public class EventTypeVO {
    private String id;
    private String eventCode;
    private String eventName;
    private String eventDesc;
    private String eventSource;
    private String domainAppCode;
    private String eventId;
    private String classifyPath;
    private Boolean characteristic;
    private Map<String, Object> beanMap;
}

/**
 * 事件详情VO
 */
@Data
public class EventDetailVO {
    private String eventName;
    private String description;
    private String eventCode;
    private String apiInfo;
    private String domainAppCode;
    private Map<String, Object> parsedMessage;
}
```

---

## 常见问题

### Q1: 查询结果为空怎么办？
A: 尝试缩短关键词或使用更通用的名称进行模糊查询，例如"员工"而不是"员工新增"。

### Q2: 事件报文中example和paramList同时有值怎么办？
A: 按规则优先取example，paramList作为备用信息。

### Q3: paramList的children是空字符串怎么处理？
A: 解析时跳过空字符串和null值。

### Q4: 如何确定sysId？
A: sysId是系统标识，常见值如`current_yonbip_default_sys`，可从配置中心获取。

### Q5: 事件报文字段过多怎么办？
A: 按层级结构展示，嵌套字段用点号分隔（如 `model.id`），每个层级缩进2个空格。

### Q6: Token获取失败怎么办？
A: 检查 `.env` 文件中的 `API_APP_KEY` 和 `API_APP_SECRET` 是否正确配置。

### Q7: 事件详情查询返回404怎么办？
A: 部分环境的详情 API 未在网关注册，这是平台限制。可通过联调测试获取实际报文格式。

---

## Python 脚本使用说明

### 目录结构

```
scripts/
├── event_query.py          # 事件查询主脚本
├── iuap_common/
│   ├── __init__.py
│   └── bip_auth.py        # Token获取与缓存模块
└── .venv/                 # Python虚拟环境
```

### bip_auth.py 模块

Token 获取模块，提供以下功能：

- **Token 缓存**：进程内缓存，过期前自动刷新
- **熔断器保护**：连续失败时自动熔断
- **限流器**：防止请求过于频繁
- **重试机制**：401 时自动刷新 token 并重试

#### 签名算法（与 Java SignHelper 一致）

```
1. 参数按 key 字母排序
2. 拼接为 key1value1key2value2...
3. HMAC-SHA256(suiteSecret, 拼接字符串)
4. Base64 编码（不做URL编码）
```

#### API

```python
from iuap_common.bip_auth import get_access_token, http_get_json, http_post_json

# 获取 token
token = get_access_token(
    base_url="https://v5devkk1.yonyoucloud.com",
    app_key="your_app_key",
    app_secret="your_app_secret",
    force_refresh=False  # 是否强制刷新
)

# GET 请求
result = http_get_json(base_url, path, token, {"isAjax": "1"})

# POST 请求
result = http_post_json(base_url, path, token, {"eventName": "员工新增"})

# 清除缓存
from iuap_common.bip_auth import invalidate_token_cache
invalidate_token_cache()
```

### event_query.py 命令行参数

| 参数 | 说明 |
|------|------|
| `--query`, `-q` | 查询事件类型（支持模糊匹配） |
| `--detail`, `-d` | 查询事件详情（需要事件ID） |
| `--select`, `-s` | 选择事件列表中的第N个 |
| `--all`, `-a` | 列出所有事件类型 |
| `--token`, `-t` | 指定 access_token |
| `--force-refresh` | 强制刷新 token |
| `--debug` | 启用调试日志 |
| `--json`, `-j` | **只输出事件报文 JSON**（不输出字段说明，便于复制使用） |

### 输出示例

**不带 --json（完整详情）：**
```bash
python scripts/event_query.py --query "待结算数据办结"
```

**带 --json（纯报文 JSON）：**
```bash
python scripts/event_query.py --query "待结算数据办结" --json
# 输出：
{
  "dataSettledId": "2240516075696422920",
  "code": "SD225040700000043",
  "wdataOrigin": "16",
  ...
}
```
