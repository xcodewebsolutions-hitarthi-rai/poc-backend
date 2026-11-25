# BIP标准API调用框架

> **⚠️ 强制规范：本框架根据不同场景选择HTTP客户端，详见下方说明。**

本技能提供一套标准化的BIP（用友BIP）OpenAPI调用规范，适用于任何需要与BIP平台交互的Java/Spring项目。

## 架构概述

```
┌─────────────────────────────────────────────────────────────────┐
│                      业务服务层                                  │
│              (Business Service - 业务调用方)                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    BIP API 调用框架                              │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐          │
│  │   鉴权模块   │    │  HTTP调用模块 │    │  响应解析模块 │          │
│  │AccessToken │    │HttpURLConn  │    │ JSON解析   │          │
│  │  Utils    │    │(获取Token)   │    │           │          │
│  └─────────────┘    └─────────────┘    └─────────────┘          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    BIP OpenAPI 平台                             │
│         (网关地址 + API URI + AccessToken)                       │
└─────────────────────────────────────────────────────────────────┘
```

## HTTP客户端选择规范

| 场景 | HTTP客户端 | 说明 |
|------|-----------|------|
| **获取Token接口** | `HttpURLConnection` | ⚠️ 必须使用，RestTemplate的GET请求设置Content-Type会导致500错误 |
| **业务API调用** | `HttpURLConnection` | ✅ 推荐，与Postman行为一致 |

> **⚠️ 重要说明**：使用RestTemplate发送GET请求时，即使不设置body，某些服务器端会对Content-Type header做特殊处理。用友BIP的Token接口对Content-Type敏感，建议统一使用HttpURLConnection以确保行为与Postman一致。

```java
// ✅ 正确：获取Token使用HttpURLConnection
public Map<String, Object> getMethodForToken(String requestUrl) {
    HttpURLConnection conn = null;
    try {
        URL url = new URL(requestUrl);
        conn = (HttpURLConnection) url.openConnection();
        conn.setRequestMethod("GET");
        conn.setRequestProperty("Accept", "application/json");  // 只设置Accept，不设置Content-Type
        // ...
    }
}

// ❌ 错误：RestTemplate的GET请求设置Content-Type会导致500
RestTemplate restTemplate = new RestTemplate();
HttpHeaders headers = new HttpHeaders();
headers.setContentType(MediaType.APPLICATION_JSON);  // ❌ GET请求不需要，且会导致BIP Token接口500
ResponseEntity<Map> response = restTemplate.exchange(url, HttpMethod.GET,
    new HttpEntity<>(headers), Map.class);
```

## 核心组件

### 1. 鉴权模块 - AccessTokenUtils

负责获取BIP平台的访问令牌。

> **⚠️ 签名算法关键点**：
> 1. 签名参数只包含 `appKey` 和 `timestamp`，**不包括 `signature` 本身**
> 2. 必须使用 `TreeMap` 确保参数按字母顺序排序
> 3. 先计算签名，再将签名加入params

```java
@Component
public class AccessTokenUtils {

    private static final String URL_TOKEN = "/open-auth/selfAppAuth/getAccessToken";

    @Autowired
    private OpenApiUtils openApiUtils;

    /**
     * 获取AccessToken
     * @param openApiUrl 认证域名 (如: http://xxx/iuap-api-auth)
     * @param appKey 应用Key
     * @param appSecret 应用密钥
     * @return access_token字符串
     */
    public String getAccessToken(String openApiUrl, String appKey, String appSecret) throws Exception {
        // ⚠️ 必须使用TreeMap确保参数按字母顺序排序
        Map<String, Object> params = new TreeMap<>();
        params.put("appKey", appKey);
        params.put("timestamp", String.valueOf(System.currentTimeMillis()));

        // ⚠️ 关键：先计算签名，此时params只有appKey和timestamp（不包括signature）
        String signature = SignHelper.sign(params, appSecret);
        // 计算完签名后再加入signature参数
        params.put("signature", signature);

        String requestUrl = openApiUrl + URL_TOKEN;
        // ⚠️ 使用专用Token获取方法，不附加access_token参数
        Map<String, Object> responseMap = openApiUtils.getMethodForToken(params, requestUrl);

        if (responseMap == null || responseMap.get("data") == null) {
            throw new RuntimeException("access token 获取失败!");
        }
        return JSONObject.parseObject(JSON.toJSONString(responseMap.get("data")))
                .getString("access_token");
    }
}
```

**⚠️ 常见错误示例**：

```java
// ❌ 错误：在签名计算之前就把signature放入params
Map<String, Object> params = new HashMap<>();
params.put("appKey", appKey);
params.put("timestamp", ...);
params.put("signature", sign(params, appSecret)); // ❌ 签名内容包含了signature本身！

// ❌ 错误：使用HashMap不保证顺序
Map<String, Object> params = new HashMap<>(); // ❌ 顺序不确定

// ✅ 正确：先签名（不含signature），再加入signature
Map<String, Object> params = new TreeMap<>();
params.put("appKey", appKey);
params.put("timestamp", ...);
String signature = sign(params, appSecret); // ✅ 此时params只有appKey和timestamp
params.put("signature", signature);
```

**禁止在（application.yml）配置：**
```yaml
# BIP应用配置
bip:
  appKey: your_app_key
  appSecret: your_app_secret
  # 网关地址（可选，如已在代码中拼接则不需要）
  # gatewayHost: http://xxx/iuap-api-gateway
  # 认证地址（可选）
  # authHost: http://xxx/iuap-api-auth

# 域名配置
domain:
  url: http://your-domain
```
**必须使用YMS配置，同时采用@Value模式引用配置**


### 2. HTTP调用模块 - OpenApiUtils

提供标准化的GET/POST请求方法。使用HttpURLConnection确保与Postman行为一致。

```java
@Component
public class OpenApiUtils {

    private static final int CONNECT_TIMEOUT = 120000;
    private static final int READ_TIMEOUT = 120000;

    /**
     * GET请求
     * @param requestUrl 完整请求URL（包含query参数）
     * @return 响应Map
     */
    public Map<String, Object> getMethod(String requestUrl) {
        HttpURLConnection conn = null;
        BufferedReader reader = null;
        StringBuilder result = new StringBuilder();

        try {
            URL url = new URL(requestUrl);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("GET");
            conn.setConnectTimeout(CONNECT_TIMEOUT);
            conn.setReadTimeout(READ_TIMEOUT);
            conn.setRequestProperty("Accept", "application/json");  // ⚠️ 只设置Accept，不设置Content-Type

            int responseCode = conn.getResponseCode();
            if (responseCode == 200) {
                reader = new BufferedReader(new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8));
                String line;
                while ((line = reader.readLine()) != null) {
                    result.append(line);
                }
                return JSON.parseObject(result.toString());
            } else {
                throw new RuntimeException("BIP API GET请求失败，响应码: " + responseCode);
            }
        } catch (Exception e) {
            throw new RuntimeException("BIP API GET请求失败: " + e.getMessage(), e);
        } finally {
            closeQuietly(reader);
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    /**
     * 获取Token专用GET请求（不附加access_token参数）
     * ⚠️ 用于 open-auth/selfAppAuth/getAccessToken 接口
     * @param params 请求参数
     * @param requestUrl 完整API路径
     * @return 响应Map
     */
    public Map<String, Object> getMethodForToken(Map<String, Object> params, String requestUrl) {
        StringBuilder urlBuilder = new StringBuilder(requestUrl);
        if (params != null && !params.isEmpty()) {
            urlBuilder.append("?");
            for (Map.Entry<String, Object> entry : params.entrySet()) {
                if (entry.getValue() != null) {
                    urlBuilder.append(entry.getKey())
                              .append("=")
                              .append(entry.getValue())
                              .append("&");
                }
            }
            // 移除末尾多余的 &
            if (urlBuilder.charAt(urlBuilder.length() - 1) == '&') {
                urlBuilder.deleteCharAt(urlBuilder.length() - 1);
            }
        }
        return getMethod(urlBuilder.toString());
    }

    /**
     * POST请求
     * @param jsonBody JSON字符串请求体
     * @param requestUrl 完整请求URL
     * @return 响应字符串
     */
    public String postMethod(String jsonBody, String requestUrl) {
        HttpURLConnection conn = null;
        OutputStreamWriter out = null;
        BufferedReader reader = null;
        StringBuilder result = new StringBuilder();

        try {
            URL url = new URL(requestUrl);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            conn.setDoInput(true);
            conn.setConnectTimeout(CONNECT_TIMEOUT);
            conn.setReadTimeout(READ_TIMEOUT);
            conn.setRequestProperty("Content-Type", "application/json;charset=UTF-8");
            conn.setRequestProperty("Accept", "application/json");

            out = new OutputStreamWriter(conn.getOutputStream(), StandardCharsets.UTF_8);
            out.write(jsonBody);
            out.flush();

            int responseCode = conn.getResponseCode();
            if (responseCode == 200) {
                reader = new BufferedReader(new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8));
                String line;
                while ((line = reader.readLine()) != null) {
                    result.append(line);
                }
                return result.toString();
            } else {
                throw new RuntimeException("BIP API POST请求失败，响应码: " + responseCode);
            }
        } catch (Exception e) {
            throw new RuntimeException("BIP API POST请求失败: " + e.getMessage(), e);
        } finally {
            closeQuietly(out);
            closeQuietly(reader);
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    private void closeQuietly(Closeable closeable) {
        if (closeable != null) {
            try {
                closeable.close();
            } catch (IOException ignored) {}
        }
    }
}
```

### 3. URI常量定义 - BipOpenApiUriConst

统一管理所有BIP API的URI地址。

```java
public class BipOpenApiUriConst {
    
    /**
     * 网关和认证地址
     */
    public static final String OPEN_GATEWAY_HOST = "/iuap-api-gateway";
    public static final String OPEN_AUTH_HOST = "/iuap-api-auth";
    
    /**
     * 通用档案查询URI
     * 具体业务API URI由各模块自行定义
     * 例如：
     * - 客户档案: /yonbip/digitalModel/merchant/queryByPage
     * - 供应商档案: /yonbip/digitalModel/vendor/queryByPage
     * - 物料档案: /yonbip/digitalModel/product/queryByPage
     */
    
    /**
     * 通用详情查询URI模板
     */
    public static final String DETAIL_TEMPLATE = "/yonbip/{module}/{entity}/detail";
    
    /**
     * 通用保存URI模板
     */
    public static final String SAVE_TEMPLATE = "/yonbip/{module}/{entity}/save";
    
    /**
     * 通用审核URI模板
     */
    public static final String AUDIT_TEMPLATE = "/yonbip/{module}/{entity}/audit";
}
```

## 标准调用流程

### Step 1: 配置初始化

在Service基类中初始化网关地址：

```java
public abstract class BaseBipServiceImpl implements InitializingBean {

    protected String openGatewayHost;
    protected String openAuthHost;

    @Value("${domain.url}")
    protected String domainUrl;

    @Value("${bip.appKey}")
    protected String appKey;

    @Value("${bip.appSecret}")
    protected String appSecret;

    @Override
    public void afterPropertiesSet() throws Exception {
        openGatewayHost = domainUrl + BipOpenApiUriConst.OPEN_GATEWAY_HOST;
        openAuthHost = domainUrl + BipOpenApiUriConst.OPEN_AUTH_HOST;
    }
}
```

### Step 2: 获取AccessToken

```java
@Autowired
AccessTokenUtils accessTokenUtils;

String accessToken = accessTokenUtils.getAccessToken(
    openAuthHost, 
    appKey, 
    appSecret
);
```

### Step 3: 构建请求URL

```java
String requestUrl = openGatewayHost 
    + apiUri 
    + "?access_token=" + accessToken 
    + "&id=" + entityId;
```

### Step 4: 发起调用

```java
Map<String, Object> params = new HashMap<>();
// 添加业务参数
params.put("paramKey", paramValue);

Map<String, Object> result = OpenApiUtils.getMethod(params, requestUrl);
```

### Step 5: 解析响应

```java
if ("200".equals(result.get("code"))) {
    Object data = result.get("data");
    JSONObject jsonObject = JSONObject.parseObject(JsonUtils.toJson(data));
    return jsonObject;
}
return null;
```

## 完整示例

以下为通用调用模板，请根据实际业务替换占位符：

```java
/**
 * BIP API 通用调用模板
 * @param apiUri API路径，如: /yonbip/sd/vouchersaleinvoice/detail
 * @param entityId 业务实体ID
 * @return 业务数据JSONObject
 */
protected JSONObject queryBipEntityById(String apiUri, String entityId) throws Exception {
    // 1. 获取令牌
    String accessToken = accessTokenUtils.getAccessToken(
        openAuthHost, 
        appKey, 
        appSecret
    );
    
    // 2. 构建请求参数
    Map<String, Object> params = new HashMap<>();
    
    // 3. 拼接完整URL
    String requestUrl = openGatewayHost 
        + apiUri 
        + "?access_token=" + accessToken 
        + "&id=" + entityId;
    
    // 4. 发起GET请求
    Map<String, Object> responseMap = OpenApiUtils.getMethod(params, requestUrl);
    
    // 5. 解析响应
    if ("200".equals(responseMap.get("code"))) {
        Object data = responseMap.get("data");
        return JSONObject.parseObject(JsonUtils.toJson(data));
    }
    
    return null;
}

/**
 * POST调用示例 - 保存业务数据
 */
protected JSONObject saveBipEntity(String apiUri, Map<String, Object> businessData) throws Exception {
    // 1. 获取令牌
    String accessToken = accessTokenUtils.getAccessToken(
        openAuthHost, 
        appKey, 
        appSecret
    );
    
    // 2. 构建完整URL
    String requestUrl = openGatewayHost + apiUri + "?access_token=" + accessToken;
    
    // 3. 发起POST请求
    String responseStr = OpenApiUtils.postMethod(businessData, requestUrl, accessToken);
    
    // 4. 解析响应
    JSONObject responseMap = JSONObject.parseObject(responseStr);
    if ("200".equals(responseMap.get("code"))) {
        return responseMap.getJSONObject("data");
    }
    
    return null;
}
```

## 响应标准格式

BIP API返回的标准JSON格式：

```json
{
    "code": "200",
    "data": { ... },
    "message": "success"
}
```

或错误响应：

```json
{
    "code": "500",
    "message": "错误描述",
    "detailMessage": "详细错误信息"
}
```

## 异常处理规范

```java
try {
    Map<String, Object> result = OpenApiUtils.getMethod(params, url);
    
    if (!"200".equals(result.get("code"))) {
        String errorMsg = String.format("BIP API调用失败: code=%s, message=%s",
            result.get("code"), 
            result.get("message")
        );
        throw new RuntimeException(errorMsg);
    }
    
    return result.get("data");
} catch (Exception e) {
    LOGGER.error("调用BIP API异常", e);
    throw new BusinessException("调用BIP接口失败: " + e.getMessage());
}
```

```java

### 4. 签名工具类 - SignHelper

> **⚠️ 签名算法关键点**：
> 1. 签名内容 = key1 + value1 + key2 + value2 ...（按字母顺序）
> 2. **签名时不包含 signature 字段本身**
> 3. 使用 HmacSHA256 + Base64 + URLEncoder

```java
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.InvalidKeyException;
import java.security.NoSuchAlgorithmException;
import java.util.Base64;
import java.util.Map;
import java.util.TreeMap;

/**
 * BIP OpenAPI 签名工具类
 */
public class SignHelper {

    /**
     * 按参数名排序后依次拼接参数名称与数值，
     * 之后对该字符串使用 HmacSHA256 加签，
     * 加签结果进行 base64 返回
     *
     * ⚠️ 关键：params中不应包含signature字段
     *
     * @param params      请求参数 map（不包含signature）
     * @param suiteSecret 套件密钥，用作 mac key
     * @return 签名
     */
    public static String sign(Map<String, Object> params, String suiteSecret)
            throws NoSuchAlgorithmException, UnsupportedEncodingException, InvalidKeyException {

        // ⚠️ 必须使用TreeMap确保按字母顺序排序
        TreeMap<String, Object> treeMap = new TreeMap<>(params);

        StringBuilder stringBuilder = new StringBuilder();
        for (Map.Entry<String, Object> entry : treeMap.entrySet()) {
            stringBuilder.append(entry.getKey()).append(entry.getValue());
        }

        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(suiteSecret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        byte[] signData = mac.doFinal(stringBuilder.toString().getBytes(StandardCharsets.UTF_8));
        String base64String = Base64.getEncoder().encodeToString(signData);
        return URLEncoder.encode(base64String, "UTF-8");
    }
}
```

**签名示例**：

```
输入参数：
- appKey = "ddd355dc5ad2491ebd7b74fde79fbc0c"
- timestamp = "1778501016028"

TreeMap排序后：
appKey → ddd355dc5ad2491ebd7b74fde79fbc0c
timestamp → 1778501016028

签名内容字符串：
appKeyddd355dc5ad2491ebd7b74fde79fbc0ctimestamp1778501016028

签名结果（HmacSHA256 + Base64 + URLEncode）：
BgKvtlZzlSt3yXA%2FRUuGAPVC5HTfj5qbFd4Ta7rmlz0%3D
```
```

## GET vs POST 选择

| 场景 | 推荐方式 | 说明 |
|------|---------|------|
| 查询详情 | GET | 参数拼接在URL中，如 `/detail?id=xxx` |
| 分页查询 | POST | 请求体包含分页参数和查询条件 |
| 保存/更新 | POST | 请求体包含完整业务数据 |
| 批量操作 | POST | 请求体包含批量数据数组 |

## 入参准确性规范（强制）

> **⚠️ 核心原则：入参必须严格按照 API 文档构建，禁止臆想参数名和参数类型**

### 入参构建流程

```java
/**
 * 根据 API 文档构建入参
 * @param apiInfo API查询结果（来自Python脚本）
 * @param bizData 业务数据
 * @return 构建好的请求参数
 */
public Map<String, Object> buildRequestParams(Map<String, Object> apiInfo, Map<String, Object> bizData) {
    Map<String, Object> params = new HashMap<>();

    // 1. 获取入参列表（来自API查询结果）
    List<Map<String, Object>> inputParams = (List<Map<String, Object>>) apiInfo.get("入参列表");

    for (Map<String, Object> param : inputParams) {
        String paramName = (String) param.get("参数名");
        Boolean required = (Boolean) param.get("必填");
        String paramType = (String) param.getOrDefault("参数类型", "String");
        Object defaultValue = param.get("默认值");

        // 必填参数校验
        if (Boolean.TRUE.equals(required)) {
            if (!bizData.containsKey(paramName) && defaultValue == null) {
                throw new IllegalArgumentException("必填参数[" + paramName + "]未提供");
            }
        }

        // 获取参数值
        Object value = bizData.get(paramName);
        if (value == null) {
            value = defaultValue;
        }

        // 类型转换
        params.put(paramName, convertValue(value, paramType));
    }

    return params;
}

/**
 * 参数类型转换
 */
private Object convertValue(Object value, String paramType) {
    if (value == null) return null;

    switch (paramType.toLowerCase()) {
        case "integer":
        case "int":
            if (value instanceof Number) return ((Number) value).intValue();
            return Integer.parseInt(value.toString());
        case "long":
            if (value instanceof Number) return ((Number) value).longValue();
            return Long.parseLong(value.toString());
        case "double":
        case "decimal":
            if (value instanceof Number) return ((Number) value).doubleValue();
            return Double.parseDouble(value.toString());
        case "boolean":
            if (value instanceof Boolean) return value;
            return Boolean.parseBoolean(value.toString());
        default:
            return value.toString();
    }
}
```

### 禁止臆想参数

```java
// ❌ 错误：臆想参数名（可能与实际API不符）
Map<String, Object> params = new HashMap<>();
params.put("product_code", code);      // 参数名可能是 productCode
params.put("org_id", orgId);           // 参数名可能是 orgId 或 orgCode

// ✅ 正确：使用API查询结果的参数名
List<Map<String, Object>> inputParams = apiInfo.get("入参列表");
for (Map<String, Object> param : inputParams) {
    String paramName = (String) param.get("参数名");
    params.put(paramName, bizData.get(paramName));
}
```

## 出参解析规范（强制）

> **⚠️ 核心原则：出参必须严格按照 API 文档解析，确保数据准确性**

### 出参解析流程

```java
/**
 * 根据 API 文档解析出参
 * @param response API响应字符串
 * @param apiInfo API查询结果（来自Python脚本）
 * @param targetClass 目标类型
 * @return 解析后的对象
 */
public <T> T parseResponse(String response, Map<String, Object> apiInfo, Class<T> targetClass) {
    // 1. 解析响应字符串
    JSONObject result = JSON.parseObject(response);

    // 2. 校验响应码
    String code = result.getString("code");
    if (!"200".equals(code)) {
        String message = result.getString("message");
        throw new RuntimeException("API调用失败: code=" + code + ", message=" + message);
    }

    // 3. 获取出参结构（来自API查询结果）
    Map<String, Object> outputStruct = (Map<String, Object>) apiInfo.get("出参结构");

    // 4. 按出参结构解析数据
    Object data = result.get("data");
    if (data == null) {
        return null;
    }

    // 5. 转换为目标类型
    String dataJson = JSON.toJSONString(data);
    return JSON.parseObject(dataJson, targetClass);
}
```

### 出参结构示例

```java
// API返回结构
// {
//   "code": "200",
//   "data": {
//     "id": 123,
//     "billno": "INV2024010001",
//     "creator": {
//       "id": 456,
//       "name": "张三"
//     },
//     "items": [
//       {"productCode": "P001", "qty": 10, "price": 100.00}
//     ]
//   },
//   "message": "success"
// }

// 出参结构（来自API查询结果）
// {
//   "code": {"类型": "String", "描述": "响应码"},
//   "data": {
//     "类型": "Object",
//     "子字段": {
//       "id": {"类型": "Long", "描述": "单据ID"},
//       "billno": {"类型": "String", "描述": "单据号"},
//       "creator": {
//         "类型": "Object",
//         "子字段": {
//           "id": {"类型": "Long", "描述": "创建人ID"},
//           "name": {"类型": "String", "描述": "创建人姓名"}
//         }
//       },
//       "items": {"类型": "Array", "描述": "明细行"}
//     }
//   }
// }

// 解析为 DTO
public class InvoiceDTO {
    private Long id;
    private String billno;
    private CreatorDTO creator;
    private List<InvoiceItemDTO> items;
    // getter/setter
}

// 使用
InvoiceDTO invoice = parseResponse(response, apiInfo, InvoiceDTO.class);
```

## 最佳实践

1. **统一入口**: 所有BIP API调用必须经过本框架，确保鉴权和异常处理一致
2. **配置分离**: API地址、appKey、appSecret等配置放在配置文件中
3. **日志记录**: 记录请求URL、参数、响应状态，便于问题排查
4. **异常封装**: 自定义业务异常类，区分系统异常和业务异常
5. **超时控制**: 为HttpURLConnection配置连接超时和读取超时（建议120秒）
6. **重试机制**: 对于非幂等性操作，考虑添加重试机制
7. **Token缓存**: 生产环境建议实现Token缓存机制，避免频繁请求
8. **常量抽取**: 所有API URI必须抽取到 `BipOpenApiUriConst` 常量类，禁止硬编码

---

## ⚠️ API URI常量规范（强制）

### 为什么必须抽取常量

| 问题 | 硬编码的后果 | 使用常量的好处 |
|------|-------------|--------------|
| 维护困难 | 多个地方硬编码，改一处要改全部 | 改常量类一处，全局生效 |
| 可读性差 | 字符串难以理解 | 常量名语义清晰 |
| 容易出错 | 拼写错误难以发现 | 编译时检查 |
| 代码冗余 | 相同URI重复出现 | 复用常量 |

### 强制规范

> **⚠️ 禁止在代码中硬编码API URI！**
>
> 所有API URI必须：
> 1. 定义在 `BipOpenApiUriConst` 常量类中
> 2. 使用常量引用，禁止字符串拼接

### 正确示例

```java
// ✅ 正确：引用常量
@Autowired
private OpenApiConfig openApiConfig;

public JSONObject queryOrder(String billId) {
    String requestUrl = openApiConfig.getOpenGatewayHost()
            + BipOpenApiUriConst.PU_ORDER_DETAIL
            + "?access_token=" + accessToken
            + "&id=" + billId;
    return openApiUtils.getMethod(requestUrl);
}

// ✅ 正确：在常量类中定义
public class BipOpenApiUriConst {
    // 采购订单API
    public static final String PU_ORDER_DETAIL = "/yonbip/scm/purchaseorder/detail";
    public static final String PU_ORDER_SAVE = "/yonbip/scm/purchaseorder/save";
}
```

### 错误示例

```java
// ❌ 错误：硬编码API地址
public JSONObject queryOrder(String billId) {
    String requestUrl = "https://xxx.com/iuap-api-gateway/yonbip/scm/purchaseorder/detail"
            + "?access_token=" + accessToken
            + "&id=" + billId;
    return openApiUtils.getMethod(requestUrl);
}

// ❌ 错误：字符串拼接API地址
public JSONObject queryOrder(String billId) {
    String requestUrl = openApiConfig.getOpenGatewayHost()
            + "/yonbip/scm/purchaseorder/detail"  // ❌ 硬编码！
            + "?access_token=" + accessToken
            + "&id=" + billId;
    return openApiUtils.getMethod(requestUrl);
}
```

### 新增API常量流程

```
1. 在 BipOpenApiUriConst.java 中添加常量
2. 在 Client/Service 类中引用常量
3. 禁止在任何地方硬编码API URI
```

## 常见问题排查

### 1. "签名不正确" 错误

**可能原因**：
- 签名参数包含了 `signature` 本身（应先计算签名，再加入params）
- 未使用 `TreeMap` 导致参数顺序不一致
- `appKey`、`appSecret` 与平台配置不一致

**排查步骤**：
```java
// 1. 打印签名内容
TreeMap<String, Object> treeMap = new TreeMap<>(params);
StringBuilder signContent = new StringBuilder();
for (Map.Entry<String, Object> entry : treeMap.entrySet()) {
    signContent.append(entry.getKey()).append(entry.getValue());
}
System.out.println("签名内容: " + signContent.toString());
System.out.println("签名结果: " + SignHelper.sign(params, appSecret));
```

### 2. "access token 获取失败" 或 500 错误

**可能原因**：
- GET请求设置了 `Content-Type: application/json`（BIP Token接口对此敏感）
- URL中附加了空的 `access_token=` 参数
- 网络连接问题

**排查步骤**：
```java
// 1. 检查请求URL是否正确
System.out.println("请求URL: " + url);

// 2. 确认GET请求没有设置Content-Type
// HttpURLConnection只设置Accept，不设置Content-Type

// 3. 确认URL中没有空的access_token参数
// 获取Token接口不应该有access_token参数
```

### 3. URL拼接规范

```java
// ✅ 正确：获取Token接口不附加access_token
String url = baseUrl + "/open-auth/selfAppAuth/getAccessToken?appKey=xxx&timestamp=xxx&signature=xxx"

// ✅ 正确：业务API附加access_token
String url = baseUrl + "/yonbip/xxx/detail?access_token=" + token + "&id=" + id

// ❌ 错误：获取Token接口附加了空的access_token
String url = baseUrl + "/open-auth/...?access_token=" // ❌ 不应该有空值
```

## 扩展建议

1. **Token自动刷新**: 实现Token过期自动刷新机制
2. **统一响应包装**: 封装统一的响应结果类
3. **日志追踪**: 添加请求ID便于日志追踪
4. **熔断降级**: 集成Sentinel或Hystrix实现熔断
5. **API版本管理**: 不同版本API使用不同URI常量类
