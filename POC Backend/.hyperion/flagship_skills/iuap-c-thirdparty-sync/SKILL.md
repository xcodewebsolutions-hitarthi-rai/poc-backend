---
name: iuap-c-thirdparty-integration
description: 第三方系统单据对接代码生成工具。根据用户描述的第三方集成场景（WMS、LIMS、MES等），生成符合用友BIP平台规范的第三方对接代码。支持4种对接方式：rule规则、plugin插件、页面按钮后端接口、事件中心监听。生成内容包括字段转换、接口调用、状态回写。触发条件：用户提到第三方系统对接、WMS、LIMS、MES、第三方集成、第三方同步等关键词。
triggers:
  - 第三方对接
  - WMS集成
  - LIMS集成
  - MES集成
  - 第三方系统集成
  - 第三方同步
---

# 规则

- 所有单据查询操作（本领域和非本领域）统一使用 `IBillQueryRepository`
- 本领域单据的增删改操作使用 `IBillRepository`，事件扩展使用Rule规则方式
- 非领域单据的增删改操作使用 `IYmsJdbcApi`或者`IComOperateApi`rpc调用，事件扩展使用plugin插件方式
- 优先使用批量操作方法，避免循环逐条处理
- 如果项目中已有对应实体类则直接使用，禁止新建实体类
- 必须引入正确的依赖，不能报错
- 参照字段（如人员、组织等）需要先通过编码查询到ID，再进行赋值

# 工作流程

## Step 1：确认第三方对接信息

从用户输入中提取：
- 单据名称（如 `采购订单`）或 单据URI（fullName）
- 第三方系统名称（WMS、LIMS、MES等）
- 接口文档是否提供
- 对接方式（rule规则/plugin插件/页面按钮调后端接口/事件中心监听）
- 动作出发时机（审批后/保存后/其他）
- BIP单据接口地址（只有使用api查询/更新单据信息时需要；使用单据元数据增删改查时不需要IBillQueryRepository、IBillRepository、IYmsJdbcApi）

如果信息不完整，向用户确认。

## Step 2：判断对接方式

根据业务场景选择最合适的对接方式：

| 对接方式 | 适用场景 | 实现方式 |
|---------|---------|----------|
| rule规则 | 单据保存/提交时自动触发第三方集成逻辑 | 实现 IYpdCommonRul 接口 |
| plugin插件 | 单据生命周期各节点做处理 | 继承 AbstractBillPlugin |
| 页面按钮调后端接口 | 用户在页面点击按钮触发第三方调用 | 暴露外部接口（Controller） |
| 事件中心监听 | 跨系统异步解耦 | 事件监听开发 |

## Step 3：确认第三方接口信息

有接口文档则从接口文档中获取
- 第三方系统名称（如 WMS、LIMS、MES等）
- 第三方接口API地址
- 认证方式（Bearer Token、API Key等）
- 请求方式
- 传值方式

根据接口文档情况处理：
- 有文档：按文档实现字段映射
- 无文档：创建TODO占位，标注需要补充的内容

## Step 4：查询单据元数据

调用 `iuap-c-metadata` 技能，根据单据名称或URI查询单据元数据，获取字段信息。

## Step 5：生成代码

调用`iuap-c-server-codegen`技能获取开发知识，根据选择的对接方式和单据元数据，生成对应的代码：

以下为部分示例
### 5.1 rule规则模板
```java
@Component
public class ThirdPartySyncRule implements IYpdCommonRul {
    @Override
    public void execute(RulContext rulContext) {
        // 获取单据数据
        Map billData = rulContext.getBill();
        // TODO: 调用第三方接口
        // TODO: 处理返回值并回写状态
    }
}
```

### 5.2 plugin插件模板
```java
@BillPlugin("thirdPartySyncPlugin")
public class ThirdPartySyncPlugin extends AbstractBillPlugin {
    @Override
    public void onSave(BillContext billContext) {
        // TODO: 调用第三方接口
    }

    @Override
    public void onSubmit(BillContext billContext) {
        // TODO: 调用第三方接口
    }
}
```

### 5.3 页面按钮Controller模板
```java
@RestController
@RequestMapping("/thirdParty/sync")
public class ThirdPartySyncController extends BaseController {
    @PostMapping("/execute")
    public Result<Void> execute(@RequestBody SyncRequest request) {
        // TODO: 调用第三方接口
        // TODO: 回写状态
        return Result.success();
    }
}
```

### 5.4 事件监听模板
```java
public class DemoRestEventListener {
    @ResponseBody
    @RequestMapping(value = "/event/demoListener", method = RequestMethod.POST)
    public Object onEvent(HttpServletRequest request) {
        // 获取事件入参（字段名固定）
        String sourceID   = request.getParameter("sourceID");
        String eventType  = request.getParameter("eventType");
        String userObject = request.getParameter("userObject");
        String queueName  = request.getParameter("queueName");
        String tenantCode = request.getParameter("tenantCode");

        // 幂等处理（非常关键）
        if (alreadyProcessed(sourceID, eventType, userObject)) {
            Map<String, String> response = new HashMap<>();
            response.put("success", "true");
            response.put("msg", "repeat request, ignore");
            return response;
        }

        // 业务处理
        try {
            handleBusiness(sourceID, eventType, userObject, tenantCode, queueName);
            Map<String, String> response = new HashMap<>();
            response.put("success", "true"); // 必须是字符串 "true"
            response.put("msg", "ok");
            return response;
        } catch (Exception e) {
            Map<String, String> response = new HashMap<>();
            response.put("success", "false");
            response.put("msg", e.getMessage();
            return response;
        }
    }
}
```

## Step 6：字段转换与接口调用

根据接口文档生成字段转换代码：
```java
// 有文档时
requestBody.put("第三方字段名", billData.get("BIP字段名"));

// 无文档时
// TODO: 待第三方提供接口文档后补充以下字段映射
```

接口调用代码：
```java
@Autowired
private RestTemplate restTemplate;
// TODO: 待补充第三方接口，再进行调用实现
HttpHeaders headers = new HttpHeaders();
headers.setContentType(MediaType.APPLICATION_JSON);
HttpEntity<String> entity = new HttpEntity<>(requestBody.toJSONString(), headers);
ResponseEntity<String> response = restTemplate.postForEntity(url, entity, String.class);
```

## Step 7：状态回写代码

根据返回值判断成功/失败，回写单据状态：
```java
// 可以使用 IYmsJdbcApi / RPC调用 / 单据API 三种方式

// 判断返回值
if ("200".equals(response.getStatusCode().toString()) || success) {
    // 回写成功状态
    bizObject.set("fsyncstatus", 1); // 同步成功
} else {
    // 回写失败状态和错误信息
    bizObject.set("fsyncstatus", 2); // 同步失败
    bizObject.set("ferrordesc", errorMsg);
}
```

## Step 8：输出前自检

- 是否有字段转换逻辑
- 是否有HTTP调用代码
- 是否有状态回写逻辑
- 是否正确判断第三方返回值成功/失败
- TODO注释是否清晰标注待补充内容
- 是否正确选择了查询/写操作接口
- 是否包含租户条件
- 是否正确设置了 `_status`
- 参照字段是否有翻译逻辑
- 依赖引入是否正确和完整
- 代码是否有报错

# 参考文档

详细开发规范参考：`<skill-base>/references/第三方单据对接开发.md`